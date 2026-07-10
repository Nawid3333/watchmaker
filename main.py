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


class FileLock:
    """Cross-platform PID-based lock file using a plain file."""

    def __init__(self, lock_path: str, timeout: float = 30.0, stale_seconds: float = 300.0):
        self.lock_path = lock_path
        self.timeout = timeout
        self.stale_seconds = stale_seconds
        self._owned = False

    def _read_lock_pid(self) -> int | None:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                pid_str = f.read(32).strip()
            return int(pid_str) if pid_str else None
        except (FileNotFoundError, ValueError, PermissionError):
            return None

    def _is_stale(self) -> bool:
        try:
            mtime = os.path.getmtime(self.lock_path)
            return time.time() - mtime > self.stale_seconds
        except FileNotFoundError:
            return True

    def _try_acquire(self) -> bool:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            self._owned = True
            return True
        except FileExistsError:
            return False

    def _break_stale(self) -> bool:
        if not self._is_stale():
            return False
        try:
            os.remove(self.lock_path)
            return self._try_acquire()
        except (FileNotFoundError, PermissionError):
            return self._try_acquire()

    def acquire(self) -> bool:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if self._try_acquire():
                return True
            if self._break_stale():
                return True
            time.sleep(0.1)
        return False

    def release(self) -> None:
        if not self._owned:
            return
        try:
            pid = self._read_lock_pid()
            if pid is None or pid == os.getpid():
                os.remove(self.lock_path)
        except (FileNotFoundError, PermissionError):
            pass
        finally:
            self._owned = False

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise TimeoutError(f"Could not acquire lock: {self.lock_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


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

    # First pass: only check hosts actually used in the batch, plus any
    # not-in-input-but-supported host would only slow things down and can
    # confuse users with unrelated unreachable warnings.
    hosts_to_check = set(grouped)
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
        sub = "✓" if self.subscribed else "✗" if self.subscribed is False else "?"
        wl = "✓" if self.watchlist else "✗" if self.watchlist is False else "?"
        status = "✓" if self.ok and self.watched_episodes == self.total_episodes else "✗"
        display = f"{self.title} ({self.slug})" if self.title else self.slug
        return (
            f"{status} {display} [{','.join(self.season_labels)}]: "
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
            # Verify login by checking whether the login form is gone.
            # Some BS mirrors (e.g. bs.cine.to) do not render the same
            # section.navigation logout link that bs.to does.
            text = await self._get(login_url)
            soup = BeautifulSoup(text, "html.parser")
            has_login_form = (
                soup.find("form", action=lambda v: v and "login" in v)
                or soup.find("input", {"name": "login[user]"})
                or soup.find("input", {"name": "security_token"})
            )
            if not has_login_form:
                return True
            # Fallback: classic navigation logout link. Scan all links
            # because on mirrors like bs.cine.to the first link is not logout.
            nav = soup.select_one("section.navigation")
            if nav is not None:
                for link in nav.find_all("a", href=True):
                    if "logout" in (link.get("href") or ""):
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
        result = {
            "season": season,
            "watched_before": before_watched,
            "watched_after": before_watched,
            "total": total,
            "ok": True,
        }

        try:
            if family == "aniworld":
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
                r = await self._get(endpoint)
                if r.status_code != 200:
                    raise RuntimeError(f"bs mark returned {r.status_code}")
                err = _check_error_page(r.text, family)
                if err:
                    raise RuntimeError(f"bs mark returned error page {err}")
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
                print(
                    f"    [{idx:>{num_w}}/{len(urls)}] {short} ...", end=" ", flush=True)
                result = await worker.mark_series(url, action)
                results.append(result)
                if result.ok:
                    stats["successful"] += 1
                else:
                    stats["failed"] += 1
                    stats["failed_urls"].append(url)
                    _append_failed_url(url)
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
        print("    current status | after marking")
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
    """Write the final consolidated failed-URL list (idempotent after per-URL appends)."""
    failed = sorted(set(stats.get("failed_urls", [])))
    Path(FAILED_URLS_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(FAILED_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(failed, f, indent=2, ensure_ascii=False)
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
        # Serialize concurrent appends to the same scraper list across processes.
        # This uses a PID-based lock file so it works on both Windows and POSIX.
        lock_path = export_path + ".watchmaker.lock"
        lock = FileLock(lock_path)
        with lock:
            # Re-read under the lock to avoid duplicates from racing writers.
            live_existing = set(_read_lines(export_path))
            live_existing.discard("")
            live_new_urls = sorted(urls - live_existing)
            if not live_new_urls:
                logger.info("No new URLs to append for %s", family)
                continue
            with open(export_path, "a", encoding="utf-8") as f:
                for url in live_new_urls:
                    f.write(url + "\n")
            logger.info(
                "Appended %d URL(s) to %s scraper list: %s",
                len(live_new_urls), family, export_path)
            print(
                f"  appended {len(live_new_urls)} new URL(s) → {export_path}")


# ==================== UI ====================
def _status_emoji(status: str) -> str:
    if status.startswith("OK"):
        return "✓"
    return "✗"


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _pause_before_clear(prompt: str = "  press Enter to continue...") -> None:
    """Wait for the user so they can read the previous output before clearing."""
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        pass


def print_banner() -> None:
    print("=" * 56)
    print("  watchmaker  —  batch mark series")
    print("=" * 56)


def print_menu(urls_file: str, statuses: dict[str, str], has_failed: bool) -> None:
    print(f"\n  batch file: {urls_file}")
    if not _batch_has_urls(urls_file):
        print("  default batch file is empty.")
        print("  use option 6 to add a URL or switch batch file.")
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
    if has_failed:
        print("\n  failed URLs available for retry")
    print("\n  options:")
    print("    1  mark as WATCHED")
    print("    2  mark as UNWATCHED")
    print("    3  export URLs to scraper lists")
    print("    4  rewrite URLs to reachable hosts")
    print("    5  retry failed URLs")
    print("    6  add / change batch")
    print("    7  import URLs from scraper lists")
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
    # For any family with no reachable host, still keep the first host so the
    # user sees failure; if we already selected a reachable host, merge any
    # remaining same-family URLs so duplicate mirror URLs are not lost.
    for host, urls in grouped.items():
        family = SUPPORTED_DOMAINS.get(host)
        if not family:
            continue
        if family not in seen_families:
            selected[host] = urls
            seen_families.add(family)
        elif host in selected:
            # already selected this host above
            pass
        else:
            # Same family already represented by another host; merge URLs while
            # preserving order and deduplicating within the selected host.
            representative_host = next(
                (h for h, f in ((h, SUPPORTED_DOMAINS.get(h)) for h in DOMAIN_ORDER)
                 if f == family and h in selected), None
            )
            if representative_host:
                selected[representative_host] = list(
                    dict.fromkeys(selected[representative_host] + urls)
                )
    return selected


async def run_action(action: str, urls_file: str) -> None:
    missing = validate_credentials_for_batch(urls_file)
    if missing:
        print("\n  ✗ missing credentials for:", ", ".join(missing))
        print("  please fill in watchmaker/.env")
        return

    print_batch_summary(urls_file, action=action)

    grouped, _ = load_url_batches(urls_file)
    print("\n  → preview before marking:")
    print(f"  action: {action}")
    print()
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
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Preview title extraction failed for %s: %s", url, exc)
                    for season in seasons:
                        season_url = worker._season_url_from_slug(url, season)
                        before_watched, total = worker._count_episodes(await worker._get(season_url))
                        result.seasons.append({
                            "season": season,
                            "watched_before": before_watched,
                            "watched_after": before_watched,
                            "total": total,
                            "ok": True,
                        })
                    print(f"  {host}: {result.title or slug}")
                    print("      current status | preview")
                    for line in result.detail_lines():
                        print(f"      {line.strip()}")
                except Exception as exc:
                    print(f"  ✗ preview failed for {url}: {exc}")
                    continue

    if not ask_yes_no("\n  proceed with marking?", default=False):
        print("  marking cancelled.")
        return

    report, results = await process_batch(urls_file, action)
    _print_run_summary(report, results)


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


def _append_failed_url(url: str) -> None:
    """Persist a failed URL immediately so crashes don't lose it."""
    existing = set(_load_failed_urls())
    if url in existing:
        return
    existing.add(url)
    Path(FAILED_URLS_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(FAILED_URLS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(existing), f, indent=2, ensure_ascii=False)
    logger.warning("Recorded failed URL: %s", url)


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


async def rewrite_urls_to_reachable(urls_file: str) -> None:
    """Rewrite URLs in the batch file to the first reachable host per family."""
    grouped, rejected = load_url_batches(urls_file)
    if not grouped:
        print("\n  no supported URLs to rewrite.")
        return

    # Check every known host so we can pick a reachable representative
    statuses: dict[str, str] = {}
    for host in sorted(SUPPORTED_DOMAINS):
        ok, msg = await check_host(host)
        statuses[host] = f"{'OK' if ok else 'FAIL'} ({msg})"

    family_representative: dict[str, str] = {}
    for host in DOMAIN_ORDER:
        family = SUPPORTED_DOMAINS.get(host)
        if not family:
            continue
        if statuses.get(host, "").startswith("OK"):
            family_representative.setdefault(family, host)

    rewrites: list[tuple[str, str]] = []
    unchanged: list[str] = []
    for host, urls in grouped.items():
        family = SUPPORTED_DOMAINS.get(host)
        if not family:
            unchanged.extend(urls)
            continue
        target_host = family_representative.get(family, host)
        if target_host == host:
            unchanged.extend(urls)
            continue
        for url in urls:
            new_url = _url_for_host(url, target_host)
            if new_url:
                rewrites.append((url, new_url))
            else:
                unchanged.append(url)

    if not rewrites:
        print("\n  all URLs already point to reachable hosts.")
        return

    print(f"\n  → {len(rewrites)} URL(s) will be rewritten:")
    for old, new in rewrites:
        print(f"    {old} → {new}")
    if unchanged:
        print(f"\n  {len(unchanged)} URL(s) unchanged")

    if not ask_yes_no("\n  save rewritten URLs to batch file?"):
        print("  rewrite cancelled.")
        return

    all_urls = [new for _, new in rewrites] + unchanged
    all_urls = list(dict.fromkeys(all_urls))
    with open(urls_file, "w", encoding="utf-8") as f:
        for url in all_urls:
            f.write(url + "\n")
    print(f"  saved {len(all_urls)} URL(s) → {urls_file}")
    logger.info("Rewrote %d URL(s) in %s", len(rewrites), urls_file)


async def _detect_and_add_input(urls_file: str) -> str:
    """Scraper-style input: detect URL, existing file path, or file name."""
    print("\n  add / change batch")
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
        target = DEFAULT_BATCH_FILE
        existing_lines = _read_lines(target)
        existing_urls = set(line.strip() for line in existing_lines if line.strip(
        ) and not line.strip().startswith("#"))

        if target != urls_file and _batch_has_urls(target):
            if not ask_yes_no(f"  add URL to existing {target}?", default=True):
                print("  cancelled.")
                return urls_file

        new_lines = [line for line in existing_lines if line.strip()]
        if user_input not in existing_urls:
            new_lines.append(user_input)

        with open(target, "w", encoding="utf-8") as f:
            for line in new_lines:
                f.write(line + "\n")
        print(
            f"  wrote 1 new URL → {target} (kept {len(existing_urls)} existing)")
        return target

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

    while True:
        clear_screen()

        host_statuses = await startup_host_check(urls_file)

        print_banner()
        print_menu(urls_file, host_statuses, _has_failed_urls())

        choice = input("\n  enter number: ").strip()
        if choice == "0":
            print("  exiting.")
            break
        elif choice == "1":
            await run_action("watched", urls_file)
            _pause_before_clear("  press Enter to return to menu...")
            host_statuses = await startup_host_check(urls_file)
        elif choice == "2":
            await run_action("unwatched", urls_file)
            _pause_before_clear("  press Enter to return to menu...")
            host_statuses = await startup_host_check(urls_file)
        elif choice == "3":
            await export_urls(urls_file)
        elif choice == "4":
            await rewrite_urls_to_reachable(urls_file)
            host_statuses = await startup_host_check(urls_file)
        elif choice == "5":
            urls_file = await retry_failed_urls(urls_file)
            host_statuses = await startup_host_check(urls_file)
        elif choice == "6":
            clear_screen()
            urls_file = await _detect_and_add_input(urls_file)
            host_statuses = await startup_host_check(urls_file)
        elif choice == "7":
            await import_urls(urls_file)
            host_statuses = await startup_host_check(urls_file)
        else:
            print("  invalid option.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  interrupted.")
        sys.exit(1)
