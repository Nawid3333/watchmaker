"""
watchmaker

Batch mark whole series as watched or unwatched on aniworld.to, bs.to family,
and s.to family streaming sites.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
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
# Keep importing this module silent; setup_logging() installs the real handlers.
logger.addHandler(logging.NullHandler())

# ==================== CONSTANTS ====================
REACHABILITY_TIMEOUT = 8.0
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0
_MAX_BACKOFF = 30.0
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_OK_POST_STATUS = frozenset({200, 201, 204, 301, 302})

ACTION_WATCHED = "watched"
ACTION_UNWATCHED = "unwatched"

# Families that expose a subscribe / watchlist control. bs.to has none.
SUBSCRIBE_FAMILIES = frozenset({"aniworld", "sto"})

# Pseudo-season used by aniworld for the /filme page.
MOVIES_SEASON = "Filme"

_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_ANIME_SLUG_RE = re.compile(r"^/anime/stream/([^/?#]+)/?")
_SERIE_SLUG_RE = re.compile(r"^/serie/([^/?#]+)/?")
_STAFFEL_RE = re.compile(r"/staffel-(\d+)")
_FILME_RE = re.compile(r"/filme(?:/|$)")
_ERROR_TITLE_RE = re.compile(
    r"^(?:Error\s+)?(?P<code>\d{3})\b|\b(?:Error|Fehler)\s+(?P<code2>\d{3})\b",
    re.IGNORECASE,
)

# Row-level markers that mean "this episode is watched".
_WATCHED_CLASS_TOKENS = frozenset({"seen", "watched"})
_WATCHED_ATTRS = ("data-watched", "data-seen")
_TRUTHY = frozenset({"1", "true", "yes"})

# Selectors that prove a page really is a series page (used to rule out
# error pages that are served with HTTP 200).
_SEASON_NAV_SELECTORS: dict[str, tuple[str, ...]] = {
    "aniworld": (
        "#stream ul li a[href*='/staffel-']",
        "#stream ul li a[href*='/filme']",
    ),
    "sto": (
        "#season-nav a[data-season-pill]",
        '#season-nav a[href*="/staffel-"]',
    ),
    "bs": ("#seasons a",),
}

# Selectors that prove the session is authenticated, per family.
_LOGIN_MARKERS: dict[str, str] = {
    "aniworld": "div.avatar a[href*='/user/profil/']",
    "sto": "form[action='/logout']",
    "bs": "section.navigation a[href='logout']",
}


class ControlMissingError(RuntimeError):
    """A control the marking step depends on is absent from the page.

    Usually means the session expired and we were served a logged-out page,
    so the caller may re-authenticate and try once more.
    """


# ==================== SMALL HELPERS ====================
def _attr_str(value: object) -> str | None:
    """Return a BeautifulSoup attribute value if it is a plain string."""
    return value if isinstance(value, str) else None


def _attr_int(value: object) -> int | None:
    """Return an int parsed from a BeautifulSoup attribute value."""
    if isinstance(value, (str, int)):
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    return None


# lxml parses these sites' pages ~1.2x faster than the stdlib parser. Verified
# identical, not assumed: every parser this module runs (error page, title,
# CSRF, series id, login state, episode counts, subscription state, season
# discovery) was compared across 165 real captured pages from all three site
# families with zero mismatches. The fallback keeps watchmaker working where
# lxml was never installed, at the old speed.
try:
    import lxml  # noqa: F401

    _HTML_PARSER = "lxml"
except ImportError:  # pragma: no cover - depends on the install
    _HTML_PARSER = "html.parser"


def _soup(text: str) -> BeautifulSoup:
    return BeautifulSoup(text, _HTML_PARSER)


def _is_truthy_attr(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


_TITLE_SEASON_RE = re.compile(r"\s*\b(?:Staffel|Season|St\.)\s*\d+.*$", re.IGNORECASE)
_TITLE_SPECIALS_RE = re.compile(r"\s*\bSpecials\s*$", re.IGNORECASE)
_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-–|]\s*(?:Burning\s*Series|AniWorld|Serienstream|s\.to|bs\.to).*$",
    re.IGNORECASE,
)
_TITLE_SEASON_PAREN_RE = re.compile(r"\s*\(\d+\)\s*$")

# Catalogue/overview pages the sites serve with HTTP 200 when a series slug is
# wrong or retired. Their heading looks exactly like a series title, so without
# this list a dead URL silently previews as a real series named "Alle Serien".
_UTILITY_PAGE_TITLES = frozenset(
    {
        "alle serien",
        "andere serien",
        "beliebte serien",
        "neue serien",
        "alle animes",
        "andere animes",
        "empfehlung",
        "meistgesehen",
    }
)


def _heading_text(el) -> str:
    """Flatten a heading to text, keeping inline children as separate words.

    bs.to renders "<h2>Harry Potter<small>Specials</small></h2>". Plain
    get_text(strip=True) glues those into "Harry PotterSpecials"; passing a
    separator keeps them apart without mutating the shared soup.
    """
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def _clean_title(text: str) -> str | None:
    """Strip season markers and site-name suffixes from a raw title string."""
    title = _TITLE_SUFFIX_RE.sub("", text or "").strip()
    title = _TITLE_SEASON_RE.sub("", title).strip()
    title = _TITLE_SPECIALS_RE.sub("", title).strip()
    title = _TITLE_SEASON_PAREN_RE.sub("", title).strip()
    return title or None


def is_utility_page_title(title: str | None) -> bool:
    """True when a "series" title is really a catalogue page heading."""
    return bool(title) and title.strip().lower() in _UTILITY_PAGE_TITLES


def _json_body(response: httpx.Response) -> dict | None:
    """Return a JSON object body, or None when the site answered in plain text.

    None means "no verdict available", so callers must not treat it as a
    refusal — some mirrors answer these endpoints with an empty body.
    """
    if not response.text.strip():
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _check_error_page(soup: BeautifulSoup, family: str) -> str | None:
    """Detect 404/502/etc. pages served with HTTP 200."""
    # If the page still has real season navigation, it is not an error page.
    for selector in _SEASON_NAV_SELECTORS.get(family, ()):
        if soup.select_one(selector):
            return None

    title_tag = soup.find("title")
    if title_tag:
        m = _ERROR_TITLE_RE.search(title_tag.get_text(strip=True))
        if m:
            return m.group("code") or m.group("code2")

    h2 = soup.find("h2")
    if h2:
        code = h2.get_text(strip=True)
        if code.isdigit() and len(code) == 3:
            return code

    # s.to / aniworld specific 404 body
    p = soup.find("p")
    if p and "nicht gefunden" in p.get_text(strip=True).lower():
        return "404"

    return None


def _configure_console() -> None:
    """Make the arrow/box-drawing output safe on any code page.

    A redirected pipe or a legacy Windows code page defaults to cp1252, which
    cannot encode "→" or "─" — printing the very first summary line would kill
    the run with a UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        # sys.stdout/stderr are typed as typing.TextIO, which does not declare
        # reconfigure() even though the real runtime object (TextIOWrapper)
        # has it; cast so this passes static checking without a type: ignore.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")


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


def _scheme_for_host(host: str) -> str:
    """Return http for raw IP addresses, https for domains."""
    return "http" if _IP_RE.match(host) else "https"


def base_url(host: str) -> str:
    return f"{_scheme_for_host(host)}://{host}"


def classify_url(url: str) -> tuple[str, str, str] | None:
    """Return (host, family, slug) for a supported series URL, else None."""
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    host = _normalize_host(parsed.netloc)
    family = SUPPORTED_DOMAINS.get(host)
    if not family:
        return None

    path = parsed.path or "/"
    m = _ANIME_SLUG_RE.match(path) if family == "aniworld" else _SERIE_SLUG_RE.match(path)
    if not m or not m.group(1):
        return None
    return (host, family, m.group(1))


def slug_for(url: str, family: str) -> str:
    """Return the series slug of a URL.

    Falls back to a raw path split so callers never have to repeat the
    ``classify_url() or split()`` dance; raises if neither works, which is
    better than silently marking the wrong series.
    """
    classification = classify_url(url)
    if classification:
        return classification[2]
    marker = "/anime/stream/" if family == "aniworld" else "/serie/"
    try:
        return urlparse(url).path.split(marker, 1)[1].split("/", 1)[0]
    except IndexError as exc:
        raise ValueError(f"Cannot extract series slug from {url!r}") from exc


def _url_for_host(url: str, new_host: str) -> str | None:
    """Rewrite a URL so it points at new_host, keeping path/query."""
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    # The scheme follows the *target* host: IP mirrors are http-only, domains
    # are https. Carrying the source scheme over would downgrade a domain
    # mirror to http just because the batch came from an IP mirror.
    return parsed._replace(scheme=_scheme_for_host(new_host), netloc=new_host).geturl()


def load_url_batches(source: str) -> tuple[dict[str, list[str]], list[dict]]:
    """Group batch-file URLs by host, in DOMAIN_ORDER, dropping duplicates.

    Two URLs for the same series (e.g. ``/serie/x`` and ``/serie/x/staffel-3``)
    mark exactly the same thing, so they are collapsed to the first occurrence
    instead of being fetched and marked twice.
    """
    grouped: dict[str, list[str]] = {host: [] for host in SUPPORTED_DOMAINS}
    rejected: list[dict] = []
    seen_series: set[tuple[str, str]] = set()
    duplicates = 0

    for raw in _read_lines(source):
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
        key = (host, slug.lower())
        if key in seen_series:
            duplicates += 1
            logger.info("Skipping duplicate series URL %s", line)
            continue
        seen_series.add(key)
        grouped[host].append(line)

    if duplicates:
        logger.info("Collapsed %d duplicate series URL(s) in %s", duplicates, source)

    ordered: dict[str, list[str]] = {}
    for host in DOMAIN_ORDER:
        if grouped.get(host):
            ordered[host] = grouped[host]
    return ordered, rejected


# ==================== HOST CHECK ====================
async def check_host(client: httpx.AsyncClient, host: str) -> tuple[bool, str]:
    url = base_url(host)
    for method in ("HEAD", "GET"):
        try:
            r = await client.request(method, url)
            if r.status_code < 400:
                return True, f"{method} {r.status_code}"
            if method == "HEAD" and r.status_code in (403, 405, 501):
                continue  # some mirrors reject HEAD outright
            return False, f"{method} {r.status_code}"
        except httpx.TimeoutException:
            if method == "GET":
                return False, "timeout"
        except Exception as exc:  # noqa: BLE001
            if method == "GET":
                return False, exc.__class__.__name__
    return False, "unreachable"


async def check_hosts(hosts: list[str]) -> dict[str, str]:
    """Ping every host concurrently and return ``{host: "OK (...)"|"FAIL (...)"}``.

    Concurrency only affects *different* hosts, so it cannot trip any single
    site's rate limits, but it turns a worst case of ``len(hosts) * timeout``
    seconds of startup into one timeout.
    """
    if not hosts:
        return {}
    async with httpx.AsyncClient(
        http2=True,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=REACHABILITY_TIMEOUT,
    ) as client:
        checks = await asyncio.gather(*(check_host(client, host) for host in hosts))

    statuses: dict[str, str] = {}
    for host, (ok, msg) in zip(hosts, checks, strict=True):
        statuses[host] = f"{'OK' if ok else 'FAIL'} ({msg})"
        if ok:
            logger.info("Reachable %s (%s)", host, msg)
        else:
            logger.warning("Unreachable %s (%s)", host, msg)
    return statuses


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
    grouped, _rejected = load_url_batches(urls_file)

    families_in_batch = {SUPPORTED_DOMAINS[host] for host in grouped if host in SUPPORTED_DOMAINS}
    hosts_to_check = [host for host in DOMAIN_ORDER if SUPPORTED_DOMAINS.get(host) in families_in_batch]
    statuses = await check_hosts(hosts_to_check)

    # Pick the first reachable host per family in DOMAIN_ORDER.
    active_host_by_family: dict[str, str] = {}
    for host in DOMAIN_ORDER:
        family = SUPPORTED_DOMAINS.get(host)
        if family and family not in active_host_by_family and statuses.get(host, "").startswith("OK"):
            active_host_by_family[family] = host

    resolved: dict[str, list[str]] = {}
    rewrites: dict[str, str] = {}

    for host, urls in grouped.items():
        family = SUPPORTED_DOMAINS.get(host)
        active_host = active_host_by_family.get(family or "")
        if not active_host:
            logger.warning("No reachable host for family %s — skipping %d URL(s)", family, len(urls))
            statuses[host] = f"FAIL (no reachable {family} mirror)"
            continue
        if host == active_host:
            resolved.setdefault(host, []).extend(urls)
            continue

        migrated: list[str] = []
        for url in urls:
            new_url = _url_for_host(url, active_host)
            if new_url:
                migrated.append(new_url)
                rewrites[url] = new_url
                logger.info("Migrated %s -> %s", url, new_url)
            else:
                logger.warning("Could not rewrite %s to %s", url, active_host)
        if migrated:
            resolved.setdefault(active_host, []).extend(migrated)
            statuses[host] = f"FAIL -> {active_host}"
            logger.warning("Migrated %d URL(s) from %s to %s", len(migrated), host, active_host)

    for host in resolved:
        resolved[host] = list(dict.fromkeys(resolved[host]))

    if rewrites and _rewrite_batch_urls(urls_file, rewrites):
        print(f"\n  → rewritten {len(rewrites)} URL(s) to active hosts:")
        for old, new in rewrites.items():
            print(f"    {old} -> {new}")
        logger.info("Rewrote %d URL(s) in %s", len(rewrites), urls_file)

    return resolved, statuses, active_host_by_family


# ==================== RESULT MODEL ====================
@dataclass
class SeasonOutcome:
    """Before/after episode counts for a single season."""

    season: int | str
    total: int = 0
    watched_before: int = 0
    watched_after: int = 0
    ok: bool = True
    note: str = ""

    def target(self, action: str) -> int:
        if action == ACTION_WATCHED:
            return self.total
        if action == ACTION_UNWATCHED:
            return 0
        return self.watched_after


@dataclass
class SeriesResult:
    host: str
    family: str
    url: str
    slug: str
    action: str = ""
    seasons: list[SeasonOutcome] = field(default_factory=list)
    subscribed: bool | None = None
    watchlist: bool | None = None
    title: str | None = None
    ok: bool = True
    note: str = ""

    @property
    def total_episodes(self) -> int:
        return sum(s.total for s in self.seasons)

    @property
    def watched_episodes(self) -> int:
        return sum(s.watched_after for s in self.seasons)

    @property
    def watched_before(self) -> int:
        return sum(s.watched_before for s in self.seasons)

    @property
    def at_target(self) -> bool:
        """True when every season reached the state this action asked for.

        Note this is action-aware: a fully *unwatched* series is at target with
        0 watched episodes. Comparing watched==total unconditionally would flag
        every successful unwatch run as a failure.
        """
        if not self.seasons:
            return False
        return all(s.watched_after == s.target(self.action) for s in self.seasons)

    @property
    def season_summary(self) -> str:
        return f"[{','.join(str(s.season) for s in self.seasons)}]"

    @property
    def status_extra(self) -> str:
        """`(Sub:✓ WL:✗)` for families that have those controls."""
        if self.family not in SUBSCRIBE_FAMILIES:
            return ""
        sub = _tri_state(self.subscribed)
        wl = _tri_state(self.watchlist)
        return f" (Sub:{sub} WL:{wl})"

    def line(self) -> str:
        status = "✓" if self.ok and self.at_target else "✗"
        display = f"{self.title} ({self.slug})" if self.title else self.slug
        note = f" — {self.note}" if self.note else ""
        return (
            f"{status} {display} {self.season_summary}: "
            f"{self.watched_episodes}/{self.total_episodes} watched"
            f"{self.status_extra}{note}"
        )

    def detail_lines(self) -> list[str]:
        lines = []
        for s in self.seasons:
            target = s.target(self.action)
            if s.watched_before == target and s.ok:
                continue
            note = f"  ({s.note})" if s.note else ""
            lines.append(f"▶S{s.season}: {s.watched_before}/{s.total} -> {s.watched_after}/{s.total}{note}")
        return lines


@dataclass
class SeriesPlan:
    """What the preview pass already learned, reused by the marking pass."""

    url: str
    host: str
    family: str
    slug: str
    seasons: list[int | str]
    title: str | None = None


def _tri_state(value: bool | None) -> str:
    return "✓" if value else "✗" if value is False else "?"


# ==================== DOMAIN WORKER ====================
class DomainWorker:
    def __init__(self, host: str):
        self.host = host
        family = SUPPORTED_DOMAINS.get(host)
        if not family:
            raise ValueError(f"Unsupported host: {host}")
        self.family: str = family
        self.creds = CREDENTIALS.get(self.family, {})
        self.client: httpx.AsyncClient | None = None
        self.logged_in = False

    @property
    def base(self) -> str:
        return base_url(self.host)

    async def __aenter__(self):
        self.client = httpx.AsyncClient(
            http2=True,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            timeout=HTTP_REQUEST_TIMEOUT,
        )
        return self

    async def __aexit__(self, *args):
        if self.client is not None:
            await self.client.aclose()

    # ---------- transport ----------
    async def _backoff(self, attempt: int, method: str, url: str, reason: str, retry_after: str | None = None) -> None:
        wait = min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_BACKOFF)
        if retry_after:
            # Honour the server's own pacing when it asks for more than ours.
            with contextlib.suppress(ValueError):
                wait = min(max(float(retry_after), wait), _MAX_BACKOFF)
        logger.warning(
            "%s %s -> %s, retrying in %.1fs (attempt %d/%d)",
            method,
            url,
            reason,
            wait,
            attempt,
            _MAX_RETRIES,
        )
        await asyncio.sleep(wait)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """One retrying request path shared by GET and POST."""
        if self.client is None:
            raise RuntimeError("DomainWorker client not initialized")

        last_err: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                r = await self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                last_err = exc
                if attempt < _MAX_RETRIES:
                    await self._backoff(attempt, method, url, exc.__class__.__name__)
                    continue
                break

            if r.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES:
                await self._backoff(
                    attempt,
                    method,
                    url,
                    str(r.status_code),
                    retry_after=r.headers.get("Retry-After"),
                )
                continue

            r.raise_for_status()
            return r

        raise last_err or RuntimeError(f"{method} {url} failed after {_MAX_RETRIES} attempts")

    async def _get_soup(self, url: str) -> BeautifulSoup:
        """Fetch a page and parse it exactly once.

        Everything downstream takes the parsed soup, so a season page costs one
        parse instead of the four it used to (error check, csrf, control lookup,
        episode count).
        """
        r = await self._request("GET", url)
        soup = _soup(r.text)
        code = _check_error_page(soup, self.family)
        if code:
            raise RuntimeError(f"error page {code} for {url}")
        return soup

    async def _post(
        self,
        url: str,
        data: dict | None = None,
        *,
        json: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        return await self._request("POST", url, data=data, json=json, headers=headers or {})

    @staticmethod
    def _csrf_headers(token: str, json: bool = True) -> dict[str, str]:
        h = {"X-CSRF-TOKEN": token, "X-Requested-With": "XMLHttpRequest"}
        if json:
            h["Accept"] = "application/json"
        return h

    # ---------- page parsing ----------
    @staticmethod
    def _extract_title(soup: BeautifulSoup, family: str) -> str | None:
        """Extract series title from a series page using scraper-style fallbacks.

        Ordered by how clean the source is: the dedicated headings first, the
        page <h2> next (bs.to's only real title, as "<name> Staffel N"), and
        og:title last because there it carries the whole site suffix.
        """
        selectors = ("h1[itemprop='name'] > span", "h1.fw-bold") if family == "aniworld" else ("h1.fw-bold",)
        for selector in selectors:
            title = _clean_title(_heading_text(soup.select_one(selector)))
            if title:
                return title

        title = _clean_title(_heading_text(soup.find("h2")))
        if title:
            return title

        og = soup.find("meta", attrs={"property": "og:title"})
        if og:
            title = _clean_title(_attr_str(og.get("content")) or "")
            if title:
                return title
        return None

    @staticmethod
    def _extract_csrf_token(soup: BeautifulSoup) -> str | None:
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta:
            token = _attr_str(meta.get("content"))
            if token:
                return token
        for inp in soup.find_all("input", attrs={"name": "_token", "value": True}):
            return _attr_str(inp.get("value"))
        return None

    @staticmethod
    def _extract_series_id(soup: BeautifulSoup) -> str | None:
        container = soup.select_one("div.add-series")
        return _attr_str(container.get("data-series-id")) if container else None

    def _is_logged_in(self, soup: BeautifulSoup) -> bool:
        marker = _LOGIN_MARKERS.get(self.family)
        if marker and soup.select_one(marker):
            return True
        # bs.to uses a relative `href="logout"`, which some layouts render
        # outside section.navigation.
        return self.family == "bs" and soup.find("a", href="logout") is not None

    # ---------- authentication ----------
    async def login(self) -> bool:
        if self.logged_in:
            return True
        if not any(self.creds.values()):
            logger.error("No credentials for family %r", self.family)
            return False
        try:
            self.logged_in = await self._login_form()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Login failed for %s: %s", self.host, exc)
            return False
        return self.logged_in

    async def _login_form(self) -> bool:
        base = self.base
        login_url = f"{base}/login"
        soup = await self._get_soup(login_url)

        if self.family == "bs":
            token_input = soup.find("input", {"name": "security_token"})
            token = (_attr_str(token_input.get("value")) if token_input else "") or ""
            if not token:
                logger.warning("CSRF security_token not found on login page for %s", self.host)
            r = await self._post(
                login_url,
                data={
                    "login[user]": self.creds.get("username", ""),
                    "login[pass]": self.creds.get("password", ""),
                    "security_token": token,
                },
            )
            if r.status_code not in _OK_POST_STATUS:
                return False
            # Verify on the series catalogue page, just like the BS scraper,
            # because the homepage may not reliably render the logout link.
            return self._is_logged_in(await self._get_soup(f"{base}/andere-serien"))

        # aniworld + s.to family
        payload: dict[str, str] = {}
        form = soup.find("form")
        if form:
            for inp in form.find_all("input", attrs={"name": True}):
                name = _attr_str(inp.get("name"))
                if name:
                    payload[name] = _attr_str(inp.get("value")) or ""
        payload["email"] = self.creds.get("email", "")
        payload["password"] = self.creds.get("password", "")

        # s.to family uses _token; aniworld uses security_token
        token = payload.get("_token") or payload.get("security_token", "")
        if not token:
            for name in ("_token", "security_token"):
                inp = soup.find("input", {"name": name, "value": True})
                if inp:
                    token = _attr_str(inp.get("value")) or ""
                    break
        if token:
            logger.info("Login CSRF token for %s: %s...", self.host, token[:16])
        else:
            logger.warning("CSRF token not found on login page for %s", self.host)

        r = await self._post(login_url, data=payload)
        if r.status_code not in _OK_POST_STATUS:
            return False
        return self._is_logged_in(await self._get_soup(base))

    async def _recover_session(self) -> bool:
        """Re-authenticate if the session went stale; False if it is still valid.

        Called only when a control we need is missing, which is the shape an
        expired session takes: the page renders, just without the logged-in
        controls. Returning False means the session is fine and the control is
        genuinely absent, so the caller should not retry.
        """
        try:
            if self._is_logged_in(await self._get_soup(self.base)):
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Session check failed for %s: %s", self.host, exc)

        logger.warning("Session for %s looks expired — re-authenticating", self.host)
        self.logged_in = False
        if self.client is not None:
            self.client.cookies.clear()
        return await self.login()

    # ---------- series structure ----------
    def season_url(self, slug: str, season: int | str) -> str:
        if isinstance(season, str) and season.lower() == MOVIES_SEASON.lower():
            return f"{self.base}/anime/stream/{slug}/filme"
        if self.family == "aniworld":
            return f"{self.base}/anime/stream/{slug}/staffel-{season}"
        if self.family == "sto":
            return f"{self.base}/serie/{slug}/staffel-{season}"
        return f"{self.base}/serie/{slug}/{season}"

    def discover_seasons(self, soup: BeautifulSoup, slug: str) -> list[int | str]:
        """List every season of a series from its already-fetched page.

        Each fallback runs only when the tier above it found nothing. Running
        them unconditionally (as this used to for bs/s.to) let unrelated numeric
        <option> values and stray data-season-id attributes invent seasons that
        do not exist.
        """
        seasons: set[int] = set()

        if self.family == "aniworld":
            has_movies = False
            for a in soup.select("#stream ul:first-of-type li a"):
                href = _attr_str(a.get("href")) or ""
                if not href:
                    continue
                m = _STAFFEL_RE.search(href)
                if m:
                    seasons.add(int(m.group(1)))
                elif _FILME_RE.search(href):
                    has_movies = True

            if not seasons:
                for a in soup.find_all("a", href=True):
                    href = _attr_str(a.get("href"))
                    if not href or "/anime/stream/" not in href:
                        continue
                    m = _STAFFEL_RE.search(href)
                    if m:
                        seasons.add(int(m.group(1)))
                    elif _FILME_RE.search(href):
                        has_movies = True

            if not seasons:
                seasons |= self._season_ids_from_attrs(soup)

            result: list[int | str] = sorted(seasons)
            if has_movies:
                result.append(MOVIES_SEASON)

        elif self.family == "bs":
            for a in soup.select("#seasons a"):
                href = (_attr_str(a.get("href")) or "").split("?")[0].split("#")[0]
                parts = href.strip("/").split("/")
                if len(parts) >= 3 and parts[0] == "serie":
                    with contextlib.suppress(ValueError, IndexError):
                        seasons.add(int(parts[2]))
            if not seasons:
                for opt in soup.find_all("option", value=True):
                    opt_value = _attr_str(opt.get("value"))
                    if opt_value and opt_value.isdigit():
                        seasons.add(int(opt_value))
            result = sorted(seasons)

        else:  # sto
            for link in soup.select("#season-nav a[data-season-pill]"):
                season_num = _attr_str(link.get("data-season-pill"))
                if season_num and season_num.isdigit():
                    seasons.add(int(season_num))
            if not seasons:
                staffel_re = re.compile(rf"/serie/{re.escape(slug)}/staffel-(\d+)")
                for a in soup.find_all("a", href=True):
                    m = staffel_re.search(_attr_str(a.get("href")) or "")
                    if m:
                        seasons.add(int(m.group(1)))
            if not seasons:
                seasons |= self._season_ids_from_attrs(soup)
            result = sorted(seasons)

        if not result:
            logger.warning("No seasons discovered for %s on %s — assuming season 1", slug, self.host)
            return [1]
        return result

    @staticmethod
    def _season_ids_from_attrs(soup: BeautifulSoup) -> set[int]:
        """Last-resort season numbers from data-season-id attributes.

        Must use select(): find_all("[data-season-id]") looks for a *tag* with
        that literal name and always matches nothing.
        """
        found: set[int] = set()
        for el in soup.select("[data-season-id]"):
            season_id = _attr_int(el.get("data-season-id"))
            if season_id is not None:
                found.add(season_id)
        return found

    @staticmethod
    def _row_is_watched(row) -> bool:
        if set(row.get("class") or []) & _WATCHED_CLASS_TOKENS:
            return True
        return any(_is_truthy_attr(row.get(attr)) for attr in _WATCHED_ATTRS)

    def _count_episodes(self, soup: BeautifulSoup) -> tuple[int, int]:
        """Return (watched_count, total_count) for a season page."""
        if self.family == "aniworld":
            rows = soup.select("table.seasonEpisodesList tbody tr[data-episode-id]") or soup.select(
                "tr[data-episode-id]"
            )
        else:  # bs and sto share the same episode table markup
            rows = (
                soup.select(".episode-table tbody tr.episode-row")
                or soup.select("tr.episode-row")
                or soup.select(".episode-row")
            )
            if not rows:
                table = soup.select_one("table.episodes")
                if table:
                    rows = [r for r in table.select("tr") if r.find_all("td")]

        return sum(1 for row in rows if self._row_is_watched(row)), len(rows)

    def _detect_subscription_status(self, soup: BeautifulSoup) -> tuple[bool | None, bool | None]:
        """Return (subscribed, watchlist) for aniworld/s.to families."""
        if self.family not in SUBSCRIBE_FAMILIES:
            return None, None

        if self.family == "aniworld":
            container = soup.select_one("div.add-series")
            if container:
                return (
                    _attr_str(container.get("data-series-favourite")) == "1",
                    _attr_str(container.get("data-series-watchlist")) == "1",
                )
            return (
                soup.select_one("li.setFavourite.buttonAction.true") is not None,
                soup.select_one("li.setWatchlist.buttonAction.true") is not None,
            )

        subscribed: bool | None = None
        watchlist: bool | None = None
        for button in self._sto_action_buttons(soup):
            data_type = _attr_str(button.get("data-type"))
            if data_type == "favorite":
                subscribed = self._sto_button_active(button)
            elif data_type == "watchlater":
                watchlist = self._sto_button_active(button)
        return subscribed, watchlist

    @staticmethod
    def _sto_action_buttons(soup: BeautifulSoup) -> list:
        return soup.select(".d-none.d-md-flex .js-action-btn") or soup.select(".js-action-btn")

    @staticmethod
    def _sto_button_active(button) -> bool:
        return "btn-glass-primary" in (button.get("class") or []) or button.get("data-active") == "1"

    # ---------- actions ----------
    async def ensure_subscribed(self, url: str, soup: BeautifulSoup) -> bool:
        """Subscribe to a series if the control is present and not already active."""
        if self.family not in SUBSCRIBE_FAMILIES:
            return True

        subscribed, _ = self._detect_subscription_status(soup)
        if subscribed:
            logger.info("Already subscribed: %s", url)
            return True

        if self.family == "aniworld":
            series_id = self._extract_series_id(soup)
            if not series_id:
                logger.warning("No series-id found for subscribe on %s", url)
                return False
            r = await self._post(f"{self.base}/ajax/setFavourite", data={"series": series_id})
            if r.status_code != 200:
                return False
            body = _json_body(r)
            # A plain-text/empty body is not a refusal. The authoritative check
            # is the Sub: status re-read from the series page after marking.
            return bool(body.get("status")) if body is not None else True

        # sto
        sub_url = None
        for button in self._sto_action_buttons(soup):
            if button.get("data-type") == "favorite":
                sub_url = _attr_str(button.get("data-url"))
                break
        token = self._extract_csrf_token(soup)
        if not sub_url:
            logger.warning("No favorite toggle URL found for %s", url)
            return False
        if not token:
            logger.warning("No CSRF token found for subscribe on %s", url)
            return False

        r = await self._post(urljoin(url, sub_url), headers=self._csrf_headers(token, json=False))
        if r.status_code != 200:
            logger.warning("Subscribe failed for %s: %s body=%r", url, r.status_code, r.text[:200])
            return False
        return True

    async def _issue_mark(
        self, soup: BeautifulSoup, season_url: str, slug: str, season: int | str, action: str
    ) -> None:
        """Send the site's native 'mark whole season' request.

        Raises ControlMissingError when a required control/token is absent so the
        caller can re-authenticate and retry once.
        """
        if self.family == "aniworld":
            season_id = None
            clear_all = soup.find("span", class_="clearAllEpisodesFromThisSeason")
            if clear_all:
                season_id = _attr_str(clear_all.get("data-season-id"))
            if not season_id and isinstance(season, int) and season in self._season_ids_from_attrs(soup):
                # Any element on this season page whose data-season-id matches.
                season_id = str(season)
            if not season_id:
                raise ControlMissingError(f"No season-id found for {slug} s{season}")

            series_id = self._extract_series_id(soup)
            if not series_id:
                raise ControlMissingError(f"No series-id found for {slug} s{season}")

            r = await self._post(
                f"{self.base}/ajax/watchseason",
                data={
                    "series": series_id,
                    "season": season_id,
                    "watch": "true" if action == ACTION_WATCHED else "false",
                },
            )
            if r.status_code not in _OK_POST_STATUS:
                raise RuntimeError(f"mark returned {r.status_code}")
            body = _json_body(r)
            if body is not None and body.get("status") is not True:
                raise RuntimeError(f"mark refused: {body}")

        elif self.family == "bs":
            verb = "watch:all" if action == ACTION_WATCHED else "unwatch:all"
            await self._get_soup(f"{self.base}/serie/{slug}/{season}/des/{verb}")

        else:  # sto
            ctrl = soup.select_one("#season-mark")
            mark_path = _attr_str(ctrl.get("data-mark-url")) if ctrl else None
            token = self._extract_csrf_token(soup)
            if not mark_path:
                raise ControlMissingError(f"No #season-mark control for {slug} s{season}")
            if not token:
                raise ControlMissingError(f"No CSRF token for {slug} s{season}")

            mark_url = urljoin(season_url, mark_path)
            r = await self._post(
                mark_url,
                json={"action": "seen" if action == ACTION_WATCHED else "unseen"},
                headers=self._csrf_headers(token),
            )
            logger.info("s.to mark POST %s -> %s body=%r", mark_url, r.status_code, r.text[:200])
            if r.status_code not in _OK_POST_STATUS:
                raise RuntimeError(f"mark returned {r.status_code}")
            body = _json_body(r)
            if body is not None and body.get("ok") is not True:
                raise RuntimeError(f"s.to mark did not report ok=true: {body}")

    async def mark_season(self, slug: str, season: int | str, action: str) -> SeasonOutcome:
        """Mark one season and verify the result by re-reading the page."""
        season_url = self.season_url(slug, season)
        outcome = SeasonOutcome(season=season)

        try:
            soup = await self._get_soup(season_url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not load season %s of %s: %s", season, slug, exc)
            return SeasonOutcome(season=season, ok=False, note=f"load failed: {exc}")

        before, total = self._count_episodes(soup)
        outcome.total = total
        outcome.watched_before = before
        outcome.watched_after = before

        if total == 0:
            # No episode rows parsed means we cannot mark or verify anything.
            # Reporting success here would be a silent lie.
            outcome.ok = False
            outcome.note = "no episodes found"
            logger.error("No episode rows found on %s — cannot mark or verify", season_url)
            return outcome

        target = total if action == ACTION_WATCHED else 0
        if before == target:
            logger.info("Skipping mark for %s season %s (already %s)", slug, season, action)
        else:
            try:
                try:
                    await self._issue_mark(soup, season_url, slug, season, action)
                except ControlMissingError as exc:
                    if not await self._recover_session():
                        raise
                    soup = await self._get_soup(season_url)
                    logger.info("Retrying mark for %s season %s after re-login (%s)", slug, season, exc)
                    await self._issue_mark(soup, season_url, slug, season, action)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed marking %s season %s: %s", slug, season, exc)
                outcome.ok = False
                outcome.note = str(exc)
                return outcome

        # Always verify against a freshly fetched page — for a skipped mark and
        # for an issued one alike. An HTTP 200 from these sites does not prove
        # the state actually changed.
        try:
            after, after_total = self._count_episodes(await self._get_soup(season_url))
        except Exception as exc:  # noqa: BLE001
            outcome.ok = False
            outcome.note = f"unverified: {exc}"
            logger.error("Could not verify season %s of %s: %s", season, slug, exc)
            return outcome

        if after_total and after_total != total:
            logger.warning(
                "Episode count for %s season %s changed during marking: %d -> %d",
                slug,
                season,
                total,
                after_total,
            )
            outcome.total = after_total
            target = after_total if action == ACTION_WATCHED else 0

        outcome.watched_after = after
        if after != target:
            outcome.ok = False
            outcome.note = f"expected {target}/{outcome.total} after {action}, got {after}"
            logger.error(
                "Verification failed for %s season %s: %d/%d watched, expected %d",
                slug,
                season,
                after,
                outcome.total,
                target,
            )
        return outcome

    async def inspect_series(self, url: str, action: str) -> tuple[SeriesResult, SeriesPlan]:
        """Read current state without changing anything (the preview pass).

        The returned plan carries the season list and title so the marking pass
        does not have to re-download and re-parse the series page.
        """
        slug = slug_for(url, self.family)
        result = SeriesResult(self.host, self.family, url, slug, action=action)

        soup = await self._get_soup(url)
        result.title = self._extract_title(soup, self.family)
        if is_utility_page_title(result.title):
            # A retired or mistyped slug is answered with the catalogue page at
            # HTTP 200. Marking whatever seasons it lists would target the wrong
            # thing entirely, so refuse instead of guessing.
            raise RuntimeError(f"not a series page — got catalogue page {result.title!r}")
        result.subscribed, result.watchlist = self._detect_subscription_status(soup)
        seasons = self.discover_seasons(soup, slug)

        for season in seasons:
            season_soup = await self._get_soup(self.season_url(slug, season))
            before, total = self._count_episodes(season_soup)
            outcome = SeasonOutcome(season=season, total=total, watched_before=before, watched_after=before)
            # watched_after holds the *planned* state so the preview can render
            # "12/24 → 24/24"; the marking pass overwrites it with reality.
            outcome.watched_after = outcome.target(action)
            result.seasons.append(outcome)

        plan = SeriesPlan(url=url, host=self.host, family=self.family, slug=slug, seasons=seasons, title=result.title)
        return result, plan

    @property
    def needs_subscribe(self) -> bool:
        return self.family in SUBSCRIBE_FAMILIES

    async def mark_series(self, plan: SeriesPlan, action: str) -> SeriesResult:
        result = SeriesResult(self.host, self.family, plan.url, plan.slug, action=action, title=plan.title)

        if not self.logged_in and not await self.login():
            result.ok = False
            result.note = "login failed"
            return result

        if action == ACTION_WATCHED and self.needs_subscribe:
            try:
                await self.ensure_subscribed(plan.url, await self._get_soup(plan.url))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Subscribe failed for %s: %s", plan.url, exc)

        for season in plan.seasons:
            outcome = await self.mark_season(plan.slug, season, action)
            result.seasons.append(outcome)
            if not outcome.ok:
                result.ok = False

        # Read subscription state once, after all seasons — it does not change
        # per season, and re-fetching the series page in the loop cost one full
        # page download per season for nothing.
        if self.needs_subscribe:
            try:
                result.subscribed, result.watchlist = self._detect_subscription_status(await self._get_soup(plan.url))
                if action == ACTION_WATCHED and result.subscribed is False:
                    logger.warning("Subscribe did not take effect for %s", plan.url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Status check failed for %s: %s", plan.url, exc)

        logger.info(
            "[%s] %s seasons %s action=%s",
            "OK" if result.ok else "FAIL",
            plan.url,
            plan.seasons,
            action,
        )
        return result


# ==================== BATCH PROCESSOR ====================
@dataclass
class RunReport:
    total_urls: int = 0
    successful: int = 0
    failed: int = 0
    failed_urls: list[str] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)


async def process_batch(
    action: str,
    plans_by_host: dict[str, list[SeriesPlan]],
    presets: list[SeriesResult],
) -> tuple[RunReport, list[SeriesResult]]:
    """Mark every planned series; ``presets`` are already-verified results."""
    results: list[SeriesResult] = list(presets)
    report = RunReport(total_urls=sum(len(p) for p in plans_by_host.values()) + len(presets))

    for host, plans in plans_by_host.items():
        print(f"\n  → {host}: {len(plans)} series")
        logger.info("Processing %s (%d URLs)", host, len(plans))

        async with DomainWorker(host) as worker:
            num_w = len(str(len(plans)))
            for idx, plan in enumerate(plans, 1):
                label = plan.title or plan.slug
                print(f"    [{idx:>{num_w}}/{len(plans)}] {label} ...", end=" ", flush=True)
                result = await worker.mark_series(plan, action)
                results.append(result)
                print(result.line())

    for result in results:
        if result.ok and result.at_target:
            report.successful += 1
        else:
            report.failed += 1
            report.failed_urls.append(result.url)

    _persist_failed_urls(report, {r.url for r in results})
    return report, results


def _persist_failed_urls(report: RunReport, attempted_urls: set[str]) -> None:
    """Reconcile this run's failures into the shared failed-urls file.

    IMPORTANT: this must never simply overwrite FAILED_URLS_FILE with
    ``report.failed_urls``. That list only reflects whatever batch/host
    scope *this* run happened to process (e.g. the user may have switched
    to a smaller or different batch file via the menu, or a host may have
    been skipped as unreachable). Blindly overwriting the file would wipe
    out unrelated failures recorded by an earlier, differently-scoped run
    that the user hasn't retried yet.

    Instead, only reconcile the URLs actually attempted in this run: a URL
    that succeeded is cleared, a URL that failed is (re)recorded, and any
    URL already on disk that this run never touched is left exactly as-is.
    """
    existing = set(_load_failed_urls())
    failed_this_run = {u for u in report.failed_urls if u}
    reconciled = sorted((existing - attempted_urls) | failed_this_run)

    Path(FAILED_URLS_FILE).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(FAILED_URLS_FILE, json.dumps(reconciled, indent=2, ensure_ascii=False))
    logger.info("Finished: %d successful, %d failed", report.successful, report.failed)


# ==================== FILE I/O ====================
def _atomic_write(path: str, content: str) -> None:
    """Write text to *path* atomically (temp file + fsync + os.replace).

    A plain open(path, "w") truncates the file immediately; if the process
    is interrupted mid-write (crash, kill, power loss) the file is left
    truncated/corrupted. Writing to a temp file in the same directory,
    flushing it to disk, and then os.replace()-ing it into place means
    readers only ever see either the old complete content or the new
    complete content.
    """
    directory = os.path.dirname(path) or "."
    Path(directory).mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise


def _read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f]
    except FileNotFoundError:
        return []


def _append_lines(path: str, lines: list[str]) -> None:
    """Append lines, repairing a missing trailing newline first.

    Appending to a file whose last line has no newline would splice the new
    URL onto the end of the existing one, silently corrupting both.
    """
    if not lines:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    needs_newline = False
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b"\n", b"\r")
    with open(path, "a", encoding="utf-8") as f:
        if needs_newline:
            f.write("\n")
        for line in lines:
            f.write(line + "\n")


def _rewrite_batch_urls(path: str, mapping: dict[str, str]) -> bool:
    """Swap migrated URLs in place, keeping comments and unknown lines.

    Rebuilding the file from the parsed batch would silently delete every
    comment, blank line and unsupported entry the user had in there.
    """
    lines = _read_lines(path)
    changed = False
    out: list[str] = []
    for line in lines:
        new = mapping.get(line.strip())
        if new and new != line.strip():
            out.append(new)
            changed = True
        else:
            out.append(line)
    if changed:
        _atomic_write(path, "".join(line + "\n" for line in out))
    return changed


def _load_failed_urls() -> list[str]:
    if not os.path.exists(FAILED_URLS_FILE):
        return []
    try:
        with open(FAILED_URLS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(u) for u in data if u]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read failed URLs: %s", exc)
    return []


def _batch_has_urls(urls_file: str) -> bool:
    return any(line.strip() and not line.strip().startswith("#") for line in _read_lines(urls_file))


def append_urls_to_scraper_lists(by_family: dict[str, set[str]]) -> None:
    """Append URLs to the per-family scraper series_urls.txt files."""
    for family in sorted(by_family):
        urls = by_family[family]
        export_path = SERIES_URLS_EXPORTS.get(family)
        if not export_path or not urls:
            continue

        if not os.path.exists(export_path):
            prompt = f"\n  export path for {family} does not exist:\n    {export_path}\n  create it?"
            if not ask_yes_no(prompt, default=False):
                print(f"  skipped {family} export")
                logger.info("Skipped %s export because path is missing", family)
                continue
            Path(export_path).parent.mkdir(parents=True, exist_ok=True)

        existing = {line.strip() for line in _read_lines(export_path)}
        existing.discard("")
        new_urls = sorted(urls - existing)
        if not new_urls:
            logger.info("No new URLs to append for %s", family)
            print(f"  {family}: nothing new to append")
            continue

        _append_lines(export_path, new_urls)
        logger.info("Appended %d URL(s) to %s scraper list: %s", len(new_urls), family, export_path)
        print(f"  appended {len(new_urls)} new URL(s) → {export_path}")


# ==================== UI ====================
def _trunc(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 1)] + "…"


def _term_width() -> int:
    return max(shutil.get_terminal_size().columns - 12, 40)


def _print_table(headers: list[str], rows: list[list[str]], caps: list[int], indent: str = "    ") -> None:
    """Render one aligned table; used for both the host list and the run summary."""
    if not rows:
        return
    widths = [max(1, min(max(len(headers[i]), *(len(row[i]) for row in rows)), caps[i])) for i in range(len(headers))]
    gap = "  "
    print((indent + gap.join(h.ljust(w) for h, w in zip(headers, widths, strict=True))).rstrip())
    print(indent + gap.join("─" * w for w in widths))
    for row in rows:
        print((indent + gap.join(_trunc(c, w).ljust(w) for c, w in zip(row, widths, strict=True))).rstrip())
    print("─" * (len(indent) + sum(widths) + len(gap) * (len(widths) - 1)))


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
        print("  use option 5 to add a URL or switch batch files.")

    print("\n  hosts:")
    if statuses:
        term_w = _term_width()
        rows = []
        for host, status in sorted(statuses.items()):
            short = status[3:] if status.startswith("OK ") else status[5:] if status.startswith("FAIL ") else status
            marker = "  ← ACTIVE" if host in active_hosts else ""
            rows.append([host, "✓" if status.startswith("OK") else "✗", f"{short}{marker}"])
        _print_table(["Host", "State", "Details"], rows, [term_w // 2, 5, term_w // 2])
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
    # Show which answer Enter picks, so the default is never a surprise.
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


def print_batch_summary(
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
    for host, urls in sorted(grouped.items()):
        family = SUPPORTED_DOMAINS.get(host, "?")
        print(f"      • {host} ({family}): {len(urls)}")
        for url in urls[:max_urls_per_host]:
            print(f"          {url}")
        remaining = len(urls) - max_urls_per_host
        if remaining > 0:
            print(f"          ... and {remaining} more")
    if rejected:
        print(f"    ⚠ skipped {len(rejected)} unsupported URL(s):")
        for item in rejected[:max_urls_per_host]:
            print(f"          {item['line']}  ({item['reason']})")


def _print_run_summary(report: RunReport, results: list[SeriesResult]) -> None:
    print("\n" + "=" * 56)
    print("  RUN SUMMARY")
    print("=" * 56)

    if results:
        col = _term_width() // 3
        rows = [[r.host, r.title or r.slug, r.line()] for r in results]
        _print_table(["Host", "Series", "Result"], rows, [col, col, col])

    metrics = [
        ("Series processed", str(report.total_urls)),
        ("Successful", str(report.successful)),
        ("Failed", str(report.failed)),
        ("Episodes watched", f"{sum(r.watched_episodes for r in results)}/{sum(r.total_episodes for r in results)}"),
    ]
    if report.rejected:
        metrics.append(("Unsupported lines", str(len(report.rejected))))
    if report.failed:
        metrics.append(("Failed list", FAILED_URLS_FILE))

    label_w = max(len(m[0]) for m in metrics)
    for label, value in metrics:
        print(f"    {label:<{label_w}}  {value}".rstrip())
    print("=" * 56)


def validate_credentials_for_batch(grouped: dict[str, list[str]]) -> list[str]:
    used = {SUPPORTED_DOMAINS[host] for host in grouped if host in SUPPORTED_DOMAINS}
    return sorted(family for family in used if not any(CREDENTIALS.get(family, {}).values()))


# ==================== MENU ACTIONS ====================
def _failed_result(host: str, family: str, url: str, action: str, note: str) -> SeriesResult:
    try:
        slug = slug_for(url, family)
    except ValueError:
        slug = url
    return SeriesResult(host, family, url, slug, action=action, ok=False, note=note)


async def _preview(
    action: str,
    grouped: dict[str, list[str]],
) -> tuple[list[tuple[SeriesResult, SeriesPlan]], list[SeriesResult], list[SeriesResult]]:
    """Read the current state of every series.

    Returns (todo, already_done, broken): series that need work paired with
    their plan, series already at the target state, and series we could not
    read at all. Nothing here writes to the sites.
    """
    todo: list[tuple[SeriesResult, SeriesPlan]] = []
    done: list[SeriesResult] = []
    broken: list[SeriesResult] = []

    for host, urls in sorted(grouped.items()):
        family = SUPPORTED_DOMAINS.get(host, "?")
        async with DomainWorker(host) as worker:
            if not await worker.login():
                print(f"  ✗ could not log in to {host} — {len(urls)} series skipped")
                # Record them as failures rather than dropping them silently,
                # so they land in the retry list.
                broken.extend(_failed_result(host, family, url, action, "login failed") for url in urls)
                continue

            for url in urls:
                try:
                    result, plan = await worker.inspect_series(url, action)
                except Exception as exc:  # noqa: BLE001
                    print(f"  ✗ preview failed for {url}: {exc}")
                    logger.exception("Preview failed for %s: %s", url, exc)
                    broken.append(_failed_result(host, family, url, action, f"preview failed: {exc}"))
                    continue

                needs_sub = action == ACTION_WATCHED and worker.needs_subscribe and result.subscribed is False
                needs_episodes = any(s.watched_before != s.target(action) for s in result.seasons)
                unreadable = any(s.total == 0 for s in result.seasons)

                sub_badge = " ⚡" if needs_sub else ""
                counter = f"{result.watched_before}/{result.total_episodes}"
                if needs_episodes:
                    counter += f" → {result.watched_episodes}/{result.total_episodes}"
                print(
                    f"  {host}: {result.title or result.slug}{result.status_extra}{sub_badge} "
                    f"{result.season_summary} — {counter}"
                )
                for line in result.detail_lines():
                    print(f"      {line}")
                if unreadable:
                    print("      ⚠ some seasons list no episodes — will be reported as failed")

                if needs_episodes or needs_sub or unreadable:
                    todo.append((result, plan))
                else:
                    done.append(result)

    return todo, done, broken


async def run_action(action: str, grouped: dict[str, list[str]], rejected: list[dict]) -> None:
    missing = validate_credentials_for_batch(grouped)
    if missing:
        print("\n  ✗ missing credentials for:", ", ".join(missing))
        print("  please fill in watchmaker/.env")
        return

    if not grouped:
        print("\n  nothing to do — no reachable series in this batch.")
        return

    print_batch_summary(grouped, action=action)
    print("\n  → preview before marking:")
    print(f"  action: {action}")
    print()

    todo, done, broken = await _preview(action, grouped)

    if not todo:
        if broken:
            print(f"\n  ✗ {len(broken)} series could not be read; nothing was marked.")
            report, results = RunReport(total_urls=len(broken) + len(done)), broken + done
            report.failed = len(broken)
            report.successful = len(done)
            report.failed_urls = [r.url for r in broken]
            report.rejected = rejected
            _persist_failed_urls(report, {r.url for r in results})
            _print_run_summary(report, results)
        else:
            print(f"\n  → nothing to do; all {len(done)} series already at target state ({action}).")
        return

    print(f"\n  → {len(todo)} series to change, {len(done)} already at target state.")
    if not ask_yes_no("\n  proceed with marking?", default=False):
        print("  marking cancelled.")
        return

    plans_by_host: dict[str, list[SeriesPlan]] = {}
    for _result, plan in todo:
        plans_by_host.setdefault(plan.host, []).append(plan)

    # Series already at the target state were just verified page-by-page in the
    # preview, so they carry straight into the report instead of being fetched
    # and re-verified a second time.
    report, results = await process_batch(action, plans_by_host, done + broken)
    report.rejected = rejected
    _print_run_summary(report, results)


def _urls_by_family(grouped: dict[str, list[str]]) -> dict[str, set[str]]:
    by_family: dict[str, set[str]] = {}
    for host, urls in grouped.items():
        family = SUPPORTED_DOMAINS.get(host)
        if family:
            by_family.setdefault(family, set()).update(urls)
    return by_family


async def export_urls(urls_file: str) -> None:
    """Manually export URLs from the batch file to scraper lists."""
    grouped, _ = load_url_batches(urls_file)
    by_family = _urls_by_family(grouped)
    if not by_family:
        print("\n  → 0 series available to export")
        print("  no supported URLs to export.")
        return

    print("\n  → export URLs to scraper lists")
    print(f"  total series: {sum(len(u) for u in by_family.values())}")
    print("\n  per family:")
    for family in sorted(by_family):
        print(f"    {family} ({len(by_family[family])}) → {SERIES_URLS_EXPORTS.get(family) or 'disabled'}")

    print("\n  URLs:")
    for family in sorted(by_family):
        print(f"\n  [{family}]")
        for idx, url in enumerate(sorted(by_family[family]), 1):
            print(f"    {idx}. {url}")

    if not ask_yes_no("\n  proceed with export?"):
        print("  export cancelled.")
        return

    append_urls_to_scraper_lists(by_family)


async def import_urls(urls_file: str) -> None:
    """Manually import URLs from scraper lists into the batch file."""
    by_family: dict[str, list[str]] = {}
    missing_paths: list[tuple[str, str]] = []

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
            url = line.split("#", 1)[0].rstrip()
            classification = classify_url(url)
            if classification is None or classification[1] != family or url in seen:
                continue
            seen.add(url)
            by_family.setdefault(family, []).append(url)

    if not by_family:
        print("\n  → 0 series available to import")
        for family, path in missing_paths:
            print(f"  {family} scraper list not found: {path}")
        print("  no supported URLs to import.")
        return

    print("\n  → import URLs from scraper lists")
    print(f"  total series found: {sum(len(u) for u in by_family.values())}")
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

    # Deduplicate against the current batch by series identity, not by exact
    # string, so /serie/x and /serie/x/staffel-2 are not both imported.
    grouped, _ = load_url_batches(urls_file)
    existing_keys = {
        (c[0], c[2].lower()) for urls in grouped.values() for url in urls if (c := classify_url(url)) is not None
    }

    added_by_family: dict[str, int] = {}
    new_urls: list[str] = []
    for family in sorted(by_family):
        for url in by_family[family]:
            classification = classify_url(url)
            if classification is None:
                continue
            key = (classification[0], classification[2].lower())
            if key in existing_keys:
                continue
            existing_keys.add(key)
            new_urls.append(url)
            added_by_family[family] = added_by_family.get(family, 0) + 1

    if not new_urls:
        print("\n  no new URLs to import (all already in batch).")
        return

    _append_lines(urls_file, new_urls)
    print(f"\n  appended {len(new_urls)} new URL(s) → {urls_file}")
    for family, count in sorted(added_by_family.items()):
        print(f"    {family}: {count}")
    logger.info("Imported %d URL(s) from scraper lists into %s", len(new_urls), urls_file)


async def retry_failed_urls(urls_file: str) -> str:
    """Replace the batch file with the recorded failed URLs."""
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

    _atomic_write(urls_file, "".join(url + "\n" for url in failed))
    print(f"  wrote {len(failed)} failed URL(s) → {urls_file}")
    logger.info("Wrote %d failed URL(s) to %s", len(failed), urls_file)
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

    if user_input.startswith(("http://", "https://")):
        if classify_url(user_input) is None:
            print(f"  ✗ not a supported series URL: {user_input}")
            return urls_file
        if (
            urls_file != DEFAULT_BATCH_FILE
            and _batch_has_urls(DEFAULT_BATCH_FILE)
            and not ask_yes_no(f"  overwrite {DEFAULT_BATCH_FILE}?", default=False)
        ):
            print("  cancelled.")
            return urls_file
        _atomic_write(DEFAULT_BATCH_FILE, user_input + "\n")
        print(f"  wrote 1 URL → {DEFAULT_BATCH_FILE}")
        return DEFAULT_BATCH_FILE

    candidate = user_input
    if not os.path.exists(candidate):
        candidate = os.path.join(os.path.dirname(DEFAULT_BATCH_FILE), user_input)
    if not os.path.exists(candidate):
        print(f"  ✗ file not found: {user_input}")
        return urls_file
    print(f"  loaded batch file → {candidate}")
    return candidate


# ==================== MAIN ====================
async def main() -> None:
    _configure_console()
    setup_logging(verbose=False)

    urls_file = DEFAULT_BATCH_FILE
    Path(urls_file).parent.mkdir(parents=True, exist_ok=True)

    initial_grouped, rejected = load_url_batches(urls_file)
    print_banner()
    print_batch_summary(initial_grouped, header="loaded batch", rejected=rejected)

    print("\n  → checking hosts ...")
    resolved, host_statuses, active_host_by_family = await resolve_active_hosts(urls_file)

    async def refresh() -> None:
        nonlocal resolved, host_statuses, active_host_by_family, rejected
        print("\n  → refreshing host resolution ...")
        resolved, host_statuses, active_host_by_family = await resolve_active_hosts(urls_file)
        _, rejected = load_url_batches(urls_file)

    while True:
        print_banner()
        print_menu(urls_file, host_statuses, bool(_load_failed_urls()), active_host_by_family)

        choice = input("\n  enter number: ").strip()
        if choice == "0":
            print("  exiting.")
            break
        if choice == "1":
            await run_action(ACTION_WATCHED, resolved, rejected)
        elif choice == "2":
            await run_action(ACTION_UNWATCHED, resolved, rejected)
        elif choice == "3":
            await export_urls(urls_file)
        elif choice == "4":
            urls_file = await retry_failed_urls(urls_file)
            await refresh()
        elif choice == "5":
            urls_file = await _detect_and_add_input(urls_file)
            await refresh()
        elif choice == "6":
            await import_urls(urls_file)
            await refresh()
        else:
            print("  invalid option.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  interrupted.")
        sys.exit(1)
