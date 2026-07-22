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
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    CREDENTIALS,
    DEFAULT_BATCH_FILE,
    DOMAIN_ORDER,
    FAILED_URLS_FILE,
    HTTP_REQUEST_TIMEOUT,
    LOG_FILE,
    LOGS_DIR,
    SERIES_URLS_EXPORTS,
    SUPPORTED_DOMAINS,
    USER_AGENT,
)

logger = logging.getLogger("watchmaker")

REACHABILITY_TIMEOUT = 8.0
_ANIME_SLUG_RE = re.compile(r"^/anime/stream/([^/?#]+)/?")
_SERIE_SLUG_RE = re.compile(r"^/serie/([^/?#]+)/?")
_STAFFEL_RE = re.compile(r"/staffel-(\d+)")
_FILME_RE = re.compile(r"/filme(?:/|$)")
_ERROR_TITLE_RE = re.compile(
    r"^(?:Error\s+)?(?P<code>\d{3})\b|\b(?:Error|Fehler)\s+(?P<code2>\d{3})\b",
    re.IGNORECASE,
)
_SERVER_ERROR_CODES = {"429", "500", "502", "503", "504"}
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0


def _check_error_page(html: str, family: str) -> str | None:
    """Detect 404/502/etc. pages served with HTTP 200."""
    soup = BeautifulSoup(html, "html.parser")

    # If the page still has real season navigation, it is not an error page.
    if family == "aniworld":
        if soup.select_one("#stream ul li a[href*='/staffel-']") or soup.select_one(
            "#stream ul li a[href*='/filme']"
        ):
            return None
    elif family == "sto":
        if soup.select_one("#season-nav a[data-season-pill]") or soup.select_one(
            '#season-nav a[href*="/staffel-"]'
        ):
            return None
    else:  # bs
        if soup.select_one("#seasons a"):
            return None

    title_tag = soup.find("title")
    if title_tag:
        m = _ERROR_TITLE_RE.search(title_tag.get_text(strip=True))
        if m:
            return m.group("code") or m.group("code2")

    h2 = soup.find("h2")
    if h2 and h2.get_text(strip=True).isdigit():
        code = h2.get_text(strip=True)
        if len(code) == 3:
            return code

    # s.to / aniworld specific 404 body
    p = soup.find("p")
    if p and "nicht gefunden" in p.get_text(strip=True).lower():
        return "404"

    return None


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
def _scheme_for_host(host: str) -> str:
    """Return http for raw IP addresses, https for domains."""
    return "http" if re.match(r"^\d+\.\d+\.\d+\.\d+$", host) else "https"


async def check_host(host: str) -> tuple[bool, str]:
    url = f"{_scheme_for_host(host)}://{host}"
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


def _url_for_host(url: str, new_host: str) -> str | None:
    """Rewrite a URL so it points at new_host, keeping path/query."""
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    new_netloc = new_host
    # Preserve the original scheme if the source URL is already http:// for IPs,
    # otherwise default to the computed scheme for the new host.
    original_is_http = parsed.scheme == "http"
    target_is_ip = bool(re.match(r"^\d+\.\d+\.\d+\.\d+$", new_host))
    scheme = "http" if (original_is_http or target_is_ip) else "https"
    return parsed._replace(scheme=scheme, netloc=new_netloc).geturl()


async def filter_reachable(grouped: dict[str, list[str]]) -> tuple[dict[str, list[str]], dict[str, str]]:
    reachable: dict[str, list[str]] = {}
    statuses: dict[str, str] = {}

    # First pass: check every host we know about
    hosts_to_check = set(grouped) | set(SUPPORTED_DOMAINS)
    for host in sorted(hosts_to_check):
        ok, msg = await check_host(host)
        statuses[host] = f"{'OK' if ok else 'FAIL'} ({msg})"
        if ok:
            logger.info("Reachable %s (%s)", host, msg)
        else:
            logger.warning("Unreachable %s (%s)", host, msg)

    # Second pass: map each input host to a reachable mirror in the same family
    fallback_map: dict[str, str] = {}
    family_representative: dict[str, str] = {}
    for host in DOMAIN_ORDER:
        family = SUPPORTED_DOMAINS.get(host)
        if not family:
            continue
        if statuses.get(host, "").startswith("OK"):
            family_representative.setdefault(family, host)

    for host in sorted(grouped):
        family = SUPPORTED_DOMAINS.get(host)
        if not family:
            continue
        if statuses.get(host, "").startswith("OK"):
            reachable.setdefault(host, []).extend(grouped[host])
            continue

        # Host is down: try to migrate URLs to a reachable family mirror
        alt_host = family_representative.get(family)
        if alt_host and alt_host != host:
            migrated: list[str] = []
            for url in grouped[host]:
                new_url = _url_for_host(url, alt_host)
                if new_url:
                    migrated.append(new_url)
                    logger.info("Migrated %s → %s", url, new_url)
            if migrated:
                reachable.setdefault(alt_host, []).extend(migrated)
                statuses[host] = f"FAIL → {alt_host}"
                logger.warning(
                    "Migrated %d URL(s) from %s to %s",
                    len(migrated), host, alt_host)
            else:
                logger.warning("Unreachable %s — skipping %d URL(s)",
                               host, len(grouped[host]))
        else:
            logger.warning("Unreachable %s — skipping %d URL(s)",
                           host, len(grouped[host]))

    # Deduplicate per host after migration
    for host in reachable:
        reachable[host] = list(dict.fromkeys(reachable[host]))

    return reachable, statuses


async def resolve_active_hosts(
    urls_file: str,
) -> tuple[dict[str, list[str]], dict[str, str], dict[str, str]]:
    """Pick one reachable host per site family in the batch and rewrite URLs to it.

    Only hosts belonging to families actually present in the batch are checked,
    avoiding wasted pings for unused families. Once a family has an active mirror,
    every URL in that family is rewritten to that host and the batch file is updated.

    Returns ``(resolved, statuses, active_host_by_family)`` where
    ``active_host_by_family`` maps each family in the batch to the host that will
    actually be used, so the UI can highlight it.
    """
    grouped, rejected = load_url_batches(urls_file)
    statuses: dict[str, str] = {}

    # Only check hosts for families that appear in the batch.
    families_in_batch: set[str] = {
        SUPPORTED_DOMAINS[host]
        for host in grouped
        if host in SUPPORTED_DOMAINS
    }
    hosts_to_check = [
        host
        for host in DOMAIN_ORDER
        if SUPPORTED_DOMAINS.get(host) in families_in_batch
    ]

    for host in hosts_to_check:
        ok, msg = await check_host(host)
        statuses[host] = f"{'OK' if ok else 'FAIL'} ({msg})"
        if ok:
            logger.info("Reachable %s (%s)", host, msg)
        else:
            logger.warning("Unreachable %s (%s)", host, msg)

    # Pick the first reachable host per family in DOMAIN_ORDER.
    active_host_by_family: dict[str, str] = {}
    for host in DOMAIN_ORDER:
        family = SUPPORTED_DOMAINS.get(host)
        if not family or family in active_host_by_family:
            continue
        if statuses.get(host, "").startswith("OK"):
            active_host_by_family[family] = host

    # Rewrite URLs to the active host of their family.
    resolved: dict[str, list[str]] = {}
    rewritten: list[str] = []
    for host, urls in grouped.items():
        family = SUPPORTED_DOMAINS.get(host)
        if not family:
            resolved.setdefault(host, []).extend(urls)
            continue
        active_host = active_host_by_family.get(family)
        if not active_host:
            logger.warning(
                "No reachable host for family %s — skipping %d URL(s)",
                family, len(urls),
            )
            statuses[host] = f"FAIL (no reachable {family} mirror)"
            continue
        if host == active_host:
            resolved.setdefault(host, []).extend(urls)
        else:
            migrated: list[str] = []
            for url in urls:
                new_url = _url_for_host(url, active_host)
                if new_url:
                    migrated.append(new_url)
                    rewritten.append(f"{url} -> {new_url}")
                    logger.info("Migrated %s -> %s", url, new_url)
                else:
                    logger.warning("Could not rewrite %s to %s",
                                   url, active_host)
            if migrated:
                resolved.setdefault(active_host, []).extend(migrated)
                statuses[host] = f"FAIL -> {active_host}"
                logger.warning(
                    "Migrated %d URL(s) from %s to %s",
                    len(migrated), host, active_host,
                )

    # Deduplicate per host after migration.
    for host in resolved:
        resolved[host] = list(dict.fromkeys(resolved[host]))

    # Persist any rewrites to the batch file permanently.
    if rewritten:
        all_urls: list[str] = []
        seen: set[str] = set()
        for urls in resolved.values():
            for url in urls:
                if url not in seen:
                    seen.add(url)
                    all_urls.append(url)
        Path(urls_file).parent.mkdir(parents=True, exist_ok=True)
        with open(urls_file, "w", encoding="utf-8") as f:
            for url in all_urls:
                f.write(url + "\n")
        print(f"\n  → rewritten {len(rewritten)} URL(s) to active hosts:")
        for line in rewritten:
            print(f"    {line}")
        logger.info("Rewrote %d URL(s) in %s", len(rewritten), urls_file)

    return resolved, statuses, active_host_by_family


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
        self.title: str | None = None
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

    def set_title(self, text: str) -> None:
        title = DomainWorker._extract_title(text, self.family)
        self.title = title

    def line(self) -> str:
        status = "✓" if self.ok and self.watched_episodes == self.total_episodes else "✗"
        display = f"{self.title} ({self.slug})" if self.title else self.slug
        extra = ""
        if self.family != "bs":
            sub = "✓" if self.subscribed else "✗" if self.subscribed is False else "?"
            wl = "✓" if self.watchlist else "✗" if self.watchlist is False else "?"
            extra = f" (Sub:{sub} WL:{wl})"
        return (
            f"{status} {display} [{','.join(self.season_labels)}]: "
            f"{self.watched_episodes}/{self.total_episodes} watched"
            f"{extra}"
        )

    def detail_lines(self, action: str = "") -> list[str]:
        lines = []
        for s in self.seasons:
            label = s.get("season", "?")
            before = s.get("watched_before", 0)
            after = s.get("watched_after", before)
            total = s.get("total", 0)
            planned_after = total if action == "watched" else 0 if action == "unwatched" else after
            if not action or before == planned_after:
                continue
            lines.append(f"    ▶S{label}: {before}/{total} -> {after}/{total}")
        return lines

    def season_summary(self) -> str:
        labels = [str(s.get("season", "?")) for s in self.seasons]
        return f"[{','.join(labels)}]"


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
        last_err: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                r = await self.client.get(url)
            except httpx.RequestError as exc:
                last_err = exc
                if attempt < _MAX_RETRIES:
                    wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning("GET %s %s, retrying in %.1fs (attempt %d/%d)",
                                   url, exc.__class__.__name__, wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                continue

            if r.status_code in (429, 500, 502, 503, 504):
                if attempt < _MAX_RETRIES:
                    wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning("GET %s returned %s, retrying in %.1fs (attempt %d/%d)",
                                   url, r.status_code, wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                last_err = httpx.HTTPStatusError(
                    f"GET {url} returned {r.status_code}", request=r.request, response=r
                )
                continue

            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last_err = exc
                if attempt < _MAX_RETRIES and exc.response.status_code in (429, 500, 502, 503, 504):
                    wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning("GET %s HTTP error %s, retrying in %.1fs (attempt %d/%d)",
                                   url, exc.response.status_code, wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                raise

            err = _check_error_page(r.text, self.family)
            if err:
                raise RuntimeError(f"error page {err} for {url}")
            return r.text

        raise last_err or RuntimeError(
            f"GET {url} failed after {_MAX_RETRIES} attempts")

    async def _post(self, url: str, data: dict | None = None, *, json: dict | None = None, headers: dict | None = None) -> httpx.Response:
        merged = dict(headers) if headers else {}
        last_err: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                r = await self.client.post(url, data=data, json=json, headers=merged)
            except httpx.RequestError as exc:
                last_err = exc
                if attempt < _MAX_RETRIES:
                    wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning("POST %s %s, retrying in %.1fs (attempt %d/%d)",
                                   url, exc.__class__.__name__, wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                continue

            if r.status_code in (429, 500, 502, 503, 504):
                if attempt < _MAX_RETRIES:
                    wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning("POST %s returned %s, retrying in %.1fs (attempt %d/%d)",
                                   url, r.status_code, wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                last_err = httpx.HTTPStatusError(
                    f"POST {url} returned {r.status_code}", request=r.request, response=r
                )
                continue

            try:
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                last_err = exc
                if attempt < _MAX_RETRIES and exc.response.status_code in (429, 500, 502, 503, 504):
                    wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                    logger.warning("POST %s HTTP error %s, retrying in %.1fs (attempt %d/%d)",
                                   url, exc.response.status_code, wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                raise

            return r

        raise last_err or RuntimeError(
            f"POST {url} failed after {_MAX_RETRIES} attempts")

    def _csrf_headers(self, token: str, json: bool = True) -> dict[str, str]:
        h = {
            "X-CSRF-TOKEN": token,
            "X-Requested-With": "XMLHttpRequest",
        }
        if json:
            h["Accept"] = "application/json"
        return h

    @staticmethod
    def _extract_title(text: str, family: str) -> str | None:
        """Extract series title from a series page using scraper-style fallbacks."""
        soup = BeautifulSoup(text, "html.parser")
        if family == "aniworld":
            h1_span = soup.select_one("h1[itemprop='name'] > span")
            if h1_span:
                title = h1_span.get_text(strip=True)
                if title:
                    return title
            h1 = soup.select_one("h1.fw-bold")
            if h1:
                title = h1.get_text(strip=True)
                if title:
                    return title
        else:
            h1 = soup.select_one("h1.fw-bold")
            if h1:
                title = h1.get_text(strip=True)
                if title:
                    return title
        h2 = soup.find("h2")
        if h2:
            title = h2.get_text(strip=True)
            title = re.sub(r"\s*Staffel\s*\d+.*$", "", title)
            if title:
                return title
        return None

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

        base = f"{_scheme_for_host(self.host)}://{self.host}"
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
            # Verify on the series catalogue page, just like the BS scraper,
            # because the homepage may not reliably render the logout link.
            text = await self._get(f"{base}/andere-serien")
            nav = BeautifulSoup(text, "html.parser").select_one(
                "section.navigation")
            if nav is not None and nav.find("a", href="logout") is not None:
                return True
            # Fallback: any exact logout href on the verification page.
            if BeautifulSoup(text, "html.parser").find("a", href="logout") is not None:
                return True
            return False

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

            # Primary selector: the first season list in #stream (matches the Aniworld scraper).
            for a in soup.select("#stream ul:first-of-type li a"):
                href = a.get("href", "")
                if not href:
                    continue
                m = _STAFFEL_RE.search(href)
                if m:
                    seasons.add(int(m.group(1)))
                elif _FILME_RE.search(href):
                    has_movies = True

            # Fallback: scan all links when the primary selector is not available.
            if not seasons:
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/anime/stream/" in href:
                        if "/staffel-" in href:
                            try:
                                seasons.add(
                                    int(href.rsplit("/staffel-", 1)[1].split("/", 1)[0]))
                            except (ValueError, IndexError):
                                continue
                        elif href.rstrip('/').endswith('/filme'):
                            has_movies = True

            # Last resort: numeric data-season-id attributes.
            if not seasons:
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
            # Primary: use the dedicated season navigation container.
            for a in soup.select("#seasons a"):
                href = a.get("href", "").split("?")[0].split("#")[0]
                parts = href.strip("/").split("/")
                if len(parts) >= 3 and parts[0] == "serie":
                    try:
                        seasons.add(int(parts[2]))
                    except (ValueError, IndexError):
                        pass
            # Fallback: season <option> values.
            for opt in soup.find_all("option", value=True):
                if opt["value"].isdigit():
                    seasons.add(int(opt["value"]))
        else:  # sto
            classification = classify_url(url)
            slug = classification[2] if classification else (
                url.split("/serie/", 1)[1].split("/", 1)[0]
            )
            staffel_re = re.compile(
                rf'/serie/{re.escape(slug)}/staffel-(\d+)')
            # Primary: use the dedicated season nav pills.
            for link in soup.select("#season-nav a[data-season-pill]"):
                season_num = link.get("data-season-pill", "")
                if season_num and str(season_num).isdigit():
                    seasons.add(int(season_num))
            # Fallback: href pattern scoped to this series slug.
            for a in soup.find_all("a", href=True):
                m = staffel_re.search(a["href"])
                if m:
                    seasons.add(int(m.group(1)))
            # Last resort: numeric data-season-id on the page.
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
            rows = soup.select(
                "table.seasonEpisodesList tbody tr[data-episode-id]")
            if not rows:
                # Fallback: all episode rows in the first table on the page.
                table = soup.select_one("table.seasonEpisodesList")
                if table:
                    rows = [r for r in table.select(
                        "tbody tr") if r.get("data-episode-id")]
            if not rows:
                # Last resort: any table with data-episode-id rows.
                rows = soup.select("tr[data-episode-id]")
        elif family == "bs":
            rows = soup.select(".episode-table tbody tr.episode-row")
            if not rows:
                rows = soup.select("tr.episode-row")
            if not rows:
                rows = soup.select(".episode-row")
            if not rows:
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
        base = f"{_scheme_for_host(self.host)}://{self.host}"
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
            if r.status_code != 200:
                return False
            try:
                return bool(r.json().get("status"))
            except json.JSONDecodeError:
                return '"status":true' in r.text or '"status" :true' in r.text or "status\":true" in r.text

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
        base = f"{_scheme_for_host(self.host)}://{self.host}"
        classification = classify_url(url)
        slug = classification[2] if classification else (
            url.split("/anime/stream/" if family ==
                      "aniworld" else "/serie/", 1)[1].split("/", 1)[0]
        )
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
        planned_after = total if action == "watched" else 0
        result = {
            "season": season,
            "watched_before": before_watched,
            "watched_after": before_watched,
            "total": total,
            "ok": True,
        }

        # Skip issuing a mark request when the season already matches the target state.
        # We still verify by re-fetching the season page afterwards.
        skip_mark = before_watched == planned_after and total > 0

        try:
            if skip_mark:
                logger.info(
                    "Skipping mark for %s season %s (already %s)",
                    url, season, action,
                )
            elif family == "aniworld":
                soup = BeautifulSoup(text, "html.parser")
                # The season page we just fetched is already scoped to the requested
                # season. Prefer the clear-all control, which carries the correct
                # data-season-id for this exact season.
                season_id = None
                clear_all = soup.find(
                    "span", class_="clearAllEpisodesFromThisSeason")
                if clear_all and clear_all.has_attr("data-season-id"):
                    season_id = clear_all["data-season-id"]
                else:
                    # Fallback: find any element on this season page whose
                    # data-season-id matches the requested season number.
                    for el in soup.find_all(attrs={"data-season-id": True}):
                        try:
                            if int(el["data-season-id"]) == int(season):
                                season_id = el["data-season-id"]
                                break
                        except (ValueError, TypeError):
                            continue
                if not season_id:
                    raise RuntimeError(
                        f"No season-id found for {slug} s{season}")

                series_id = self._extract_series_id(soup)
                if not series_id:
                    raise RuntimeError(
                        f"No series-id found for {slug} s{season}")

                endpoint = f"{base}/ajax/watchseason"
                payload = {
                    "series": series_id,
                    "season": season_id,
                    "watch": "true" if action == "watched" else "false",
                }
                r = await self._post(endpoint, data=payload)
                if r.status_code not in (200, 301, 302):
                    raise RuntimeError(f"mark returned {r.status_code}")
                try:
                    body = r.json()
                    if body.get("status") is not True:
                        raise RuntimeError(f"mark refused: {body}")
                except json.JSONDecodeError:
                    # Some versions return plain text; accept as long as HTTP is OK.
                    pass
            elif family == "bs":
                endpoint = f"{base}/serie/{slug}/{season}/des/{'watch:all' if action == 'watched' else 'unwatch:all'}"
                await self._get(endpoint)
            else:  # sto
                token = self._extract_csrf_token(text)
                ctrl = BeautifulSoup(
                    text, "html.parser").select_one("#season-mark")
                if not ctrl or not ctrl.has_attr("data-mark-url"):
                    raise RuntimeError(
                        f"No #season-mark control for {slug} s{season}")
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
            logger.exception(
                "Failed marking %s season %s: %s", url, season, exc)
            result["ok"] = False
            return result

        try:
            text = await self._get(season_url)
            after_watched, _ = self._count_episodes(text)
            result["watched_after"] = after_watched
            if skip_mark and after_watched != planned_after:
                result["ok"] = False
                logger.error(
                    "Season %s of %s expected to be already %s but "
                    "verification shows %d/%d watched",
                    season, url, action, after_watched, result["total"],
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Could not re-check season %s after mark: %s", season, exc)
        return result

    @staticmethod
    def _extract_series_id(soup: BeautifulSoup) -> str | None:
        container = soup.select_one("div.add-series")
        if container:
            return container.get("data-series-id") or None
        return None

    def _season_url_from_slug(self, url: str, season: int | str) -> str:
        """Build a season page URL from a series URL and season identifier."""
        family = self.family
        base = f"{_scheme_for_host(self.host)}://{self.host}"
        slug = url.split("/anime/stream/" if family ==
                         "aniworld" else "/serie/", 1)[1].split("/", 1)[0]
        if isinstance(season, str) and season.lower() == "filme":
            return f"{base}/anime/stream/{slug}/filme"
        if family == "aniworld":
            return f"{base}/anime/stream/{slug}/staffel-{season}"
        if family == "sto":
            return f"{base}/serie/{slug}/staffel-{season}"
        return f"{base}/serie/{slug}/{season}"

    async def mark_series(self, url: str, action: str) -> SeriesResult:
        if not self.logged_in and not await self.login():
            classification = classify_url(url)
            slug = classification[2] if classification else (
                url.split("/anime/stream/" if self.family ==
                          "aniworld" else "/serie/", 1)[1].split("/", 1)[0]
            )
            result = SeriesResult(self.host, self.family, url, slug)
            result.ok = False
            return result

        family = self.family
        classification = classify_url(url)
        slug = classification[2] if classification else (
            url.split("/anime/stream/" if family ==
                      "aniworld" else "/serie/", 1)[1].split("/", 1)[0]
        )
        result = SeriesResult(self.host, family, url, slug)

        try:
            seasons = await self.discover_seasons(url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Season discovery failed for %s: %s", url, exc)
            result.ok = False
            return result

        # Try to extract the series title once from the series page.
        try:
            series_text = await self._get(url)
            result.set_title(series_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Title extraction failed for %s: %s", url, exc)

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
async def process_batch(
    action: str,
    grouped: dict[str, list[str]],
    statuses: dict[str, str],
    rejected: list[dict],
) -> tuple[dict, list[SeriesResult]]:
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
                print(
                    f"    [{idx:>{num_w}}/{len(urls)}] {short} ...", end=" ", flush=True)
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


def _print_run_summary(stats: dict, results: list[SeriesResult], action: str = "") -> None:
    total = stats["total_urls"]
    ok_count = stats["successful"]
    fail = stats["failed"]
    skipped = stats.get("skipped_hosts", [])
    total_eps = sum(r.total_episodes for r in results)
    watched_eps = sum(r.watched_episodes for r in results)

    print("\n" + "=" * 56)
    print("  RUN SUMMARY")
    print("=" * 56)

    if results:
        term_w = max(shutil.get_terminal_size().columns - 12, 40)
        host_w = min(max(len(r.host) for r in results), term_w // 3)
        series_w = min(max(len(r.name) for r in results), term_w // 3)
        result_w = min(max(len(r.line()) for r in results), term_w // 3)

        def _trunc(text, width):
            return text if len(text) <= width else text[:width - 1] + '…'

        result_w = max(result_w, len("Result"))
        table_w = host_w + series_w + result_w + 6
        sep = '─' * table_w

        print(
            f"    {'Host':<{host_w}}  {'Series':<{series_w}}  {'Result':<{result_w}}")
        print(f"    {'─' * host_w}  {'─' * series_w}  {'─' * result_w}")
        for r in results:
            row = f"    {_trunc(r.host, host_w):<{host_w}}  {_trunc(r.name, series_w):<{series_w}}  {_trunc(r.line(), result_w):<{result_w}}"
            print(row.rstrip())
        print(sep)

    summary_metrics = [
        ("Series processed", str(total)),
        ("Successful", str(ok_count)),
        ("Failed", str(fail)),
        ("Episodes watched", f"{watched_eps}/{total_eps}"),
    ]
    if skipped:
        summary_metrics.append(("Skipped hosts", str(len(skipped))))
    if fail:
        summary_metrics.append(("Failed list", FAILED_URLS_FILE))

    label_w = max(len(m[0]) for m in summary_metrics)
    value_w = max(len(m[1]) for m in summary_metrics)
    for label, value in summary_metrics:
        line = f"    {label:<{label_w}}  {value:<{value_w}}"
        print(line.rstrip())
    print("=" * 56)


def _persist_failed_urls(stats: dict) -> None:
    Path(FAILED_URLS_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(FAILED_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats.get("failed_urls", []),
                  f, indent=2, ensure_ascii=False)
    logger.info("Finished: %d successful, %d failed",
                stats["successful"], stats["failed"])


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f]
    except FileNotFoundError:
        return []


def append_urls_to_scraper_lists(results: list[SeriesResult]) -> None:
    """Append successful URLs to per-family scraper series_urls.txt files."""
    if not results:
        return

    by_family: dict[str, set[str]] = {}
    for result in results:
        if not result.ok or not result.url:
            continue
        by_family.setdefault(result.family, set()).add(result.url)

    for family, urls in by_family.items():
        export_path = SERIES_URLS_EXPORTS.get(family)
        if not export_path:
            continue

        if not os.path.exists(export_path):
            prompt = (
                f"\n  export path for {family} does not exist:\n"
                f"    {export_path}\n"
                f"  disable exporting for this family this run?"
            )
            if ask_yes_no(prompt, default=True):
                logger.info(
                    "Disabled %s export because path is missing", family)
                continue
            else:
                logger.warning(
                    "Proceeding without export target for %s", family)
                continue

        existing = set(_read_lines(export_path))
        existing.discard("")
        new_urls = sorted(urls - existing)
        if not new_urls:
            logger.info("No new URLs to append for %s", family)
            continue

        Path(export_path).parent.mkdir(parents=True, exist_ok=True)
        with open(export_path, "a", encoding="utf-8") as f:
            for url in new_urls:
                f.write(url + "\n")
        logger.info(
            "Appended %d URL(s) to %s scraper list: %s",
            len(new_urls), family, export_path)
        print(f"  appended {len(new_urls)} new URL(s) → {export_path}")


# ==================== UI ====================
def _status_emoji(status: str) -> str:
    if status.startswith("OK"):
        return "✓"
    return "✗"


def clear_screen() -> None:
    """Keep terminal output scrollable; clearing is disabled."""
    pass


def print_banner() -> None:
    print("=" * 56)
    print("  watchmaker  —  batch mark series")
    print("=" * 56)


def print_menu(
    urls_file: str,
    statuses: dict[str, str],
    has_failed: bool,
    active_host_by_family: dict[str, str] | None = None,
) -> None:
    active_hosts = set((active_host_by_family or {}).values())
    print(f"\n  batch file: {urls_file}")
    if not _batch_has_urls(urls_file):
        print("  default batch file is empty.")
        print("  use option 5 to add a URL or switch batch file.")
    print("\n  hosts:")
    if statuses:
        term_w = max(shutil.get_terminal_size().columns - 12, 40)
        host_w = min(max(len(h) for h in statuses), term_w // 2)
        max_status = term_w - host_w - 10

        def _trunc(text, width):
            return text if len(text) <= width else text[:width - 1] + '…'

        state_w = max(len("State"), 1)
        details_w = min(max_status, max(
            len(_trunc(s[3:] if s.startswith("OK ") else s[5:]
                if s.startswith("FAIL ") else s, max_status)) + 2
            for s in statuses.values()
        ))
        details_w = max(details_w, len("Details"))
        table_w = host_w + state_w + details_w + 6
        sep = '─' * table_w

        print(f"    {'Host':<{host_w}}  State{' ' * (state_w - 4)}  Details")
        print(f"    {'─' * host_w}  {'─' * state_w}  {'─' * details_w}")
        for host, status in sorted(statuses.items()):
            emoji = _status_emoji(status)
            short = status[3:] if status.startswith(
                "OK ") else status[5:] if status.startswith("FAIL ") else status
            marker = "  ← ACTIVE" if host in active_hosts else ""
            details = _trunc(f"{short}{marker}", max_status)
            row = f"    {_trunc(host, host_w):<{host_w}}  {emoji:<{state_w}}  {details:<{details_w}}"
            print(row.rstrip())
        print(sep)
    else:
        print("    (no supported URLs)")
    if has_failed:
        print("\n  failed URLs available for retry")
    print("\n  options:")
    print("    1  mark as WATCHED")
    print("    2  mark as UNWATCHED")
    print("    3  export URLs to scraper lists")
    print("    4  retry failed URLs")
    print("    5  add link / change batch")
    print("    6  import URLs from scraper lists")
    print("    0  exit")


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [y/n]: " if default else " [y/n]: "
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
    print_batch_summary_from_grouped(grouped, action=action, rejected=rejected)


def print_batch_summary_from_grouped(
    grouped: dict[str, list[str]],
    action: str = "",
    rejected: list[dict] | None = None,
    header: str = "",
    max_urls_per_host: int = 10,
) -> None:
    total = sum(len(urls) for urls in grouped.values())
    verb = action.lower() if action else "process"
    if header:
        print(f"\n  {header}")
    print(f"\n  → {total} series to {verb}")
    if grouped:
        for host, urls in sorted(grouped.items()):
            family = SUPPORTED_DOMAINS.get(host, "?")
            print(f"      • {host} ({family}): {len(urls)}")
            shown = urls[:max_urls_per_host]
            remaining = len(urls) - max_urls_per_host
            for url in shown:
                print(f"          {url}")
            if remaining > 0:
                print(f"          ... and {remaining} more")
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
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return True
    return False


async def startup_host_check(urls_file: str) -> dict[str, str]:
    grouped, _ = load_url_batches(urls_file)
    statuses: dict[str, str] = {}
    if not grouped:
        return statuses
    for host in sorted(grouped):
        ok, msg = await check_host(host)
        statuses[host] = f"{'OK' if ok else 'FAIL'} ({msg})"
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


async def run_action(
    action: str,
    urls_file: str,
    grouped: dict[str, list[str]],
    statuses: dict[str, str],
) -> None:
    missing = validate_credentials_for_batch(urls_file)
    if missing:
        print("\n  ✗ missing credentials for:", ", ".join(missing))
        print("  please fill in watchmaker/.env")
        return

    print_batch_summary_from_grouped(grouped, action=action)

    print("\n  → preview before marking:")
    print(f"  action: {action}")
    print()
    preview_results: list[SeriesResult] = []
    already_done_count = 0
    for host, urls in sorted(grouped.items()):
        async with DomainWorker(host) as worker:
            if not await worker.login():
                print(f"  ✗ could not log in to {host} — skipping")
                continue
            for url in urls:
                try:
                    seasons = await worker.discover_seasons(url)
                    classification = classify_url(url)
                    slug = classification[2] if classification else (
                        url.split("/anime/stream/" if worker.family ==
                                  "aniworld" else "/serie/", 1)[1].split("/", 1)[0]
                    )
                    result = SeriesResult(host, worker.family, url, slug)
                    try:
                        series_text = await worker._get(url)
                        result.set_title(series_text)
                        if worker.family in ("aniworld", "sto"):
                            result.subscribed, result.watchlist = worker._detect_subscription_status(
                                series_text)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Preview title extraction failed for %s: %s", url, exc)
                    needs_sub_change = (
                        action == "watched"
                        and worker.family in ("aniworld", "sto")
                        and result.subscribed is False
                    )
                    all_already = True
                    for season in seasons:
                        season_url = worker._season_url_from_slug(url, season)
                        before_watched, total = worker._count_episodes(await worker._get(season_url))
                        planned_after = total if action == "watched" else 0
                        result.seasons.append({
                            "season": season,
                            "watched_before": before_watched,
                            "watched_after": planned_after,
                            "total": total,
                            "ok": True,
                        })
                        if before_watched != planned_after:
                            all_already = False
                    status_extra = ""
                    if worker.family in ("aniworld", "sto"):
                        sub = "✓" if result.subscribed else "✗" if result.subscribed is False else "?"
                        wl = "✓" if result.watchlist else "✗" if result.watchlist is False else "?"
                        status_extra = f" (Sub:{sub} WL:{wl})"
                    sub_badge = " ⚡" if needs_sub_change else ""
                    before_eps = sum(
                        s.get("watched_before", 0) for s in result.seasons)
                    planned_eps = sum(
                        s.get("watched_after", s.get("watched_before", 0))
                        for s in result.seasons
                    )
                    seasons_tag = result.season_summary()
                    counter = f"{before_eps}/{result.total_episodes}"
                    if before_eps != planned_eps:
                        counter += f" → {planned_eps}/{result.total_episodes}"
                    if all_already and not needs_sub_change:
                        already_done_count += 1
                        print(
                            f"  {host}: {result.title or slug}{status_extra}{sub_badge} {seasons_tag} — {counter}")
                    else:
                        preview_results.append(result)
                        print(
                            f"  {host}: {result.title or slug}{status_extra}{sub_badge} {seasons_tag} — {counter}")
                        for line in result.detail_lines(action):
                            print(f"      {line.strip()}")
                except Exception as exc:
                    print(f"  ✗ preview failed for {url}: {exc}")
                    continue

    if not preview_results:
        print(
            f"\n  → nothing to do; all {already_done_count} series already at target state ({action}).")
        return

    if not ask_yes_no("\n  proceed with marking?", default=False):
        print("  marking cancelled.")
        return

    _, rejected = load_url_batches(urls_file)
    report, results = await process_batch(action, grouped, statuses, rejected)
    _print_run_summary(report, results, action=action)


async def import_urls(urls_file: str) -> None:
    """Manually import URLs from scraper lists into the batch file."""
    by_family: dict[str, list[str]] = {}
    missing_paths: list[tuple[str, str | None]] = []

    for family, import_path in SERIES_URLS_EXPORTS.items():
        if not import_path:
            continue
        if not os.path.exists(import_path):
            missing_paths.append((family, import_path))
            continue

        seen: set[str] = set()
        for raw in _read_lines(import_path):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            classification = classify_url(line)
            if classification is None:
                continue
            host, url_family, slug = classification
            if url_family != family or not slug:
                continue
            url = line.split("#", 1)[0].rstrip()
            if url in seen:
                continue
            seen.add(url)
            by_family.setdefault(family, []).append(url)

    total = sum(len(urls) for urls in by_family.values())
    if not by_family and missing_paths:
        print("\n  → 0 series available to import")
        for family, path in missing_paths:
            print(f"  {family} scraper list not found: {path}")
        print("  no supported URLs to import.")
        return
    if not by_family:
        print("\n  → 0 series available to import")
        print("  no supported URLs to import.")
        return

    print("\n  → import URLs from scraper lists")
    print(f"  total series found: {total}")
    print("\n  per family:")
    for family in sorted(by_family):
        print(f"    {family} ({len(by_family[family])})")
    if missing_paths:
        print("\n  missing scraper lists:")
        for family, path in missing_paths:
            print(f"    {family}: {path}")

    print("\n  URLs:")
    for family in sorted(by_family):
        print(f"\n  [{family}]")
        for idx, url in enumerate(by_family[family], 1):
            print(f"    {idx}. {url}")

    if not ask_yes_no("\n  proceed with import?"):
        print("  import cancelled.")
        return

    # Load current batch so we can deduplicate against it.
    grouped, _ = load_url_batches(urls_file)
    existing: set[str] = set()
    for urls in grouped.values():
        existing.update(urls)

    added_by_family: dict[str, int] = {}
    new_urls_total: list[str] = []
    for family in sorted(by_family):
        new_urls = [u for u in by_family[family] if u not in existing]
        if not new_urls:
            continue
        added_by_family[family] = len(new_urls)
        new_urls_total.extend(new_urls)
        existing.update(new_urls)

    if not new_urls_total:
        print("\n  no new URLs to import (all already in batch).")
        return

    Path(urls_file).parent.mkdir(parents=True, exist_ok=True)
    with open(urls_file, "a", encoding="utf-8") as f:
        for url in new_urls_total:
            f.write(url + "\n")

    print(f"\n  appended {len(new_urls_total)} new URL(s) → {urls_file}")
    for family, count in sorted(added_by_family.items()):
        print(f"    {family}: {count}")
    logger.info("Imported %d URL(s) from scraper lists into %s",
                len(new_urls_total), urls_file)


async def export_urls(urls_file: str) -> None:
    """Manually export URLs from the batch file to scraper lists."""
    grouped, rejected = load_url_batches(urls_file)
    by_family: dict[str, list[str]] = {}
    for host, urls in grouped.items():
        family = SUPPORTED_DOMAINS.get(host)
        if family:
            by_family.setdefault(family, []).extend(urls)

    total = sum(len(urls) for urls in by_family.values())
    if not by_family:
        print("\n  → 0 series available to export")
        print("  no supported URLs to export.")
        return

    print("\n  → export URLs to scraper lists")
    print(f"  total series: {total}")
    print("\n  per family:")
    for family in sorted(by_family):
        export_path = SERIES_URLS_EXPORTS.get(family)
        target = export_path or "disabled"
        print(f"    {family} ({len(by_family[family])}) → {target}")

    print("\n  URLs:")
    for family in sorted(by_family):
        print(f"\n  [{family}]")
        for idx, url in enumerate(by_family[family], 1):
            print(f"    {idx}. {url}")

    if not ask_yes_no("\n  proceed with export?"):
        print("  export cancelled.")
        return

    # Build fake results from the batch file so we can reuse append logic
    class _FakeResult:
        def __init__(self, family: str, url: str):
            self.family = family
            self.url = url
            self.ok = True

    results: list = []
    for family, urls in by_family.items():
        for url in urls:
            results.append(_FakeResult(family, url))

    append_urls_to_scraper_lists(results)


def _load_failed_urls() -> list[str]:
    if not os.path.exists(FAILED_URLS_FILE):
        return []
    try:
        with open(FAILED_URLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(u) for u in data if u]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read failed URLs: %s", exc)
    return []


def _has_failed_urls() -> bool:
    return bool(_load_failed_urls())


async def retry_failed_urls(urls_file: str) -> str:
    """Load failed URLs from JSON, write them to a temp file, and return it."""
    failed = _load_failed_urls()
    if not failed:
        print("\n  no failed URLs to retry.")
        return urls_file

    print(f"\n  → {len(failed)} failed URL(s) loaded")
    for idx, url in enumerate(failed, 1):
        print(f"    {idx}. {url}")

    if not ask_yes_no("\n  replace current batch with these failed URLs?"):
        print("  retry cancelled.")
        return urls_file

    Path(urls_file).parent.mkdir(parents=True, exist_ok=True)
    with open(urls_file, "w", encoding="utf-8") as f:
        for url in failed:
            f.write(url + "\n")
    print(f"  wrote {len(failed)} failed URL(s) → {urls_file}")
    logger.info("Wrote %d failed URL(s) to %s", len(failed), urls_file)

    print("\n  → checking hosts ...")
    return urls_file


async def _detect_and_add_input(urls_file: str) -> str:
    """Scraper-style input: detect URL, existing file path, or file name."""
    print("\n  add link / change batch")
    print(f"  current file: {urls_file}")
    print("  • Paste URL      → writes single URL to default batch")
    print("  • Enter path     → switches to that batch file")
    print("  • Press Enter    → cancel\n")

    user_input = input("  input: ").strip()
    if not user_input:
        print("  cancelled.")
        return urls_file

    if user_input.startswith("http://") or user_input.startswith("https://"):
        Path(DEFAULT_BATCH_FILE).parent.mkdir(parents=True, exist_ok=True)
        if DEFAULT_BATCH_FILE != urls_file and _batch_has_urls(DEFAULT_BATCH_FILE):
            if not ask_yes_no(f"  overwrite {DEFAULT_BATCH_FILE}?", default=False):
                print("  cancelled.")
                return urls_file
        with open(DEFAULT_BATCH_FILE, "w", encoding="utf-8") as f:
            f.write(user_input + "\n")
        print(f"  wrote 1 URL → {DEFAULT_BATCH_FILE}")
        return DEFAULT_BATCH_FILE

    candidate = user_input
    if not os.path.exists(candidate):
        candidate = os.path.join(
            os.path.dirname(DEFAULT_BATCH_FILE), user_input)
    if not os.path.exists(candidate):
        print(f"  ✗ file not found: {user_input}")
        return urls_file
    print(f"  loaded batch file → {candidate}")
    return candidate


async def main() -> None:
    setup_logging(verbose=False)

    urls_file = DEFAULT_BATCH_FILE
    Path(urls_file).parent.mkdir(parents=True, exist_ok=True)

    # Show batch overview before resolving hosts.
    initial_grouped, initial_rejected = load_url_batches(urls_file)
    print_banner()
    print_batch_summary_from_grouped(
        initial_grouped,
        header="loaded batch",
        rejected=initial_rejected,
    )

    # Single sticky host resolution at startup.
    resolved, host_statuses, active_host_by_family = await resolve_active_hosts(urls_file)

    while True:
        print_banner()
        print_menu(urls_file, host_statuses,
                   _has_failed_urls(), active_host_by_family)

        choice = input("\n  enter number: ").strip()
        if choice == "0":
            print("  exiting.")
            break
        elif choice == "1":
            await run_action("watched", urls_file, resolved, host_statuses)
        elif choice == "2":
            await run_action("unwatched", urls_file, resolved, host_statuses)
        elif choice == "3":
            await export_urls(urls_file)
        elif choice == "4":
            urls_file = await retry_failed_urls(urls_file)
            print("\n  → refreshing host resolution ...")
            resolved, host_statuses, active_host_by_family = await resolve_active_hosts(urls_file)
        elif choice == "5":
            clear_screen()
            urls_file = await _detect_and_add_input(urls_file)
            print("\n  → refreshing host resolution ...")
            resolved, host_statuses, active_host_by_family = await resolve_active_hosts(urls_file)
        elif choice == "6":
            await import_urls(urls_file)
            print("\n  → refreshing host resolution ...")
            resolved, host_statuses, active_host_by_family = await resolve_active_hosts(urls_file)
        else:
            print("  invalid option.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  interrupted.")
        sys.exit(1)
