"""
watchmaker

Batch mark whole series as watched or unwatched on aniworld.to, bs.to family,
and s.to family streaming sites.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "settings"))

from config import (  # noqa: E402
    CREDENTIALS,
    DEFAULT_BATCH_FILE,
    DOMAIN_ORDER,
    FAILED_URLS_FILE,
    HTTP_REQUEST_TIMEOUT,
    LOG_FILE,
    LOGS_DIR,
    SUPPORTED_DOMAINS,
    USER_AGENT,
)

logger = logging.getLogger("watchmaker")

REACHABILITY_TIMEOUT = 8.0
_ANIME_SLUG_RE = re.compile(r"^/anime/stream/([^/?#]+)/?")
_SERIE_SLUG_RE = re.compile(r"^/serie/([^/?#]+)/?")
_ANIME_SEASON_RE = re.compile(r"/staffel-(\d+)")
_BS_SEASON_RE = re.compile(r"/serie/[^/]+/(\d+)/")
_STO_SEASON_RE = re.compile(r"/staffel-(\d+)")


# ==================== SETUP ====================
def setup_logging(verbose: bool = False) -> None:
    Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()
    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"

    if verbose:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(fmt))
        logger.addHandler(console)

    fh = logging.FileHandler(LOG_FILE, encoding="utf-8", mode="a")
    fh.setFormatter(logging.Formatter(fmt))
    logger.addHandler(fh)


# ==================== URL PARSING ====================
def _normalize_host(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def classify_url(url: str) -> tuple[str, str, str] | None:
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    host = _normalize_host(parsed.netloc)
    family = SUPPORTED_DOMAINS.get(host)
    if not family:
        return None

    path = parsed.path or "/"
    if family == "aniworld":
        m = _ANIME_SLUG_RE.match(path)
    else:
        m = _SERIE_SLUG_RE.match(path)
    return (host, family, m.group(1)) if m else None


def extract_season(url: str, family: str) -> int | None:
    path = urlparse(url).path or "/"
    if family == "bs":
        m = _BS_SEASON_RE.search(path)
    else:
        m = (_ANIME_SEASON_RE if family ==
             "aniworld" else _STO_SEASON_RE).search(path)
    return int(m.group(1)) if m else None


def load_url_batches(source: str) -> tuple[dict[str, list[str]], list[dict]]:
    grouped: dict[str, list[str]] = {host: [] for host in SUPPORTED_DOMAINS}
    rejected: list[dict] = []

    with open(source, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith(("http://", "https://")):
                rejected.append({"line": line, "reason": "missing http(s)://"})
                continue

            classification = classify_url(line)
            if classification is None:
                host = _normalize_host(urlparse(line).netloc)
                reason = f"unsupported host: {host}" if host and host not in SUPPORTED_DOMAINS else "could not extract slug"
                rejected.append({"line": line, "reason": reason})
                continue

            host, _family, slug = classification
            if slug:
                grouped[host].append(line)
            else:
                rejected.append(
                    {"line": line, "reason": "could not extract slug"})

    ordered: dict[str, list[str]] = {}
    for host in DOMAIN_ORDER:
        if grouped.get(host):
            ordered[host] = list(dict.fromkeys(grouped[host]))
    return ordered, rejected


# ==================== HOST CHECK ====================
async def check_host(host: str) -> tuple[bool, str]:
    url = f"https://{host}"
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=REACHABILITY_TIMEOUT,
    ) as client:
        for method in ("HEAD", "GET"):
            try:
                r = await client.head(url) if method == "HEAD" else await client.get(url)
                if r.status_code < 400:
                    return True, f"{method} {r.status_code}"
                if method == "HEAD" and r.status_code == 405:
                    continue
                return False, f"{method} {r.status_code}"
            except httpx.TimeoutException:
                if method == "GET":
                    return False, "timeout"
            except Exception as exc:  # noqa: BLE001
                if method == "GET":
                    return False, f"{exc.__class__.__name__}"
    return False, "unreachable"


async def filter_reachable(grouped: dict[str, list[str]]) -> tuple[dict[str, list[str]], dict[str, str]]:
    reachable: dict[str, list[str]] = {}
    statuses: dict[str, str] = {}
    for host in sorted(grouped):
        ok, msg = await check_host(host)
        statuses[host] = f"{'OK' if ok else 'FAIL'} ({msg})"
        if ok:
            reachable[host] = grouped[host]
            logger.info("Reachable %s (%s)", host, msg)
        else:
            logger.warning("Unreachable %s (%s) — skipping %d URL(s)",
                           host, msg, len(grouped[host]))
    return reachable, statuses


# ==================== DOMAIN WORKER ====================
class SeriesResult:
    def __init__(self, host: str, family: str, url: str, slug: str):
        self.host = host
        self.family = family
        self.url = url
        self.slug = slug
        self.seasons: list[dict] = []
        self.subscribed: bool | None = None
        self.watchlist: bool | None = None
        self.ok = True

    @property
    def total_episodes(self) -> int:
        return sum(s.get("total", 0) for s in self.seasons)

    @property
    def watched_episodes(self) -> int:
        return sum(s.get("watched_after", s.get("watched_before", 0)) for s in self.seasons)

    @property
    def season_labels(self) -> list[str]:
        return [str(s.get("season", "?")) for s in self.seasons]

    def line(self) -> str:
        sub = "✓" if self.subscribed else "✗" if self.subscribed is False else "?"
        wl = "✓" if self.watchlist else "✗" if self.watchlist is False else "?"
        status = "✓" if self.ok and self.watched_episodes == self.total_episodes else "✗"
        return (
            f"{status} {self.slug} [{','.join(self.season_labels)}]: "
            f"{self.watched_episodes}/{self.total_episodes} watched "
            f"(Sub:{sub} WL:{wl})"
        )

    def detail_lines(self) -> list[str]:
        lines = []
        for s in self.seasons:
            label = s.get("season", "?")
            before = s.get("watched_before", 0)
            after = s.get("watched_after", before)
            total = s.get("total", 0)
            lines.append(f"    S{label}: {before}/{total} -> {after}/{total}")
        return lines


class DomainWorker:
    def __init__(self, host: str):
        self.host = host
        self.family = SUPPORTED_DOMAINS.get(host)
        if not self.family:
            raise ValueError(f"Unsupported host: {host}")
        self.creds = CREDENTIALS.get(self.family, {})
        self.client: httpx.AsyncClient | None = None
        self.logged_in = False

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=HTTP_REQUEST_TIMEOUT,
        )
        return self

    async def __aexit__(self, *args):
        if self.client:
            await self.client.aclose()

    async def _get(self, url: str) -> str:
        r = await self.client.get(url)
        r.raise_for_status()
        return r.text

    async def _post(self, url: str, data: dict | None = None, *, json: dict | None = None, headers: dict | None = None) -> httpx.Response:
        merged = dict(headers) if headers else {}
        return await self.client.post(url, data=data, json=json, headers=merged)

    def _csrf_headers(self, token: str, json: bool = True) -> dict[str, str]:
        h = {
            "X-CSRF-TOKEN": token,
            "X-Requested-With": "XMLHttpRequest",
        }
        if json:
            h["Accept"] = "application/json"
        return h

    @staticmethod
    def _extract_csrf_token(text: str) -> str | None:
        soup = BeautifulSoup(text, "html.parser")
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta:
            return meta.get("content") or None
        # fallback: look for _token in a logout/account form
        for inp in soup.find_all("input", attrs={"name": "_token", "value": True}):
            return inp.get("value") or None
        return None

    async def login(self) -> bool:
        if self.logged_in:
            return True
        if not any(self.creds.values()):
            logger.error("No credentials for family %r", self.family)
            return False

        base = f"https://{self.host}"
        try:
            ok = await self._login_form(base)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Login failed for %s: %s", self.host, exc)
            return False
        self.logged_in = ok
        return ok

    async def _login_form(self, base: str) -> bool:
        family = self.family
        login_url = f"{base}/login"
        text = await self._get(login_url)
        soup = BeautifulSoup(text, "html.parser")

        if family == "bs":
            token_input = soup.find("input", {"name": "security_token"})
            token = token_input.get("value", "") if token_input else ""
            if not token:
                logger.warning(
                    "CSRF security_token not found on login page for %s", self.host)
            r = await self._post(login_url, data={
                "login[user]": self.creds.get("username", ""),
                "login[pass]": self.creds.get("password", ""),
                "security_token": token,
            })
            if r.status_code not in (200, 301, 302):
                return False
            text = await self._get(base)
            nav = BeautifulSoup(text, "html.parser").select_one(
                "section.navigation")
            return nav is not None and nav.find("a", href="logout") is not None

        # aniworld + s.to family
        form = soup.find("form")
        payload: dict[str, str] = {}
        if form:
            for inp in form.find_all("input", attrs={"name": True}):
                payload[inp.get("name", "")] = inp.get("value", "")
        payload["email"] = self.creds.get("email", "")
        payload["password"] = self.creds.get("password", "")

        # s.to family uses _token; aniworld uses security_token
        token = payload.get("_token") or payload.get("security_token", "")
        if not token:
            # final fallback: search the whole page
            for name in ("_token", "security_token"):
                inp = soup.find("input", {"name": name, "value": True})
                if inp:
                    token = inp.get("value", "")
                    break
        if token:
            logger.info("Login CSRF token for %s: %s...",
                        self.host, token[:16])
        else:
            logger.warning(
                "CSRF token not found on login page for %s", self.host)

        r = await self._post(login_url, data=payload)
        if r.status_code not in (200, 301, 302):
            return False
        text = await self._get(base)
        soup = BeautifulSoup(text, "html.parser")
        if family == "aniworld":
            return soup.select_one("div.avatar a[href*='/user/profil/']") is not None
        return soup.select_one("form[action='/logout']") is not None

    async def discover_seasons(self, url: str) -> list[int | str]:
        text = await self._get(url)
        soup = BeautifulSoup(text, "html.parser")
        seasons: set[int] = set()

        if self.family == "aniworld":
            has_movies = False
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/anime/stream/" in href:
                    if "/staffel-" in href:
                        try:
                            seasons.add(
                                int(href.rsplit("/staffel-", 1)[1].split("/", 1)[0]))
                        except (ValueError, IndexError):
                            continue
                    elif "/filme" in href:
                        has_movies = True
            for el in soup.find_all(attrs={"data-season-id": True}):
                try:
                    seasons.add(int(el["data-season-id"]))
                except (ValueError, TypeError):
                    pass
            result: list[int | str] = sorted(seasons)
            if has_movies:
                result.append("Filme")
            return result if result else [1]
        elif self.family == "bs":
            for a in soup.find_all("a", href=True):
                parts = a["href"].strip("/").split("/")
                if len(parts) >= 3 and parts[0] == "serie":
                    try:
                        seasons.add(int(parts[2]))
                    except (ValueError, IndexError):
                        pass
            for opt in soup.find_all("option", value=True):
                if opt["value"].isdigit():
                    seasons.add(int(opt["value"]))
        else:  # sto
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/serie/" in href and "/staffel-" in href:
                    try:
                        seasons.add(
                            int(href.rsplit("/staffel-", 1)[1].split("/", 1)[0]))
                    except (ValueError, IndexError):
                        pass
            for el in soup.find_all(attrs={"data-season-id": True}):
                try:
                    seasons.add(int(el["data-season-id"]))
                except (ValueError, TypeError):
                    pass

        return sorted(seasons) if seasons else [1]

    def _count_episodes(self, text: str) -> tuple[int, int]:
        """Return (watched_count, total_count) for a season page."""
        soup = BeautifulSoup(text, "html.parser")
        family = self.family
        rows: list = []
        if family == "aniworld":
            rows = soup.select("table.seasonEpisodesList tbody tr[data-episode-id]")
            if not rows:
                rows = soup.select("table.seasonEpisodesList tbody tr")
        elif family == "bs":
            table = soup.select_one("table.episodes")
            if table:
                rows = [r for r in table.select("tr") if r.find_all("td")]
        else:  # sto
            rows = soup.select(".episode-table tbody tr.episode-row")
            if not rows:
                rows = soup.select("tr.episode-row")
            if not rows:
                rows = soup.select(".episode-row")
            if not rows:
                table = soup.select_one("table.episodes")
                if table:
                    rows = [r for r in table.select("tr") if r.find_all("td")]

        total = len(rows)
        watched = 0
        for row in rows:
            classes = row.get("class") or []
            if "seen" in classes or "watched" in classes:
                watched += 1
        return watched, total

    def _detect_subscription_status(self, text: str) -> tuple[bool | None, bool | None]:
        """Return (subscribed, watchlist) for aniworld/s.to families."""
        if self.family not in ("aniworld", "sto"):
            return None, None
        soup = BeautifulSoup(text, "html.parser")
        subscribed: bool | None = None
        watchlist: bool | None = None
        if self.family == "aniworld":
            container = soup.select_one("div.add-series")
            if container:
                subscribed = container.get("data-series-favourite") == "1"
                watchlist = container.get("data-series-watchlist") == "1"
            if subscribed is None:
                subscribed = soup.select_one(
                    "li.setFavourite.buttonAction.true") is not None
            if watchlist is None:
                watchlist = soup.select_one(
                    "li.setWatchlist.buttonAction.true") is not None
        else:  # sto
            buttons = soup.select(
                ".d-none.d-md-flex .js-action-btn") or soup.select(".js-action-btn")
            for button in buttons:
                data_type = button.get("data-type")
                active = "btn-glass-primary" in (
                    button.get("class") or []) or button.get("data-active") == "1"
                if data_type == "favorite":
                    subscribed = active
                elif data_type == "watchlater":
                    watchlist = active
        return subscribed, watchlist

    async def ensure_subscribed(self, url: str) -> bool:
        """Subscribe to a series if the subscribe control is present and not active."""
        if not self.logged_in and not await self.login():
            return False

        family = self.family
        base = f"https://{self.host}"
        try:
            text = await self._get(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not load series page for subscribe check: %s", exc)
            return False

        soup = BeautifulSoup(text, "html.parser")

        if family == "aniworld":
            container = soup.select_one("div.add-series")
            subscribed = False
            if container:
                fav_val = container.get("data-series-favourite")
                if fav_val is not None:
                    subscribed = fav_val == "1"
                else:
                    subscribed = soup.select_one(
                        "li.setFavourite.buttonAction.true") is not None
            if subscribed:
                logger.info("Already subscribed: %s", url)
                return True

            series_id = container.get("data-series-id") if container else None
            if not series_id:
                logger.warning("No series-id found for subscribe on %s", url)
                return False

            endpoint = f"{base}/ajax/setFavourite"
            r = await self._post(endpoint, data={"series": series_id})
            return r.status_code == 200 and "status\" :true" in r.text

        elif family == "sto":
            token = self._extract_csrf_token(text)
            buttons = soup.select(
                ".d-none.d-md-flex .js-action-btn") or soup.select(".js-action-btn")
            sub_url: str | None = None
            for button in buttons:
                if button.get("data-type") == "favorite":
                    if "btn-glass-primary" in (button.get("class") or []) or button.get("data-active") == "1":
                        logger.info("Already subscribed: %s", url)
                        return True
                    sub_url = button.get("data-url")
                    break
            if not sub_url:
                logger.warning("No favorite toggle URL found for %s", url)
                return False
            if not token:
                logger.warning("No CSRF token found for subscribe on %s", url)
                return False
            r = await self._post(
                urljoin(url, sub_url),
                headers=self._csrf_headers(token, json=False),
            )
            ok = r.status_code == 200
            if not ok:
                logger.warning("Subscribe failed for %s: %s body=%r",
                               url, r.status_code, r.text[:200])
            return ok

        return True

    async def mark_season(self, url: str, season: int | str, action: str) -> dict:
        """Mark one season and return before/after episode counts."""
        family = self.family
        base = f"https://{self.host}"
        slug = url.split("/anime/stream/" if family ==
                         "aniworld" else "/serie/", 1)[1].split("/", 1)[0]
        if isinstance(season, str) and season.lower() == "filme":
            season_url = f"{base}/anime/stream/{slug}/filme"
        else:
            season_url = (
                f"{base}/anime/stream/{slug}/staffel-{season}"
                if family == "aniworld"
                else f"{base}/serie/{slug}/staffel-{season}"
                if family == "sto"
                else f"{base}/serie/{slug}/{season}"
            )

        text = await self._get(season_url)
        before_watched, total = self._count_episodes(text)
        result = {
            "season": season,
            "watched_before": before_watched,
            "watched_after": before_watched,
            "total": total,
            "ok": True,
        }

        try:
            if family == "aniworld":
                span = BeautifulSoup(text, "html.parser").find(
                    "span", class_="clearAllEpisodesFromThisSeason")
                if not span or not span.has_attr("data-season-id"):
                    raise RuntimeError(f"No clear-all control for {slug} s{season}")
                endpoint = f"{base}/anime/stream/{slug}/season/{span['data-season-id']}/mark"
                r = await self._post(endpoint, data={"action": "true" if action == "watched" else "false"})
                if r.status_code not in (200, 301, 302):
                    raise RuntimeError(f"mark returned {r.status_code}")
            elif family == "bs":
                endpoint = f"{base}/serie/{slug}/{season}/des/{'watch:all' if action == 'watched' else 'unwatch:all'}"
                await self._get(endpoint)
            else:  # sto
                token = self._extract_csrf_token(text)
                ctrl = BeautifulSoup(
                    text, "html.parser").select_one("#season-mark")
                if not ctrl or not ctrl.has_attr("data-mark-url"):
                    raise RuntimeError(f"No #season-mark control for {slug} s{season}")
                if not token:
                    raise RuntimeError(f"No CSRF token for {slug} s{season}")
                mark_url = urljoin(season_url, ctrl["data-mark-url"])
                r = await self._post(
                    mark_url,
                    json={"action": "seen" if action ==
                          "watched" else "unseen"},
                    headers=self._csrf_headers(token),
                )
                logger.info("s.to mark POST %s -> %s body=%r",
                            mark_url, r.status_code, r.text[:200])
                if r.status_code not in (200, 301, 302):
                    raise RuntimeError(f"mark returned {r.status_code}")
                if r.text.strip():
                    try:
                        mark_data = json.loads(r.text)
                        if mark_data.get("ok") is not True:
                            raise RuntimeError(
                                f"s.to mark returned ok={mark_data.get('ok')}")
                    except json.JSONDecodeError:
                        pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed marking %s season %s: %s", url, season, exc)
            result["ok"] = False
            return result

        try:
            text = await self._get(season_url)
            after_watched, _ = self._count_episodes(text)
            result["watched_after"] = after_watched
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not re-check season %s after mark: %s", season, exc)
        return result

    async def mark_series(self, url: str, action: str) -> SeriesResult:
        if not self.logged_in and not await self.login():
            result = SeriesResult(self.host, self.family, url, url.split("/anime/stream/" if self.family ==
                                "aniworld" else "/serie/", 1)[1].split("/", 1)[0])
            result.ok = False
            return result

        family = self.family
        slug = url.split("/anime/stream/" if family ==
                         "aniworld" else "/serie/", 1)[1].split("/", 1)[0]
        result = SeriesResult(self.host, family, url, slug)

        try:
            seasons = await self.discover_seasons(url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Season discovery failed for %s: %s", url, exc)
            result.ok = False
            return result

        if action == "watched" and family in ("aniworld", "sto"):
            try:
                await self.ensure_subscribed(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Subscribe failed for %s: %s", url, exc)

        for season in seasons:
            season_result = await self.mark_season(url, season, action)
            result.seasons.append(season_result)
            if not season_result["ok"]:
                result.ok = False

            # Re-check subscription/watchlist status after each season
            if family in ("aniworld", "sto"):
                try:
                    text = await self._get(url)
                    result.subscribed, result.watchlist = self._detect_subscription_status(
                        text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Status check failed for %s: %s", url, exc)

        logger.info("[%s] %s seasons %s action=%s",
                    "OK" if result.ok else "FAIL", url, seasons, action)
        return result


# ==================== BATCH PROCESSOR ====================
async def process_batch(urls_file: str, action: str) -> tuple[dict, list[SeriesResult]]:
    grouped, rejected = load_url_batches(urls_file)
    stats = {
        "total_urls": sum(len(urls) for urls in grouped.values()),
        "rejected": rejected,
        "successful": 0,
        "failed": 0,
        "failed_urls": [],
        "skipped_hosts": [],
    }
    results: list[SeriesResult] = []

    if stats["total_urls"] == 0:
        logger.warning("No supported URLs found.")
        return stats, results

    original = dict(grouped)
    grouped, statuses = await filter_reachable(grouped)
    grouped = deduplicate_family_mirrors(grouped, statuses)
    stats["skipped_hosts"] = [
        {"host": host, "urls": len(
            urls), "status": statuses.get(host, "unknown")}
        for host, urls in original.items() if host not in grouped
    ]

    if not grouped:
        logger.warning("No reachable hosts.")
        return stats, results

    for host, urls in grouped.items():
        print(f"\n  → {host}: {len(urls)} series")
        logger.info("Processing %s (%d URLs)", host, len(urls))

        async with DomainWorker(host) as worker:
            num_w = len(str(len(urls)))
            for idx, url in enumerate(urls, 1):
                short = url.rsplit("/", 1)[-1] or url
                print(f"    [{idx:>{num_w}}/{len(urls)}] {short} ...", end=" ", flush=True)
                result = await worker.mark_series(url, action)
                results.append(result)
                if result.ok:
                    stats["successful"] += 1
                else:
                    stats["failed"] += 1
                    stats["failed_urls"].append(url)
                print(result.line())

    _persist_failed_urls(stats)
    return stats, results


def _print_run_summary(stats: dict, results: list[SeriesResult]) -> None:
    total = stats["total_urls"]
    ok_count = stats["successful"]
    fail = stats["failed"]
    skipped = stats.get("skipped_hosts", [])
    total_eps = sum(r.total_episodes for r in results)
    watched_eps = sum(r.watched_episodes for r in results)

    print("\n" + "=" * 56)
    print("  run summary")
    print("=" * 56)
    for r in results:
        print(f"  {r.line()}")
        for line in r.detail_lines():
            print(line)
    print("-" * 56)
    print(f"  series total: {total} | ok: {ok_count} | failed: {fail}")
    print(f"  episodes: {watched_eps}/{total_eps} watched")
    if skipped:
        print(f"  skipped hosts: {len(skipped)}")
    if fail:
        print(f"  failed list: {FAILED_URLS_FILE}")


def _persist_failed_urls(stats: dict) -> None:
    Path(FAILED_URLS_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(FAILED_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats.get("failed_urls", []), f, indent=2, ensure_ascii=False)
    logger.info("Finished: %d successful, %d failed",
                stats["successful"], stats["failed"])


# ==================== UI ====================
def _status_emoji(status: str) -> str:
    if status.startswith("OK"):
        return "✓"
    return "✗"


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def print_banner() -> None:
    print("=" * 56)
    print("  watchmaker  —  batch mark series")
    print("=" * 56)


def print_menu(urls_file: str, statuses: dict[str, str]) -> None:
    print(f"\n  batch file: {urls_file}")
    print("\n  hosts:")
    if statuses:
        host_w = max(len(h) for h in statuses)
        print(f"    {'Host':<{host_w}}  Status")
        print(f"    {'-' * host_w}  ------")
        for host, status in sorted(statuses.items()):
            emoji = _status_emoji(status)
            short = status[3:] if status.startswith(
                "OK ") else status[5:] if status.startswith("FAIL ") else status
            print(f"    {host:<{host_w}}  {emoji} {short}")
    else:
        print("    (no supported URLs)")
    print("\n  options:")
    print("    1  mark as WATCHED")
    print("    2  mark as UNWATCHED")
    print("    0  exit")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        choice = input(prompt + suffix).strip().lower()
        if not choice:
            return default
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print("  please enter y or n.")


def print_batch_summary(urls_file: str, action: str = "") -> None:
    grouped, rejected = load_url_batches(urls_file)
    total = sum(len(urls) for urls in grouped.values())
    verb = action.lower() if action else "process"
    print(f"\n  → {total} series to {verb}")
    if grouped:
        for host, urls in sorted(grouped.items()):
            print(f"      • {host}: {len(urls)}")
    if rejected:
        print(f"    ⚠ skipped {len(rejected)} unsupported URL(s)")


def validate_credentials_for_batch(urls_file: str) -> list[str]:
    grouped, _ = load_url_batches(urls_file)
    used = {SUPPORTED_DOMAINS[host]
            for host in grouped if host in SUPPORTED_DOMAINS}
    return [family for family in used if not any(CREDENTIALS.get(family, {}).values())]


def _batch_has_urls(urls_file: str) -> bool:
    if not os.path.exists(urls_file):
        return False
    with open(urls_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                return True
    return False


async def startup_host_check(urls_file: str) -> dict[str, str]:
    grouped, _ = load_url_batches(urls_file)
    statuses: dict[str, str] = {}
    if not grouped:
        print("\n  ⚠ no supported hosts found in batch file")
        return statuses
    print("\n  → checking hosts ...")
    for host in sorted(grouped):
        ok, msg = await check_host(host)
        statuses[host] = f"{'OK' if ok else 'FAIL'} ({msg})"
    host_w = max(len(h) for h in statuses)
    print(f"\n    {'Host':<{host_w}}  Status")
    print(f"    {'-' * host_w}  ------")
    for host, status in sorted(statuses.items()):
        emoji = _status_emoji(status)
        short = status[3:] if status.startswith(
            "OK ") else status[5:] if status.startswith("FAIL ") else status
        print(f"    {host:<{host_w}}  {emoji} {short}")
    return statuses


def deduplicate_family_mirrors(
    grouped: dict[str, list[str]], statuses: dict[str, str]
) -> dict[str, list[str]]:
    """Keep only one reachable host per site family, preferring DOMAIN_ORDER."""
    selected: dict[str, list[str]] = {}
    seen_families: set[str] = set()
    for host in DOMAIN_ORDER:
        if host not in grouped or not grouped[host]:
            continue
        family = SUPPORTED_DOMAINS.get(host)
        if not family or family in seen_families:
            continue
        if statuses.get(host, "").startswith("OK"):
            selected[host] = grouped[host]
            seen_families.add(family)
    # For any family with no reachable host, still keep first host so user sees failure
    for host, urls in grouped.items():
        family = SUPPORTED_DOMAINS.get(host)
        if family and family not in seen_families:
            selected[host] = urls
            seen_families.add(family)
    return selected


async def run_action(action: str, urls_file: str) -> None:
    missing = validate_credentials_for_batch(urls_file)
    if missing:
        print("\n  ✗ missing credentials for:", ", ".join(missing))
        print("  please fill in watchmaker/.env")
        return

    print_batch_summary(urls_file, action=action)
    if not ask_yes_no(f"\n  mark every season as {action.upper()}?"):
        print("  cancelled.")
        return

    report, results = await process_batch(urls_file, action)
    _print_run_summary(report, results)


async def main() -> None:
    setup_logging(verbose=False)

    default_file = DEFAULT_BATCH_FILE
    print("\n→ Add single link / batch from file")
    print("  • Paste URL  → uses that single URL")
    print("  • Enter file  → uses that batch file")
    print(f"  • Press Enter → uses default ({default_file})")
    print("  • Type 0      → exit\n")

    user_input = input(f"Enter [default: {default_file}]: ").strip()
    if user_input == "0":
        print("  exiting.")
        return
    if not user_input:
        user_input = default_file

    if user_input.startswith("http://") or user_input.startswith("https://"):
        Path(DEFAULT_BATCH_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(DEFAULT_BATCH_FILE, "w", encoding="utf-8") as f:
            f.write(user_input + "\n")
        urls_file = DEFAULT_BATCH_FILE
        print(f"  wrote 1 URL → {urls_file}")
    elif os.path.exists(user_input):
        urls_file = user_input
    else:
        print(f"  ✗ file not found: {user_input}")
        return

    # if the batch file is empty, offer to add a URL before showing the menu
    if not _batch_has_urls(urls_file):
        print("\n  batch file is empty.")
        new_url = input("  paste URL (blank=exit): ").strip()
        if not new_url:
            print("  exiting.")
            return
        if new_url.startswith("http://") or new_url.startswith("https://"):
            with open(urls_file, "a", encoding="utf-8") as f:
                f.write(new_url + "\n")
            print(f"  added → {new_url}")
        else:
            print("  not a valid URL.")
            return

    clear_screen()
    print_banner()
    print(f"\n  checking hosts ...")
    host_statuses = await startup_host_check(urls_file)

    while True:
        clear_screen()
        print_banner()
        print_menu(urls_file, host_statuses)

        choice = input("\n  enter number: ").strip()
        if choice == "0":
            print("  exiting.")
            break
        elif choice == "1":
            await run_action("watched", urls_file)
        elif choice == "2":
            await run_action("unwatched", urls_file)
        else:
            print("  invalid option.")

        input("\n  press Enter to return to the menu ...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  interrupted.")
        sys.exit(1)
