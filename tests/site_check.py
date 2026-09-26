"""Check the three live sites for changes that would break watchmaker.

Run monthly by .github/workflows/site-check.yml, which opens an issue when a
check fails, comments on it while it stays open, and closes it once a run
passes again. Also runnable by hand from the project root:

    python tests/site_check.py [--report FILE]

Every check runs watchmaker's own code against today's pages: the host probe
behind the mirror choice, and DomainWorker's title, season and episode
readers. So a check fails when watchmaker itself would, not merely when a
site looks different.

Without credentials only public pages are read: each family's login form, one
series page and one of its season pages. With a family's credentials set (as
repository secrets, for the workflow) it also logs in and looks for the
controls a mark would use -- through a DomainWorker that has no HTTP client
at all, so it can find a control but has no way to press it. bs.to marks by
opening a link rather than through a page control, so it gets the login
check only. Nothing is ever changed on any account.

The report carries check names, status codes and counts only -- never page
text, watched counts, or an account name -- because the issue it feeds may
be public.

Exit status: 0 every check passed; 1 a check failed, so a site changed in a
way watchmaker depends on; 2 nothing failed, but something could not be
checked (site down, or this network blocked); 3 the check itself crashed.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import traceback
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from config import CREDENTIALS, DOMAIN_ORDER, SUPPORTED_DOMAINS, USER_AGENT  # noqa: E402

PASS, FAIL, UNREACHABLE, SKIPPED = "pass", "fail", "unreachable", "skipped"
_MARK = {PASS: "✅ pass", FAIL: "❌ fail", UNREACHABLE: "⚠️ unreachable", SKIPPED: "➖ skipped"}

# Long-running series per family that should outlive any one check. A slug
# that has gone is skipped, and series linked from the home page are tried
# after these.
PROBE_SLUGS = {
    "sto": ("die-simpsons", "the-walking-dead", "breaking-bad"),
    "aniworld": ("one-piece", "naruto", "jujutsu-kaisen"),
    "bs": ("Breaking-Bad", "Better-Call-Saul", "Die-Simpsons"),
}
# The form fields _login_form posts, per family.
LOGIN_FIELDS = {
    "sto": ("email", "password"),
    "aniworld": ("email", "password"),
    "bs": ("login[user]", "login[pass]", "security_token"),
}
# The environment variables config.CREDENTIALS reads, per family.
CREDENTIAL_VARS = {
    "sto": "STO_EMAIL and STO_PASSWORD",
    "aniworld": "ANIWORLD_EMAIL and ANIWORLD_PASSWORD",
    "bs": "BS_USERNAME and BS_PASSWORD",
}
_SERIES_PATH = {"sto": "/serie/{slug}", "aniworld": "/anime/stream/{slug}", "bs": "/serie/{slug}"}
_SLUG_RE = {"sto": main._SERIE_SLUG_RE, "aniworld": main._ANIME_SLUG_RE, "bs": main._SERIE_SLUG_RE}

# Titles of the interstitial pages bot protection serves instead of the site.
# Matched on <title> only: a real login page may well mention a captcha.
_CHALLENGE_TITLE_RE = re.compile(
    r"<title>\s*(just a moment|attention required|checking your browser|ddos-guard)",
    re.IGNORECASE,
)
_HOME_PROBES = 3


@dataclass
class Result:
    check: str
    status: str
    detail: str


class UnreachableError(Exception):
    """The page could not be checked at all: site down, or this network blocked."""


def unreachable_reason(resp: httpx.Response) -> str | None:
    """Why a response says nothing about the site's layout, or None if it does."""
    challenged = resp.headers.get("cf-mitigated", "").lower() == "challenge" or bool(
        _CHALLENGE_TITLE_RE.search(resp.text[:20000])
    )
    if resp.status_code in (401, 403, 407, 429) or resp.status_code >= 500:
        return f"HTTP {resp.status_code}" + (" (bot check)" if challenged else "")
    if challenged:
        return f"HTTP {resp.status_code} bot-check page"
    return None


async def fetch(client: httpx.AsyncClient, url: str) -> httpx.Response:
    try:
        resp = await client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise UnreachableError(f"{type(exc).__name__} for {urlparse(url).netloc}") from exc
    reason = unreachable_reason(resp)
    if reason:
        raise UnreachableError(f"{reason} for {urlparse(url).netloc}")
    return resp


class DryRunWorker(main.DomainWorker):
    """A DomainWorker that can look for mark controls but cannot use them.

    It is never given an HTTP client, so _request raises before anything
    could leave the machine; _post and _get_soup refuse on top of that.
    """

    async def _post(self, url, data=None, *, json=None, headers=None):
        # Stands in for the site's success answer, so _issue_mark runs to the
        # end and only a missing control can stop it.
        return httpx.Response(200, json={"ok": True, "status": True}, request=httpx.Request("POST", url))

    async def _get_soup(self, url):
        raise AssertionError("the dry run must not fetch anything")


def check_login_page(family: str, html: str) -> Result:
    """The page the host probe accepts a mirror on, and the fields login posts."""
    if not main._looks_like_login_page(html):
        return Result("login page", FAIL, "no password field or login form: the host probe would reject this mirror")
    doc = main.make_doc(html)
    names = {str(n) for n in doc.xpath("//input/@name")} if doc is not None else set()
    missing = [f for f in LOGIN_FIELDS[family] if f not in names]
    if missing:
        return Result("login page", FAIL, f"form no longer has field(s) {', '.join(missing)}, which login posts")
    return Result("login page", PASS, f"form has {', '.join(LOGIN_FIELDS[family])}")


def slugs_linked_from(family: str, html: str, base: str) -> list[str]:
    """Series slugs linked from a page, in page order, for use as probes."""
    doc = main.make_doc(html)
    if doc is None:
        return []
    slugs: list[str] = []
    for href in doc.xpath("//a/@href"):
        m = _SLUG_RE[family].match(urlparse(urljoin(base + "/", str(href))).path)
        if m and m.group(1) not in slugs:
            slugs.append(m.group(1))
    return slugs


def has_season_nav(doc, family: str) -> bool:
    """Whether the page has the navigation that makes it a series page.

    discover_seasons cannot say this itself: with no navigation it assumes a
    single season 1 and carries on.
    """
    return any(main._first(doc, xpath) is not None for xpath in main._SEASON_NAV_XPATHS[family])


async def find_series(client: httpx.AsyncClient, worker: main.DomainWorker) -> tuple[str, str, object] | None:
    """The first probe series whose page exists, as (slug, url, parsed page)."""
    family = worker.family
    tried: list[str] = []

    async def try_slugs(slugs) -> tuple[str, str, object] | None:
        for slug in slugs:
            if slug in tried:
                continue
            tried.append(slug)
            url = worker.base + _SERIES_PATH[family].format(slug=slug)
            resp = await fetch(client, url)
            if resp.status_code == 404:
                continue
            doc = main.make_doc(resp.text)
            if doc is None or main._check_error_page(doc, family):
                continue
            return slug, url, doc
        return None

    found = await try_slugs(PROBE_SLUGS[family])
    if found is None:
        home = await fetch(client, worker.base + "/")
        found = await try_slugs(slugs_linked_from(family, home.text, worker.base)[:_HOME_PROBES])
    return found


async def check_public_pages(client: httpx.AsyncClient, worker: main.DomainWorker) -> tuple[list[Result], str | None]:
    """Check a series page and one of its seasons; also return the series' slug."""
    try:
        found = await find_series(client, worker)
    except UnreachableError as exc:
        return [Result("series page", UNREACHABLE, str(exc))], None
    if found is None:
        detail = "no probe series and no series linked from the home page has a page that parses"
        return [Result("series page", FAIL, detail)], None
    slug, _url, doc = found
    problems = []
    title = worker._extract_title(doc, worker.family)
    if not title or main.is_utility_page_title(title):
        problems.append("no series title found")
    if not has_season_nav(doc, worker.family):
        problems.append("season navigation not found, so every series would be treated as season 1 only")
    if problems:
        return [Result("series page", FAIL, f"{slug}: {'; '.join(problems)}")], slug
    seasons = worker.discover_seasons(doc, slug)
    results = [Result("series page", PASS, f"{slug}: title and {len(seasons)} season(s)")]

    season = first_numbered(seasons)
    try:
        resp = await fetch(client, worker.season_url(slug, season))
    except UnreachableError as exc:
        return [*results, Result("season page", UNREACHABLE, str(exc))], slug
    season_doc = main.make_doc(resp.text)
    _watched, total = worker._count_episodes(season_doc) if season_doc is not None else (0, 0)
    if total:
        results.append(Result("season page", PASS, f"season {season}: {total} episode rows"))
    else:
        results.append(
            Result("season page", FAIL, f"{slug} season {season}: no episode rows, so no mark can be verified")
        )
    return results, slug


def first_numbered(seasons: list[int | str]) -> int | str:
    """The first real season: specials (0) and films are tried by nothing else."""
    numbered = [s for s in seasons if isinstance(s, int) and s > 0]
    return numbered[0] if numbered else seasons[0]


async def check_logged_in(worker: main.DomainWorker, slug: str | None) -> list[Result]:
    """Log in with watchmaker's own code and look for the mark controls."""
    if not await worker.login():
        return [Result("login", FAIL, "rejected: check the credential secrets, then the login flow")]
    results = [Result("login", PASS, "logged in and verified")]
    if not slug or worker.client is None:
        return results
    family = worker.family
    series_url = worker.base + _SERIES_PATH[family].format(slug=slug)
    try:
        series_doc = main.make_doc((await fetch(worker.client, series_url)).text)
        season = first_numbered(worker.discover_seasons(series_doc, slug)) if series_doc is not None else 1
        season_url = worker.season_url(slug, season)
        season_doc = main.make_doc((await fetch(worker.client, season_url)).text)
    except UnreachableError as exc:
        return [*results, Result("mark controls", UNREACHABLE, str(exc))]
    if series_doc is None or season_doc is None or not worker._is_logged_in(season_doc):
        return [*results, Result("mark controls", FAIL, "season page does not show the logged-in marker")]
    if family == "sto" and worker._detect_subscription_status(series_doc)[0] is None:
        results.append(Result("subscribe control", FAIL, "subscribe button not found on the series page"))
    elif family == "sto":
        results.append(Result("subscribe control", PASS, "subscribe button found"))
    if family == "bs":
        results.append(Result("mark controls", SKIPPED, "bs.to marks by link, not by a page control"))
        return results
    try:
        await DryRunWorker(worker.host)._issue_mark(season_doc, season_url, slug, season, main.ACTION_WATCHED)
    except main.ControlMissingError as exc:
        results.append(Result("mark controls", FAIL, str(exc)))
    else:
        results.append(Result("mark controls", PASS, f"season {season}: everything a mark needs is on the page"))
    return results


async def _first_login_page(client: httpx.AsyncClient, family: str) -> tuple[str | None, str, list[str]]:
    """The first https mirror of a family serving a login page, in DOMAIN_ORDER.

    Mirrors on a bare IP are left out: they are plain http, and the logged-in
    checks would send the password over them.
    """
    reasons = []
    for host in DOMAIN_ORDER:
        if SUPPORTED_DOMAINS.get(host) != family or main._scheme_for_host(host) != "https":
            continue
        try:
            resp = await fetch(client, f"{main.base_url(host)}/login")
        except UnreachableError as exc:
            reasons.append(str(exc))
            continue
        if resp.status_code == 404:
            reasons.append(f"HTTP 404 for /login on {host}")
            continue
        return host, resp.text, reasons
    return None, "", reasons


async def check_family(family: str) -> tuple[str | None, list[Result]]:
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(30.0, connect=10.0)
    ) as client:
        host, login_html, reasons = await _first_login_page(client, family)
        if host is None:
            # Every mirror answering 404 means the login page moved; anything
            # else in the mix means at least one mirror could not be asked.
            status = FAIL if reasons and all("HTTP 404" in r for r in reasons) else UNREACHABLE
            return None, [Result("login page", status, "; ".join(reasons) or "no https mirror configured")]
        worker = main.DomainWorker(host)
        results = [check_login_page(family, login_html)]
        public, slug = await check_public_pages(client, worker)
        results.extend(public)

    if not all(CREDENTIALS.get(family, {}).values()):
        results.append(Result("logged-in checks", SKIPPED, f"set {CREDENTIAL_VARS[family]} to log in and check"))
        return host, results
    async with main.DomainWorker(host) as worker:
        results.extend(await check_logged_in(worker, slug))
    return host, results


async def run_checks() -> list[Result]:
    """Every family's checks, each check named after the mirror it ran on."""
    results: list[Result] = []
    for family in PROBE_SLUGS:
        host, family_results = await check_family(family)
        where = host or f"{family} (no reachable mirror)"
        results.extend(Result(f"{where}: {r.check}", r.status, r.detail) for r in family_results)
    return results


def exit_code(results: list[Result]) -> int:
    statuses = {r.status for r in results}
    if FAIL in statuses:
        return 1
    if UNREACHABLE in statuses:
        return 2
    return 0


def render(results: list[Result], today: date) -> str:
    lines = [
        f"### Site check: {today.isoformat()}",
        "",
        "| Check | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for r in results:
        lines.append(f"| {r.check} | {_MARK[r.status]} | {r.detail.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main_cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the three live sites for changes that would break watchmaker.")
    ap.add_argument("--report", help="also write the markdown report to this file")
    args = ap.parse_args(argv)
    # The report is full of ✅/❌; a Windows console or pipe on cp1252 crashed
    # on printing it after every check had already run.
    main._configure_console()
    try:
        results = asyncio.run(run_checks())
        report, code = render(results, date.today()), exit_code(results)
    except Exception:  # noqa: BLE001 -- reported as a crash, not mistaken for a failed check
        traceback.print_exc()
        report, code = "### Site check crashed\n\nSee the workflow log for the traceback.\n", 3
    print(report)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main_cli())
