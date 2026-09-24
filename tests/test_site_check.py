"""The monthly live-site check, run against fake copies of the three sites.

The check can only earn trust by being right in both directions: silent on
healthy sites, loud on each kind of change watchmaker depends on, and never
calling a blocked runner a broken site. It must also never mark anything,
which the recorded requests below prove. Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import CREDENTIALS  # noqa: E402
from tests import site_check  # noqa: E402
from tests.site_check import FAIL, PASS, SKIPPED, UNREACHABLE  # noqa: E402

ACCOUNT_NAME = "SecretAccountName"
STO, ANI, BS = "serienstream.to", "aniworld.to", "burningseries.ac"

STO_SERIES = "/serie/die-simpsons"
ANI_SERIES = "/anime/stream/one-piece"
BS_SERIES = "/serie/Breaking-Bad"

# Logged-in chrome per site: the marker DomainWorker._is_logged_in looks for,
# next to the account name the report must never repeat.
CHROME = {
    STO: f'<form action="/logout"></form><a href="/user/profil/{ACCOUNT_NAME}">{ACCOUNT_NAME}</a>',
    ANI: f'<div class="avatar"><a href="/user/profil/{ACCOUNT_NAME}">{ACCOUNT_NAME}</a></div>',
    BS: f'<section class="navigation"><strong>{ACCOUNT_NAME}</strong><a href="logout">Logout</a></section>',
}
# What only a logged-in page carries: the controls a mark or subscribe uses.
CONTROLS = {
    STO: """<meta name="csrf-token" content="tok">
<div id="season-mark" data-mark-url="/serie/die-simpsons/staffel-1/mark"></div>
<div class="d-none d-md-flex"><button class="js-action-btn" data-type="favorite"></button></div>""",
    ANI: """<div class="add-series" data-series-id="42" data-series-favourite="0" data-series-watchlist="0"></div>
<span class="clearAllEpisodesFromThisSeason" data-season-id="7"></span>""",
    BS: "",
}

PAGES = {
    STO: {
        "/login": """<html><body><form action="/login" method="post"><input type="hidden" name="_token" value="t">
<input type="email" name="email"><input type="password" name="password"></form></body></html>""",
        STO_SERIES: f"""<html><body>{{chrome}}<h1 class="fw-bold">Die Simpsons</h1><div id="season-nav">
<a data-season-pill="0" href="{STO_SERIES}/staffel-0">Filme</a>
<a data-season-pill="1" href="{STO_SERIES}/staffel-1">1</a></div>{{controls}}</body></html>""",
        f"{STO_SERIES}/staffel-1": """<html><body>{chrome}{controls}<table class="episode-table"><tbody>
<tr class="episode-row"><th class="episode-number-cell">1</th><td>Es weihnachtet schwer</td></tr>
<tr class="episode-row"><th class="episode-number-cell">2</th><td>Bart wird ein Genie</td></tr>
</tbody></table></body></html>""",
    },
    ANI: {
        "/login": """<html><body><form action="/login" method="post"><input type="email" name="email">
<input type="password" name="password"></form></body></html>""",
        ANI_SERIES: f"""<html><body>{{chrome}}<h1 itemprop="name"><span>One Piece</span></h1><div id="stream"><ul>
<li><a href="{ANI_SERIES}/staffel-1">1</a></li><li><a href="{ANI_SERIES}/filme">Filme</a></li></ul></div>
{{controls}}</body></html>""",
        f"{ANI_SERIES}/staffel-1": """<html><body>{chrome}{controls}<table class="seasonEpisodesList"><tbody>
<tr data-episode-id="11"><td>1</td></tr><tr data-episode-id="12"><td>2</td></tr></tbody></table></body></html>""",
    },
    BS: {
        "/login": """<html><body><form action="login" method="post">
<input type="hidden" name="security_token" value="t"><input type="text" name="login[user]">
<input type="password" name="login[pass]"></form></body></html>""",
        BS_SERIES: """<html><body>{chrome}<h2>Breaking Bad <small>Staffel 1</small></h2><div id="seasons"><ul>
<li><a href="serie/Breaking-Bad/0">Specials</a></li><li><a href="serie/Breaking-Bad/1">1</a></li>
</ul></div></body></html>""",
        f"{BS_SERIES}/1": """<html><body>{chrome}<table class="episodes">
<tr><td><a href="#">1</a></td><td><strong>Der Einstieg</strong></td></tr>
<tr><td><a href="#">2</a></td><td><strong>Die Katze ist im Sack</strong></td></tr></table></body></html>""",
    },
}
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>cf-chl</body></html>"


class FakeSites:
    """Serves all three sites by host and path, with a login session per host."""

    def __init__(self, pages: dict[str, dict] | None = None, *, accept_login: bool = True):
        self.pages = {host: {"/": "<html><body>{chrome}</body></html>", **paths} for host, paths in PAGES.items()}
        for host, paths in (pages or {}).items():
            self.pages[host].update(paths)
        self.accept_login = accept_login
        self.logged_in: set[str] = set()
        self.requests: list[tuple[str, str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.requests.append((request.method, host, path))
        if host not in self.pages:
            raise httpx.ConnectError("no such host", request=request)
        if request.method == "POST" and path == "/login":
            if self.accept_login:
                self.logged_in.add(host)
            return httpx.Response(200, text="")
        page = self.pages[host].get(path)
        if page is None:
            return httpx.Response(404, text="<html><head><title>404 Nicht gefunden</title></head></html>")
        status, html = page if isinstance(page, tuple) else (200, page)
        logged_in = host in self.logged_in
        html = html.replace("{chrome}", CHROME[host] if logged_in else "")
        html = html.replace("{controls}", CONTROLS[host] if logged_in else "")
        return httpx.Response(status, text=html)


def run(sites: FakeSites, *, credentials: bool = False) -> dict[str, site_check.Result]:
    transport = httpx.MockTransport(sites)
    real_client = httpx.AsyncClient

    class RoutedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs.pop("http2", None)
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    value = "x@example.org" if credentials else ""
    with (
        mock.patch.object(httpx, "AsyncClient", RoutedClient),
        mock.patch.dict(CREDENTIALS["sto"], {"email": value, "password": value}),
        mock.patch.dict(CREDENTIALS["aniworld"], {"email": value, "password": value}),
        mock.patch.dict(CREDENTIALS["bs"], {"username": value, "password": value}),
    ):
        results = asyncio.run(site_check.run_checks())
    return {r.check: r for r in results}


def statuses(results: dict[str, site_check.Result]) -> dict[str, str]:
    return {name: r.status for name, r in results.items()}


def failed(results: dict[str, site_check.Result]) -> list[str]:
    return [name for name, r in results.items() if r.status == FAIL]


class HealthySitesTests(unittest.TestCase):
    def test_every_public_check_passes_and_the_logged_in_ones_are_skipped(self):
        results = run(FakeSites())
        expected = {}
        for host in (ANI, BS, STO):
            expected.update(
                {
                    f"{host}: login page": PASS,
                    f"{host}: series page": PASS,
                    f"{host}: season page": PASS,
                    f"{host}: logged-in checks": SKIPPED,
                }
            )
        self.assertEqual(statuses(results), expected)
        self.assertEqual(site_check.exit_code(list(results.values())), 0)

    def test_with_credentials_every_logged_in_check_passes(self):
        results = run(FakeSites(), credentials=True)
        self.assertEqual(failed(results), [])
        for check in (f"{STO}: login", f"{STO}: subscribe control", f"{STO}: mark controls"):
            self.assertEqual(results[check].status, PASS, check)
        self.assertEqual(results[f"{ANI}: mark controls"].status, PASS)
        self.assertEqual(results[f"{BS}: login"].status, PASS)
        self.assertEqual(results[f"{BS}: mark controls"].status, SKIPPED)

    def test_nothing_is_ever_marked_or_subscribed(self):
        sites = FakeSites()
        run(sites, credentials=True)
        posts = [(host, path) for method, host, path in sites.requests if method != "GET"]
        self.assertEqual(sorted(posts), sorted((host, "/login") for host in (STO, ANI, BS)))
        marks = [path for _m, _h, path in sites.requests if "/des/" in path or "mark" in path or "/ajax/" in path]
        self.assertEqual(marks, [])

    def test_the_report_never_carries_the_account_name(self):
        results = run(FakeSites(), credentials=True)
        report = site_check.render(list(results.values()), date(2026, 10, 3))
        self.assertNotIn(ACCOUNT_NAME, report)
        self.assertNotIn("x@example.org", report)


class LayoutChangeTests(unittest.TestCase):
    """Each change watchmaker depends on fails its own check, and only that one."""

    def assert_only_failure(self, results, check: str) -> None:
        self.assertEqual(failed(results), [check])
        self.assertEqual(site_check.exit_code(list(results.values())), 1)

    def test_a_login_page_that_lost_its_token_field(self):
        page = PAGES[BS]["/login"].replace('name="security_token"', 'name="token"')
        self.assert_only_failure(run(FakeSites({BS: {"/login": page}})), f"{BS}: login page")

    def test_a_login_page_without_a_password_field(self):
        page = "<html><body><p>Anmelden</p></body></html>"
        self.assert_only_failure(run(FakeSites({ANI: {"/login": page}})), f"{ANI}: login page")

    def test_a_series_page_without_season_navigation(self):
        page = PAGES[STO][STO_SERIES].replace('id="season-nav"', 'id="seasons-v2"').replace("staffel-", "season/")
        self.assert_only_failure(run(FakeSites({STO: {STO_SERIES: page}})), f"{STO}: series page")

    def test_a_season_page_without_episode_rows(self):
        page = "<html><body><div class='episodes-grid'><div>1</div></div></body></html>"
        results = run(FakeSites({ANI: {f"{ANI_SERIES}/staffel-1": page}}))
        self.assert_only_failure(results, f"{ANI}: season page")

    def test_a_season_page_without_the_sto_mark_control(self):
        page = PAGES[STO][f"{STO_SERIES}/staffel-1"].replace("{controls}", '<meta name="csrf-token" content="t">')
        results = run(FakeSites({STO: {f"{STO_SERIES}/staffel-1": page}}), credentials=True)
        self.assert_only_failure(results, f"{STO}: mark controls")

    def test_a_season_page_without_the_aniworld_series_id(self):
        page = PAGES[ANI][f"{ANI_SERIES}/staffel-1"].replace(
            "{controls}", '<span class="clearAllEpisodesFromThisSeason" data-season-id="7"></span>'
        )
        results = run(FakeSites({ANI: {f"{ANI_SERIES}/staffel-1": page}}), credentials=True)
        self.assert_only_failure(results, f"{ANI}: mark controls")

    def test_a_series_page_without_the_subscribe_button(self):
        page = PAGES[STO][STO_SERIES].replace("{controls}", "")
        results = run(FakeSites({STO: {STO_SERIES: page}}), credentials=True)
        self.assert_only_failure(results, f"{STO}: subscribe control")

    def test_a_rejected_login(self):
        results = run(FakeSites(accept_login=False), credentials=True)
        self.assertEqual(sorted(failed(results)), sorted(f"{host}: login" for host in (ANI, BS, STO)))


class ProbeSeriesTests(unittest.TestCase):
    def test_the_home_page_supplies_probes_when_every_fixed_one_has_gone(self):
        sites = FakeSites()
        del sites.pages[BS][BS_SERIES]
        other = "/serie/Dark"
        sites.pages[BS]["/"] = '<html><body><a href="serie/Dark">Dark</a></body></html>'
        sites.pages[BS][other] = PAGES[BS][BS_SERIES].replace("Breaking-Bad", "Dark")
        sites.pages[BS][f"{other}/1"] = PAGES[BS][f"{BS_SERIES}/1"]
        results = run(sites)
        self.assertEqual(results[f"{BS}: series page"].status, PASS)
        self.assertTrue(results[f"{BS}: series page"].detail.startswith("Dark"))

    def test_no_series_page_anywhere_is_a_failure(self):
        sites = FakeSites()
        del sites.pages[ANI][ANI_SERIES]
        self.assertEqual(run(sites)[f"{ANI}: series page"].status, FAIL)


class UnreachableTests(unittest.TestCase):
    def test_one_blocked_site_is_unreachable_and_the_others_still_checked(self):
        results = run(FakeSites({STO: {"/login": (403, CHALLENGE)}}))
        self.assertEqual(results["sto (no reachable mirror): login page"].status, UNREACHABLE)
        self.assertEqual(results[f"{ANI}: season page"].status, PASS)
        self.assertEqual(site_check.exit_code(list(results.values())), 2)

    def test_a_down_primary_falls_back_to_the_next_mirror(self):
        sites = FakeSites()
        sites.pages["aniworld.cc"] = sites.pages.pop(ANI)
        results = run(sites)
        self.assertEqual(results["aniworld.cc: season page"].status, PASS)

    def test_a_bare_ip_mirror_is_never_used(self):
        # The logged-in checks would send the password over plain http.
        sites = FakeSites()
        sites.pages["186.2.175.5"] = sites.pages.pop(STO)
        results = run(sites)
        self.assertEqual(results["sto (no reachable mirror): login page"].status, UNREACHABLE)
        self.assertNotIn("186.2.175.5", {host for _m, host, _p in sites.requests})


class DryRunWorkerTests(unittest.TestCase):
    def test_it_has_no_way_to_send_anything(self):
        worker = site_check.DryRunWorker(STO)
        self.assertIsNone(worker.client)
        with self.assertRaises(AssertionError):
            asyncio.run(worker._get_soup(f"https://{STO}/serie/x/staffel-1"))


class MainTests(unittest.TestCase):
    def test_the_report_file_and_exit_code(self):
        result = site_check.Result("serienstream.to: login page", FAIL, "a | b")
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(site_check, "run_checks", mock.AsyncMock(return_value=[result])),
            mock.patch("builtins.print"),
        ):
            report = Path(tmp, "report.md")
            self.assertEqual(site_check.main_cli(["--report", str(report)]), 1)
            text = report.read_text(encoding="utf-8")
        self.assertIn("a / b", text)  # a pipe would split the table cell

    def test_a_crash_is_reported_as_one_not_as_a_failed_check(self):
        with (
            mock.patch.object(site_check, "run_checks", mock.AsyncMock(side_effect=KeyError("boom"))),
            mock.patch("builtins.print"),
            mock.patch("traceback.print_exc"),
        ):
            self.assertEqual(site_check.main_cli([]), 3)


if __name__ == "__main__":
    unittest.main()
