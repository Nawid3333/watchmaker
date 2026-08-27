"""Unit tests for watchmaker's pure logic.

Run with:  python -m unittest discover -s tests

These cover the parts where a silent mistake would corrupt data or report a
wrong result: URL classification, batch-file rewriting, season discovery,
episode counting, and the post-mark verification.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

import main  # noqa: E402

# main.py prints box-drawing characters and arrows. main() calls
# _configure_console() before any of that; the tests call the printing
# functions directly, so without this the whole suite errors out on a default
# Windows console (cp1252) with UnicodeEncodeError -- a failure about the
# terminal, not about the code under test.
main._configure_console()
from config import SUPPORTED_DOMAINS  # noqa: E402
from main import (  # noqa: E402
    ACTION_UNWATCHED,
    ACTION_WATCHED,
    DomainWorker,
    SeasonOutcome,
    SeriesResult,
    _append_lines,
    _atomic_write,
    _check_error_page,
    _clean_title,
    _read_lines,
    _rewrite_batch_urls,
    _url_for_host,
    classify_url,
    is_utility_page_title,
    load_url_batches,
    slug_for,
)


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class TempFileCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def write(self, name: str, content: str) -> str:
        path = os.path.join(self.dir.name, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return path


# ==================== URL parsing ====================
class TestUrlParsing(unittest.TestCase):
    def test_classify_known_hosts(self):
        self.assertEqual(
            classify_url("https://aniworld.to/anime/stream/naruto/staffel-2"),
            ("aniworld.to", "aniworld", "naruto"),
        )
        self.assertEqual(
            classify_url("https://serienstream.to/serie/don-matteo"),
            ("serienstream.to", "sto", "don-matteo"),
        )
        self.assertEqual(
            classify_url("https://burningseries.ac/serie/The-Divorce-Insurance"),
            ("burningseries.ac", "bs", "The-Divorce-Insurance"),
        )

    def test_classify_strips_www(self):
        result = classify_url("https://www.aniworld.to/anime/stream/x")
        self.assertIsNotNone(result)
        assert result is not None  # narrows for the type checker
        self.assertEqual(result[0], "aniworld.to")

    def test_classify_rejects_unknown_and_slugless(self):
        self.assertIsNone(classify_url("https://example.com/serie/x"))
        self.assertIsNone(classify_url("https://serienstream.to/"))
        self.assertIsNone(classify_url("not-a-url"))

    def test_slug_for_falls_back_to_path_split(self):
        self.assertEqual(slug_for("https://unknown.tld/serie/foo/staffel-1", "sto"), "foo")
        with self.assertRaises(ValueError):
            slug_for("https://unknown.tld/nothing-here", "sto")

    def test_url_for_host_uses_target_scheme(self):
        # IP mirrors are http-only; domains must come back as https even when
        # the source URL was the http IP mirror.
        self.assertEqual(
            _url_for_host("http://186.2.175.5/serie/foo", "serienstream.to"),
            "https://serienstream.to/serie/foo",
        )
        self.assertEqual(
            _url_for_host("https://serienstream.to/serie/foo", "186.2.175.5"),
            "http://186.2.175.5/serie/foo",
        )


class TestBatchLoading(TempFileCase):
    def test_comments_blanks_and_rejects(self):
        path = self.write(
            "b.txt",
            "\n".join(
                [
                    "# a comment",
                    "",
                    "https://serienstream.to/serie/alpha",
                    "ftp://serienstream.to/serie/beta",
                    "https://example.com/serie/gamma",
                ]
            )
            + "\n",
        )
        grouped, rejected = load_url_batches(path)
        self.assertEqual(grouped, {"serienstream.to": ["https://serienstream.to/serie/alpha"]})
        self.assertEqual([r["reason"] for r in rejected], ["missing http(s)://", "unsupported host: example.com"])

    def test_same_series_twice_is_collapsed(self):
        # Both URLs mark the whole series, so processing both is pure waste.
        path = self.write(
            "b.txt",
            "https://serienstream.to/serie/alpha\nhttps://serienstream.to/serie/alpha/staffel-3\n",
        )
        grouped, _ = load_url_batches(path)
        self.assertEqual(grouped["serienstream.to"], ["https://serienstream.to/serie/alpha"])

    def test_hosts_come_back_in_domain_order(self):
        path = self.write(
            "b.txt",
            "https://serienstream.to/serie/a\nhttps://aniworld.to/anime/stream/b\n",
        )
        grouped, _ = load_url_batches(path)
        self.assertEqual(list(grouped), ["aniworld.to", "serienstream.to"])


class TestFileWriting(TempFileCase):
    def test_append_repairs_missing_trailing_newline(self):
        path = self.write("l.txt", "https://serienstream.to/serie/a")  # no trailing \n
        _append_lines(path, ["https://serienstream.to/serie/b"])
        self.assertEqual(
            _read_lines(path),
            ["https://serienstream.to/serie/a", "https://serienstream.to/serie/b"],
        )

    def test_append_to_missing_file(self):
        path = os.path.join(self.dir.name, "new.txt")
        _append_lines(path, ["x"])
        self.assertEqual(_read_lines(path), ["x"])

    def test_rewrite_preserves_comments_and_unknown_lines(self):
        path = self.write(
            "b.txt",
            "# keep me\n\nhttps://serienstream.to/serie/a\nhttps://example.com/serie/z\n",
        )
        changed = _rewrite_batch_urls(path, {"https://serienstream.to/serie/a": "http://186.2.175.5/serie/a"})
        self.assertTrue(changed)
        self.assertEqual(
            _read_lines(path),
            ["# keep me", "", "http://186.2.175.5/serie/a", "https://example.com/serie/z"],
        )

    def test_rewrite_is_a_noop_without_matches(self):
        path = self.write("b.txt", "https://serienstream.to/serie/a\n")
        self.assertFalse(_rewrite_batch_urls(path, {"https://other/serie/b": "https://x/serie/b"}))

    def test_atomic_write_replaces_content(self):
        path = self.write("f.txt", "old")
        _atomic_write(path, "new")
        self.assertEqual(_read_lines(path), ["new"])
        # No temp files left behind.
        self.assertEqual([n for n in os.listdir(self.dir.name) if n.startswith(".tmp-")], [])


# ==================== HTML parsing ====================
ANIWORLD_SERIES = """
<div class="add-series" data-series-id="42" data-series-favourite="1" data-series-watchlist="0"></div>
<div id="stream"><ul>
  <li><a href="/anime/stream/x/staffel-1">1</a></li>
  <li><a href="/anime/stream/x/staffel-2">2</a></li>
  <li><a href="/anime/stream/x/filme">Filme</a></li>
</ul></div>
<h1 itemprop="name"><span>Naruto</span></h1>
"""

BS_SERIES = """
<div id="seasons"><a href="/serie/Foo/1">1</a><a href="/serie/Foo/2">2</a></div>
<select name="language"><option value="99">Deutsch</option></select>
<h1 class="fw-bold">Foo</h1>
"""

STO_SERIES = """
<div id="season-nav">
  <a data-season-pill="1" href="/serie/foo/staffel-1">1</a>
  <a data-season-pill="2" href="/serie/foo/staffel-2">2</a>
</div>
<div data-season-id="77"></div>
<h1 class="fw-bold">Foo</h1>
"""


class TestSeasonDiscovery(unittest.TestCase):
    def test_aniworld_seasons_and_movies(self):
        w = DomainWorker("aniworld.to")
        self.assertEqual(w.discover_seasons(soup(ANIWORLD_SERIES), "x"), [1, 2, "Filme"])

    def test_bs_ignores_unrelated_numeric_options(self):
        # Regression: the <option value="99"> fallback used to run even when the
        # #seasons nav had already been parsed, inventing a season 99.
        w = DomainWorker("burningseries.ac")
        self.assertEqual(w.discover_seasons(soup(BS_SERIES), "Foo"), [1, 2])

    def test_bs_option_fallback_still_works(self):
        w = DomainWorker("burningseries.ac")
        html = '<select><option value="1">1</option><option value="2">2</option></select>'
        self.assertEqual(w.discover_seasons(soup(html), "Foo"), [1, 2])

    def test_sto_ignores_stray_season_ids(self):
        w = DomainWorker("serienstream.to")
        self.assertEqual(w.discover_seasons(soup(STO_SERIES), "foo"), [1, 2])

    def test_sto_data_season_id_last_resort(self):
        # select() actually matches attribute selectors; find_all() never did.
        w = DomainWorker("serienstream.to")
        self.assertEqual(w.discover_seasons(soup('<div data-season-id="3"></div>'), "foo"), [3])

    def test_empty_page_defaults_to_season_one(self):
        w = DomainWorker("serienstream.to")
        self.assertEqual(w.discover_seasons(soup("<html></html>"), "foo"), [1])

    def test_sto_href_fallback_is_scoped_to_the_slug(self):
        w = DomainWorker("serienstream.to")
        html = '<a href="/serie/foo/staffel-4">4</a><a href="/serie/other/staffel-9">9</a>'
        self.assertEqual(w.discover_seasons(soup(html), "foo"), [4])


class TestEpisodeCounting(unittest.TestCase):
    def test_aniworld_rows(self):
        w = DomainWorker("aniworld.to")
        html = """<table class="seasonEpisodesList"><tbody>
            <tr data-episode-id="1" class="seen"></tr>
            <tr data-episode-id="2"></tr>
            <tr data-episode-id="3" class="watched"></tr>
        </tbody></table>"""
        self.assertEqual(w._count_episodes(soup(html)), (2, 3))

    def test_sto_rows_with_data_attribute(self):
        w = DomainWorker("serienstream.to")
        html = """<table class="episode-table"><tbody>
            <tr class="episode-row seen"></tr>
            <tr class="episode-row"></tr>
            <tr class="episode-row" data-watched="1"></tr>
        </tbody></table>"""
        self.assertEqual(w._count_episodes(soup(html)), (2, 3))

    def test_no_rows_reports_zero_total(self):
        w = DomainWorker("burningseries.ac")
        self.assertEqual(w._count_episodes(soup("<html></html>")), (0, 0))


class TestPageDetails(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(DomainWorker._extract_title(soup(ANIWORLD_SERIES), "aniworld"), "Naruto")
        self.assertEqual(DomainWorker._extract_title(soup(BS_SERIES), "bs"), "Foo")

    def test_title_from_og_meta_strips_season(self):
        html = '<meta property="og:title" content="Dark Staffel 2 online sehen">'
        self.assertEqual(DomainWorker._extract_title(soup(html), "sto"), "Dark")

    def test_bs_h2_beats_polluted_og_title(self):
        # bs.to has no h1 and its og:title carries the whole site suffix, so the
        # <h2> ("<name> Staffel N") must win.
        html = (
            "<h2>The Divorce Insurance Staffel 1</h2>"
            '<meta property="og:title" content="The Divorce Insurance (1) - Burning Series: Serien online sehen">'
        )
        self.assertEqual(DomainWorker._extract_title(soup(html), "bs"), "The Divorce Insurance")

    def test_inline_small_tag_is_not_glued_to_the_title(self):
        # Real bs.to markup: "<h2>Harry Potter<small>Specials</small></h2>".
        # get_text(strip=True) used to yield "Harry PotterSpecials".
        html = """<h2>
		Harry Potter
			<small>Specials</small>
</h2>"""
        self.assertEqual(DomainWorker._extract_title(soup(html), "bs"), "Harry Potter")

    def test_inline_small_season_marker(self):
        html = """<h2>
		The Divorce Insurance
			<small>Staffel 1</small>
</h2>"""
        self.assertEqual(DomainWorker._extract_title(soup(html), "bs"), "The Divorce Insurance")

    def test_extract_title_does_not_mutate_the_soup(self):
        # The soup is shared with season discovery and the subscribe check.
        page = soup(BS_SERIES)
        before = str(page)
        DomainWorker._extract_title(page, "bs")
        self.assertEqual(str(page), before)

    def test_utility_page_titles_are_recognised(self):
        # A retired/mistyped slug is answered with the catalogue page at HTTP 200.
        self.assertTrue(is_utility_page_title("Alle Serien"))
        self.assertTrue(is_utility_page_title("  andere serien "))
        self.assertFalse(is_utility_page_title("Don Matteo"))
        self.assertFalse(is_utility_page_title(None))

    def test_clean_title_cases(self):
        self.assertEqual(_clean_title("The Divorce Insurance (1) - Burning Series: x"), "The Divorce Insurance")
        self.assertEqual(_clean_title("Don Matteo"), "Don Matteo")
        self.assertEqual(_clean_title("Naruto Staffel 12"), "Naruto")
        self.assertEqual(_clean_title("Harry Potter Specials"), "Harry Potter")
        self.assertEqual(_clean_title("Some Show Season 3"), "Some Show")
        self.assertIsNone(_clean_title(""))

    def test_aniworld_subscription_flags(self):
        w = DomainWorker("aniworld.to")
        self.assertEqual(w._detect_subscription_status(soup(ANIWORLD_SERIES)), (True, False))

    def test_sto_subscription_flags(self):
        w = DomainWorker("serienstream.to")
        html = """
        <a class="js-action-btn btn-glass-primary" data-type="favorite" data-url="/fav"></a>
        <a class="js-action-btn" data-type="watchlater" data-url="/wl"></a>
        """
        self.assertEqual(w._detect_subscription_status(soup(html)), (True, False))

    def test_bs_has_no_subscription_controls(self):
        self.assertEqual(DomainWorker("burningseries.ac")._detect_subscription_status(soup(BS_SERIES)), (None, None))

    def test_error_page_detection(self):
        self.assertEqual(_check_error_page(soup("<title>404 Not Found</title>"), "sto"), "404")
        self.assertEqual(_check_error_page(soup("<title>Fehler 502</title>"), "bs"), "502")
        self.assertEqual(_check_error_page(soup("<h2>503</h2>"), "aniworld"), "503")
        self.assertEqual(_check_error_page(soup("<p>Seite nicht gefunden</p>"), "sto"), "404")

    def test_real_page_is_never_an_error_page(self):
        # A valid series page keeps its season nav even if a heading looks odd.
        self.assertIsNone(_check_error_page(soup(BS_SERIES + "<title>404</title>"), "bs"))
        self.assertIsNone(_check_error_page(soup(STO_SERIES), "sto"))
        self.assertIsNone(_check_error_page(soup(ANIWORLD_SERIES), "aniworld"))

    def test_login_markers(self):
        w = DomainWorker("aniworld.to")
        self.assertTrue(w._is_logged_in(soup('<div class="avatar"><a href="/user/profil/me"></a></div>')))
        self.assertFalse(w._is_logged_in(soup("<div></div>")))


# ==================== result reporting ====================
class TestSeriesResult(unittest.TestCase):
    def _result(self, action, before, after, total=10):
        return SeriesResult(
            "serienstream.to",
            "sto",
            "https://serienstream.to/serie/x",
            "x",
            action=action,
            seasons=[SeasonOutcome(season=1, total=total, watched_before=before, watched_after=after)],
        )

    def test_unwatch_success_is_not_reported_as_failure(self):
        # Regression: status used to be `watched == total`, so a fully
        # successful unwatch run showed ✗ on every single series.
        r = self._result(ACTION_UNWATCHED, before=10, after=0)
        self.assertTrue(r.at_target)
        self.assertTrue(r.line().startswith("✓"))

    def test_partial_watch_is_a_failure(self):
        r = self._result(ACTION_WATCHED, before=0, after=7)
        self.assertFalse(r.at_target)
        self.assertTrue(r.line().startswith("✗"))

    def test_full_watch_is_a_success(self):
        self.assertTrue(self._result(ACTION_WATCHED, before=0, after=10).at_target)

    def test_series_with_no_seasons_is_never_at_target(self):
        r = SeriesResult("h", "sto", "u", "s", action=ACTION_WATCHED)
        self.assertFalse(r.at_target)

    def test_detail_lines_only_show_changes(self):
        r = SeriesResult(
            "h",
            "sto",
            "u",
            "s",
            action=ACTION_WATCHED,
            seasons=[
                SeasonOutcome(season=1, total=5, watched_before=5, watched_after=5),
                SeasonOutcome(season=2, total=5, watched_before=1, watched_after=5),
            ],
        )
        self.assertEqual(r.detail_lines(), ["▶S2: 1/5 -> 5/5"])


# ==================== marking + verification ====================
class FakeWorker(DomainWorker):
    """DomainWorker with the network replaced by a scripted page sequence."""

    def __init__(self, host, pages, mark_effect=None):
        super().__init__(host)
        self.pages = list(pages)
        self.mark_effect = mark_effect
        self.marks = []
        self.logged_in = True

    async def _get_soup(self, url):
        return soup(self.pages.pop(0))

    async def _issue_mark(self, soup, season_url, slug, season, action):
        self.marks.append((slug, season, action))
        if self.mark_effect:
            self.mark_effect()


def episodes(total, watched):
    rows = "".join(f'<tr class="episode-row{" seen" if i < watched else ""}"></tr>' for i in range(total))
    return f'<table class="episode-table"><tbody>{rows}</tbody></table>'


class TestMarkSeason(unittest.IsolatedAsyncioTestCase):
    async def test_successful_mark_is_verified(self):
        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 5)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual((outcome.watched_before, outcome.watched_after, outcome.total), (0, 5, 5))
        self.assertEqual(w.marks, [("x", 1, ACTION_WATCHED)])

    async def test_mark_that_silently_did_nothing_is_a_failure(self):
        # The sites answer 200 even when nothing changed; only re-reading the
        # page proves the mark landed.
        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 0)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertIn("expected 5/5", outcome.note)

    async def test_partial_mark_is_a_failure(self):
        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 3)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)

    async def test_unwatch_is_verified_against_zero(self):
        w = FakeWorker("serienstream.to", [episodes(5, 5), episodes(5, 0)])
        outcome = await w.mark_season("x", 1, ACTION_UNWATCHED)
        self.assertTrue(outcome.ok)

    async def test_already_at_target_skips_the_request_but_still_verifies(self):
        w = FakeWorker("serienstream.to", [episodes(5, 5), episodes(5, 5)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual(w.marks, [])

    async def test_skipped_mark_that_fails_verification_is_reported(self):
        w = FakeWorker("serienstream.to", [episodes(5, 5), episodes(5, 2)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)

    async def test_page_without_episodes_is_never_reported_as_success(self):
        # Used to report OK: total 0 meant "nothing to do" and verification
        # was skipped, so a broken/changed page looked like a clean run.
        w = FakeWorker("serienstream.to", ["<html></html>"])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.note, "no episodes found")
        self.assertEqual(w.marks, [])

    async def test_unverifiable_result_is_a_failure(self):
        class Boom(FakeWorker):
            async def _get_soup(self, url):
                if not self.pages:
                    raise RuntimeError("error page 502")
                return soup(self.pages.pop(0))

        w = Boom("serienstream.to", [episodes(5, 0)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertIn("unverified", outcome.note)

    async def test_load_failure_is_a_failure(self):
        class Boom(FakeWorker):
            async def _get_soup(self, url):
                raise RuntimeError("error page 404")

        outcome = await Boom("serienstream.to", []).mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertIn("load failed", outcome.note)

    async def test_expired_session_triggers_one_retry(self):
        calls = {"n": 0}

        class Expiring(FakeWorker):
            async def _issue_mark(self, soup, season_url, slug, season, action):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise main.ControlMissingError("No CSRF token")
                self.marks.append((slug, season, action))

            async def _recover_session(self):
                return True

        w = Expiring("serienstream.to", [episodes(5, 0), episodes(5, 0), episodes(5, 5)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual(calls["n"], 2)

    async def test_missing_control_on_a_valid_session_is_not_retried(self):
        class Missing(FakeWorker):
            async def _issue_mark(self, soup, season_url, slug, season, action):
                raise main.ControlMissingError("No #season-mark control")

            async def _recover_session(self):
                return False

        w = Missing("serienstream.to", [episodes(5, 0)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertIn("season-mark", outcome.note)


class TestSeasonUrls(unittest.TestCase):
    def test_per_family_url_shapes(self):
        self.assertEqual(
            DomainWorker("aniworld.to").season_url("naruto", 2),
            "https://aniworld.to/anime/stream/naruto/staffel-2",
        )
        self.assertEqual(
            DomainWorker("aniworld.to").season_url("naruto", "Filme"),
            "https://aniworld.to/anime/stream/naruto/filme",
        )
        self.assertEqual(
            DomainWorker("serienstream.to").season_url("foo", 3),
            "https://serienstream.to/serie/foo/staffel-3",
        )
        self.assertEqual(
            DomainWorker("burningseries.ac").season_url("Foo", 3),
            "https://burningseries.ac/serie/Foo/3",
        )

    def test_ip_hosts_use_http(self):
        self.assertEqual(
            DomainWorker("186.2.175.5").season_url("foo", 1),
            "http://186.2.175.5/serie/foo/staffel-1",
        )


# ==================== Host reachability ====================
class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeClient:
    """Stands in for httpx.AsyncClient and records what was fetched."""

    def __init__(self, response=None, exc=None) -> None:
        self._response = response
        self._exc = exc
        self.requested: list[str] = []

    async def get(self, url, **kwargs):
        self.requested.append(str(url))
        if self._exc is not None:
            raise self._exc
        return self._response


LOGIN_PAGE = '<html><form><input type="password" name="pass"></form></html>'
PARKED_PAGE = "<html><body><h1>Domain for sale</h1></body></html>"


class TestLoginPageDetection(unittest.TestCase):
    """A host is only usable if it really is the site.

    The check used to be a HEAD of the homepage accepting any status under
    400, which a parked domain or a proxy error page passes exactly as
    happily as the real thing.
    """

    def test_a_password_field_identifies_a_login_page(self):
        for html in (
            '<input type="password">',
            "<input type='password'>",
            "<input type=password>",
            LOGIN_PAGE,
        ):
            with self.subTest(html):
                self.assertTrue(main._looks_like_login_page(html))

    def test_wording_alone_is_enough_in_either_language(self):
        self.assertTrue(main._looks_like_login_page("<body>Bitte anmelden</body>"))
        self.assertTrue(main._looks_like_login_page("<body>Please Login</body>"))

    def test_a_page_that_is_not_a_login_page_is_rejected(self):
        for html in ("", PARKED_PAGE, "<body>502 Bad Gateway</body>", "not markup"):
            with self.subTest(html):
                self.assertFalse(main._looks_like_login_page(html))


class TestCheckHost(unittest.IsolatedAsyncioTestCase):
    async def test_a_working_host_is_probed_on_its_login_page(self):
        client = _FakeClient(_FakeResponse(200, LOGIN_PAGE))
        ok, reason = await main.check_host(client, "aniworld.to")

        self.assertTrue(ok)
        self.assertEqual(reason, "GET 200")
        # The very URL _login_form posts to, so the probe tests what matters.
        self.assertEqual(client.requested, ["https://aniworld.to/login"])

    async def test_a_host_that_answers_but_has_no_login_form_is_unusable(self):
        client = _FakeClient(_FakeResponse(200, PARKED_PAGE))
        ok, reason = await main.check_host(client, "aniworld.to")

        self.assertFalse(ok)
        self.assertEqual(reason, "no login form")

    async def test_an_error_status_is_unusable(self):
        client = _FakeClient(_FakeResponse(503, LOGIN_PAGE))
        ok, reason = await main.check_host(client, "aniworld.to")

        self.assertFalse(ok)
        self.assertEqual(reason, "GET 503")

    async def test_a_timeout_is_unusable(self):
        client = _FakeClient(exc=httpx.TimeoutException("slow"))
        ok, reason = await main.check_host(client, "aniworld.to")

        self.assertFalse(ok)
        self.assertEqual(reason, "timeout")

    async def test_a_raw_ip_host_is_probed_over_http(self):
        client = _FakeClient(_FakeResponse(200, LOGIN_PAGE))
        await main.check_host(client, "186.2.175.5")

        self.assertEqual(client.requested, ["http://186.2.175.5/login"])


class TestActiveHostResolution(unittest.IsolatedAsyncioTestCase):
    """Which mirror ends up in the batch file on disk.

    resolve_active_hosts rewrites the user's series_urls.txt to the host it
    picks, before any login is attempted. A host that answers but cannot serve
    a login page must therefore never be picked while a working mirror exists,
    or the bad mirror is baked into the file for every later run too.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _batch(self, *urls: str) -> str:
        path = os.path.join(self._dir.name, "series_urls.txt")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(url + "\n" for url in urls))
        return path

    @staticmethod
    def _statuses(usable: set[str]):
        async def fake_check_hosts(hosts):
            return {h: ("OK (GET 200)" if h in usable else "FAIL (no login form)") for h in hosts}

        return fake_check_hosts

    async def test_an_unusable_mirror_is_not_written_into_the_batch_file(self):
        path = self._batch("https://aniworld.to/anime/stream/demo")

        with mock.patch.object(main, "check_hosts", self._statuses({"aniworld.cc"})):
            _resolved, _statuses, active = await main.resolve_active_hosts(path)

        self.assertEqual(active.get("aniworld"), "aniworld.cc")
        self.assertEqual(
            _read_lines(path),
            ["https://aniworld.cc/anime/stream/demo"],
            "the batch file must point at the usable mirror",
        )

    async def test_a_usable_first_choice_is_left_alone(self):
        path = self._batch("https://aniworld.to/anime/stream/demo")

        with mock.patch.object(main, "check_hosts", self._statuses({"aniworld.to", "aniworld.cc"})):
            _resolved, _statuses, active = await main.resolve_active_hosts(path)

        self.assertEqual(active.get("aniworld"), "aniworld.to")
        self.assertEqual(_read_lines(path), ["https://aniworld.to/anime/stream/demo"])

    async def test_a_family_with_no_usable_mirror_is_skipped_not_rewritten(self):
        path = self._batch("https://aniworld.to/anime/stream/demo")

        with mock.patch.object(main, "check_hosts", self._statuses(set())):
            resolved, statuses, active = await main.resolve_active_hosts(path)

        self.assertNotIn("aniworld", active)
        self.assertEqual(resolved, {})
        self.assertIn("no reachable aniworld mirror", statuses["aniworld.to"])
        self.assertEqual(
            _read_lines(path),
            ["https://aniworld.to/anime/stream/demo"],
            "nothing to migrate to means the file must be left untouched",
        )


# ==================== login verification ====================
# The logged-in bs homepage carries the logout link in section.navigation,
# which is exactly what _LOGIN_MARKERS["bs"] selects. The username here is a
# placeholder: the structure is what is being pinned.
BS_LOGGED_IN_HOME = """
<html><body>
  <section class="navigation">
    <div>Hallo<strong>ExampleUser</strong>!</div>
    <a href="settings">Einstellungen</a>
    <a href="messages">Nachrichten</a>
    <a href="logout">Logout</a>
  </section>
</body></html>
"""

BS_LOGGED_OUT_HOME = """
<html><body>
  <section class="navigation">
    <a href="login">Login</a>
    <a href="register">Registrieren</a>
  </section>
</body></html>
"""


class LoginRecordingWorker(DomainWorker):
    """DomainWorker with the network replaced, recording every URL fetched."""

    def __init__(self, host, verify_page, login_page='<input name="security_token" value="t">'):
        super().__init__(host)
        # Never let the real .env credentials near a test.
        self.creds = {"username": "u", "password": "p", "email": "u@example.test"}
        self.verify_page = verify_page
        self.login_page = login_page
        self.fetched = []

    async def _get_soup(self, url):
        self.fetched.append(url)
        return soup(self.login_page if url.endswith("/login") else self.verify_page)

    async def _post(self, url, **kwargs):
        return _FakeResponse(200, "")


class TestLoginStateDetection(unittest.TestCase):
    def test_the_bs_homepage_navigation_shows_a_logged_in_session(self):
        worker = LoginRecordingWorker("burningseries.ac", BS_LOGGED_IN_HOME)
        self.assertTrue(worker._is_logged_in(soup(BS_LOGGED_IN_HOME)))

    def test_a_logout_link_outside_the_navigation_still_counts(self):
        """The documented fallback for layouts that move the link."""
        worker = LoginRecordingWorker("burningseries.ac", BS_LOGGED_IN_HOME)
        stray = '<html><body><div><a href="logout">Logout</a></div></body></html>'
        self.assertTrue(worker._is_logged_in(soup(stray)))

    def test_a_logged_out_homepage_is_not_mistaken_for_a_session(self):
        worker = LoginRecordingWorker("burningseries.ac", BS_LOGGED_OUT_HOME)
        self.assertFalse(worker._is_logged_in(soup(BS_LOGGED_OUT_HOME)))


class TestBsLoginVerification(unittest.IsolatedAsyncioTestCase):
    """Which page proves the bs login worked.

    It used to be /andere-serien -- the full series catalogue, ~1.3 MB pulled
    on every login just to find one anchor. The homepage shows the same
    section.navigation logout link in 29 KB, and _recover_session has always
    checked this family on the homepage, so verifying there makes the two
    agree instead of trusting different pages for the same fact.
    """

    async def test_the_login_is_verified_on_the_homepage(self):
        worker = LoginRecordingWorker("burningseries.ac", BS_LOGGED_IN_HOME)

        self.assertTrue(await worker._login_form())
        self.assertEqual(
            worker.fetched,
            ["https://burningseries.ac/login", "https://burningseries.ac"],
        )

    async def test_the_catalogue_page_is_never_downloaded_to_check_a_login(self):
        worker = LoginRecordingWorker("burningseries.ac", BS_LOGGED_IN_HOME)
        await worker._login_form()

        self.assertNotIn(
            "andere-serien",
            " ".join(worker.fetched),
            "a 1.3 MB catalogue page must not be fetched to look for a logout link",
        )

    async def test_a_failed_bs_login_is_still_reported_as_failed(self):
        """Cheaper verification must not become weaker verification."""
        worker = LoginRecordingWorker("burningseries.ac", BS_LOGGED_OUT_HOME)

        self.assertFalse(await worker._login_form())


# ==================== per-host flow ====================
class ScriptedWorker:
    """Stands in for DomainWorker, recording the order things happen in."""

    events: list[tuple] = []
    made: list["ScriptedWorker"] = []
    refuse_login: set[str] = set()

    def __init__(self, host):
        self.host = host
        self.family = SUPPORTED_DOMAINS.get(host, "?")
        self.logged_in = False
        self.closed = False
        self.needs_subscribe = False
        ScriptedWorker.made.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def login(self):
        ScriptedWorker.events.append(("login-start", self.host))
        await asyncio.sleep(0.02)
        ScriptedWorker.events.append(("login-end", self.host))
        if self.host in ScriptedWorker.refuse_login:
            return False
        self.logged_in = True
        return True

    def _result(self, url, action, slug):
        return SeriesResult(
            self.host,
            self.family,
            url,
            slug,
            action=action,
            seasons=[SeasonOutcome(season=1, total=5, watched_before=5, watched_after=5)],
            title=slug,
        )

    async def inspect_series(self, url, action):
        ScriptedWorker.events.append(("inspect", self.host, url))
        slug = url.rstrip("/").split("/")[-1]
        plan = main.SeriesPlan(url=url, host=self.host, family=self.family, slug=slug, seasons=[1], title=slug)
        return self._result(url, action, slug), plan

    # Marking has to contain a real await point or the tasks run straight
    # through in submission order and never interleave, which would make any
    # assertion about concurrency vacuous.
    mark_delay: float = 0

    async def mark_series(self, plan, action):
        ScriptedWorker.events.append(("mark", self.host, plan.slug))
        await asyncio.sleep(ScriptedWorker.mark_delay)
        ScriptedWorker.events.append(("mark-end", self.host, plan.slug))
        return self._result(plan.url, action, plan.slug)


class HostFlowCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ScriptedWorker.events = []
        ScriptedWorker.made = []
        ScriptedWorker.refuse_login = set()
        patcher = mock.patch.object(main, "DomainWorker", ScriptedWorker)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def starts_before_any(events, kind):
        """Index of the first event of *kind*, or len(events) if absent."""
        for i, ev in enumerate(events):
            if ev[0] == kind:
                return i
        return len(events)


class TestPreviewAcrossHosts(HostFlowCase):
    """Moving from one domain to the next used to cost a fresh TLS handshake
    plus a three-request login, paid one host at a time with nothing else
    happening. The hosts are separate servers, so those logins now overlap."""

    GROUPED = {
        "serienstream.to": ["https://serienstream.to/serie/one"],
        "burningseries.ac": ["https://burningseries.ac/serie/Two"],
    }

    async def test_the_logins_for_different_hosts_overlap(self):
        await main._preview(ACTION_WATCHED, dict(self.GROUPED))

        kinds = [e[0] for e in ScriptedWorker.events]
        # Ordering, not wall time, so this cannot go flaky: run one host at a
        # time and the first login-end lands before the second login-start.
        self.assertEqual(kinds[:2], ["login-start", "login-start"])

    async def test_no_series_is_inspected_before_every_host_is_logged_in(self):
        await main._preview(ACTION_WATCHED, dict(self.GROUPED))

        events = ScriptedWorker.events
        self.assertLess(
            max(i for i, e in enumerate(events) if e[0] == "login-end"),
            self.starts_before_any(events, "inspect"),
        )

    async def test_hosts_are_still_previewed_in_a_stable_sorted_order(self):
        await main._preview(ACTION_WATCHED, dict(self.GROUPED))

        inspected = [e[1] for e in ScriptedWorker.events if e[0] == "inspect"]
        self.assertEqual(inspected, sorted(self.GROUPED))

    async def test_a_host_that_cannot_log_in_does_not_stop_the_others(self):
        ScriptedWorker.refuse_login = {"burningseries.ac"}

        todo, done, broken = await main._preview(ACTION_WATCHED, dict(self.GROUPED))

        self.assertEqual([r.url for r in broken], ["https://burningseries.ac/serie/Two"])
        self.assertEqual([r.note for r in broken], ["login failed"])
        inspected = [e[1] for e in ScriptedWorker.events if e[0] == "inspect"]
        self.assertEqual(inspected, ["serienstream.to"])
        self.assertEqual(len(done), 1, "the working host is still previewed")
        self.assertEqual(todo, [])

    async def test_every_worker_is_closed(self):
        await main._preview(ACTION_WATCHED, dict(self.GROUPED))

        self.assertEqual(len(ScriptedWorker.made), 2)
        self.assertTrue(all(w.closed for w in ScriptedWorker.made))


class TestProcessBatchAcrossHosts(HostFlowCase):
    def setUp(self):
        super().setUp()
        # process_batch reconciles the shared failed-urls file; never let a
        # test write into the real data directory.
        patcher = mock.patch.object(main, "_persist_failed_urls", lambda *a, **kw: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _plans(self):
        return {
            "serienstream.to": [
                main.SeriesPlan("https://serienstream.to/serie/one", "serienstream.to", "sto", "one", [1], "one"),
            ],
            "burningseries.ac": [
                main.SeriesPlan("https://burningseries.ac/serie/Two", "burningseries.ac", "bs", "Two", [1], "Two"),
            ],
        }

    async def test_the_logins_overlap_before_any_marking_starts(self):
        await main.process_batch(ACTION_WATCHED, self._plans(), [])

        events = ScriptedWorker.events
        self.assertEqual([e[0] for e in events[:2]], ["login-start", "login-start"])
        self.assertLess(
            max(i for i, e in enumerate(events) if e[0] == "login-end"),
            self.starts_before_any(events, "mark"),
        )

    async def test_each_host_marks_one_series_at_a_time(self):
        """The guarantee that must survive running the hosts together: no
        single site ever sees two marks in flight from this run."""
        plans = self._plans()
        plans["serienstream.to"].append(
            main.SeriesPlan("https://serienstream.to/serie/three", "serienstream.to", "sto", "three", [1], "three")
        )
        ScriptedWorker.mark_delay = 0.01
        self.addCleanup(setattr, ScriptedWorker, "mark_delay", 0)

        await main.process_batch(ACTION_WATCHED, plans, [])

        in_flight: dict[str, int] = {}
        for kind, host, _slug in [e for e in ScriptedWorker.events if e[0] in ("mark", "mark-end")]:
            if kind == "mark":
                in_flight[host] = in_flight.get(host, 0) + 1
                self.assertLessEqual(in_flight[host], 1, f"{host} had two marks in flight at once")
            else:
                in_flight[host] -= 1

    async def test_each_host_keeps_its_own_series_in_order(self):
        plans = self._plans()
        plans["serienstream.to"].append(
            main.SeriesPlan("https://serienstream.to/serie/three", "serienstream.to", "sto", "three", [1], "three")
        )
        ScriptedWorker.mark_delay = 0.01
        self.addCleanup(setattr, ScriptedWorker, "mark_delay", 0)

        await main.process_batch(ACTION_WATCHED, plans, [])

        sto = [e[2] for e in ScriptedWorker.events if e[0] == "mark" and e[1] == "serienstream.to"]
        self.assertEqual(sto, ["one", "three"])

    async def test_the_hosts_actually_overlap(self):
        """The point of the change: finishing one host must not be what
        starts the next. Asserted on event ordering, not wall-clock time."""
        plans = self._plans()
        plans["serienstream.to"].append(
            main.SeriesPlan("https://serienstream.to/serie/three", "serienstream.to", "sto", "three", [1], "three")
        )
        ScriptedWorker.mark_delay = 0.02
        self.addCleanup(setattr, ScriptedWorker, "mark_delay", 0)

        await main.process_batch(ACTION_WATCHED, plans, [])

        marks = [e for e in ScriptedWorker.events if e[0] in ("mark", "mark-end")]
        overlapped = False
        open_hosts: set[str] = set()
        for kind, host, _slug in marks:
            if kind == "mark":
                if open_hosts - {host}:
                    overlapped = True
                open_hosts.add(host)
            else:
                open_hosts.discard(host)
        self.assertTrue(overlapped, "hosts were still marked one whole host after another")

    async def test_results_are_ordered_by_host_not_by_who_finished_first(self):
        """The report and the failed-URL file are built from this list, so it
        must not depend on which server happened to answer sooner.

        The two hosts deliberately carry different numbers of series, so that
        any reordering -- by completion, by size, by anything other than the
        host order -- changes the result and is caught.
        """
        plans = self._plans()
        for name in ("three", "four"):
            plans["serienstream.to"].append(
                main.SeriesPlan(
                    f"https://serienstream.to/serie/{name}", "serienstream.to", "sto", name, [1], name
                )
            )
        ScriptedWorker.mark_delay = 0.01
        self.addCleanup(setattr, ScriptedWorker, "mark_delay", 0)

        _report, results = await main.process_batch(ACTION_WATCHED, plans, [])

        # serienstream.to has 3 series and finishes last; it must still come
        # first, because that is the order the hosts were given in.
        self.assertEqual(
            [r.host for r in results],
            ["serienstream.to"] * 3 + ["burningseries.ac"],
        )

    async def test_every_worker_is_closed(self):
        await main.process_batch(ACTION_WATCHED, self._plans(), [])

        self.assertEqual(len(ScriptedWorker.made), 2)
        self.assertTrue(all(w.closed for w in ScriptedWorker.made))


class TestBatchIsParsedOnce(unittest.IsolatedAsyncioTestCase):
    """Startup used to parse the batch file twice.

    main() read it for the "loaded batch" summary and resolve_active_hosts
    then read the same unchanged file again, so a single run logged every
    duplicate-URL notice twice -- and would report any malformed line twice
    as well.
    """

    def setUp(self):
        self.calls = []

        def counting_load(path):
            self.calls.append(path)
            return {"serienstream.to": ["https://serienstream.to/serie/x"]}, []

        async def fake_check_hosts(hosts):
            return dict.fromkeys(hosts, "OK (GET 200)")

        self.counting_load = counting_load
        for target, replacement in (
            ("load_url_batches", counting_load),
            ("check_hosts", fake_check_hosts),
        ):
            patcher = mock.patch.object(main, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_a_preloaded_batch_is_not_parsed_again(self):
        batch = self.counting_load("batch.txt")  # stands in for the caller's parse
        self.assertEqual(len(self.calls), 1)

        await main.resolve_active_hosts("batch.txt", preloaded=batch)

        self.assertEqual(
            len(self.calls), 1, "resolve_active_hosts must not re-read a file the caller just parsed"
        )

    async def test_without_a_preloaded_batch_the_file_is_still_read(self):
        """Callers that have not already parsed it must keep working."""
        await main.resolve_active_hosts("batch.txt")

        self.assertEqual(self.calls, ["batch.txt"])


# ==================== batch file sections / option 7 ====================
class SectionCase(unittest.TestCase):
    """A URL line directly under a `# KEEP` tag (no blank line between) is
    permanent; every other URL line is temporary."""

    KEEP = "# KEEP"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "series_urls.txt")

    def write(self, *lines):
        Path(self.path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def read(self):
        return Path(self.path).read_text(encoding="utf-8").splitlines()

    def urls(self, lines=None):
        lines = self.read() if lines is None else lines
        return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


class TestClassifyBatchLines(SectionCase):
    def test_a_file_without_a_tag_is_entirely_temporary(self):
        """Every batch file written before this feature has no tag."""
        permanent = main._classify_batch_lines(["https://a", "https://b"])
        self.assertEqual(permanent, [False, False])

    def test_a_tag_marks_only_the_line_directly_below_it(self):
        permanent = main._classify_batch_lines(["https://a", self.KEEP, "https://b"])
        self.assertEqual(permanent, [False, True, True])

    def test_the_tag_is_recognised_however_it_was_hand_edited(self):
        """It exists to be edited by hand, so spacing and case must not matter."""
        for variant in ("# KEEP", "#keep", "#  KEEP", "   # KEEP", "# KEEP this one"):
            with self.subTest(variant=variant):
                permanent = main._classify_batch_lines(["https://a", variant, "https://b"])
                self.assertEqual(permanent, [False, True, True], f"{variant!r} not recognised")

    def test_an_ordinary_comment_is_not_mistaken_for_the_tag(self):
        permanent = main._classify_batch_lines(["# currently watching", "https://a"])
        self.assertEqual(permanent, [False, False])

    def test_a_blank_line_between_the_tag_and_the_url_breaks_the_link(self):
        """The tag must sit directly above its entry; skip a line and the URL
        below stays temporary, with the orphaned tag just a comment."""
        permanent = main._classify_batch_lines(["https://a", self.KEEP, "", "https://b"])
        self.assertEqual(permanent, [False, False, False, False])

    def test_a_tag_can_open_the_file(self):
        permanent = main._classify_batch_lines([self.KEEP, "https://a", "https://b"])
        self.assertEqual(permanent, [True, True, False])

    def test_several_tagged_entries_can_be_scattered_through_the_file(self):
        permanent = main._classify_batch_lines(
            ["https://a", self.KEEP, "https://b", "https://c", self.KEEP, "https://d"]
        )
        self.assertEqual(permanent, [False, True, True, False, True, True])

    def test_each_mirror_of_the_same_series_needs_its_own_tag(self):
        permanent = main._classify_batch_lines(
            [self.KEEP, "https://serienstream.to/serie/x", "https://burningseries.ac/serie/X"]
        )
        self.assertEqual(permanent, [True, True, False])


class TestSectionAwareWriters(SectionCase):
    def test_added_urls_land_at_the_end_untagged(self):
        self.write("https://a", self.KEEP, "https://keepme")
        main._append_batch_urls(self.path, ["https://new"])

        lines = self.read()
        self.assertEqual(self.urls(lines), ["https://a", "https://keepme", "https://new"])
        permanent = main._classify_batch_lines(lines)
        self.assertFalse(permanent[lines.index("https://new")], "a freshly added URL was tagged permanent")

    def test_adding_to_a_file_with_no_tags_still_just_appends(self):
        self.write("https://a")
        main._append_batch_urls(self.path, ["https://new"])
        self.assertEqual(self.urls(), ["https://a", "https://new"])

    def test_replacing_the_working_list_keeps_tagged_entries(self):
        self.write("https://old1", "https://old2", self.KEEP, "https://keepme")
        main._replace_batch_urls(self.path, ["https://fresh"])

        self.assertEqual(self.urls(), ["https://keepme", "https://fresh"])
        self.assertIn(self.KEEP, self.read())

    def test_replacing_a_file_with_no_tags_replaces_everything(self):
        self.write("https://old1", "https://old2")
        main._replace_batch_urls(self.path, ["https://fresh"])
        self.assertEqual(self.urls(), ["https://fresh"])

    def test_section_counts(self):
        self.write("https://a", "https://b", "# a note", self.KEEP, "https://keepme")
        self.assertEqual(main._batch_section_counts(self.path), (2, 1))

    def test_section_counts_with_scattered_tags(self):
        self.write("https://a", self.KEEP, "https://b", "https://c", self.KEEP, "https://d")
        self.assertEqual(main._batch_section_counts(self.path), (2, 2))


class TestLoadUrlBatchesWithTags(SectionCase):
    def test_permanent_entries_are_still_loaded_like_any_other(self):
        """'Permanent' means the file keeps them, not that they are skipped."""
        self.write(
            "https://serienstream.to/serie/one",
            self.KEEP,
            "https://serienstream.to/serie/two",
        )
        grouped, rejected = main.load_url_batches(self.path)
        self.assertEqual(rejected, [])
        self.assertEqual(
            grouped["serienstream.to"],
            ["https://serienstream.to/serie/one", "https://serienstream.to/serie/two"],
        )

    def test_the_tag_is_not_reported_as_an_unsupported_line(self):
        self.write("https://serienstream.to/serie/one", self.KEEP)
        _grouped, rejected = main.load_url_batches(self.path)
        self.assertEqual(rejected, [])


class TestClearTemporaryUrls(SectionCase, unittest.IsolatedAsyncioTestCase):
    async def test_it_clears_the_working_list_and_keeps_the_rest(self):
        self.write("https://a", "https://b", self.KEEP, "https://keepme")
        with mock.patch.object(main, "ask_yes_no", return_value=True):
            await main.clear_temporary_urls(self.path)
        self.assertEqual(self.urls(), ["https://keepme"])
        self.assertIn(self.KEEP, self.read())

    async def test_saying_no_changes_nothing(self):
        self.write("https://a", self.KEEP, "https://keepme")
        before = Path(self.path).read_text(encoding="utf-8")
        with mock.patch.object(main, "ask_yes_no", return_value=False):
            await main.clear_temporary_urls(self.path)
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), before)

    async def test_it_never_asks_when_there_is_nothing_to_clear(self):
        self.write(self.KEEP, "https://keepme")
        with mock.patch.object(main, "ask_yes_no") as ask:
            await main.clear_temporary_urls(self.path)
        ask.assert_not_called()
        self.assertEqual(self.urls(), ["https://keepme"])

    async def test_your_own_comments_in_the_working_list_survive(self):
        """A tidy-up that also deleted the notes you wrote about the list
        would be a surprise."""
        self.write("# currently watching", "https://a", self.KEEP, "https://keepme")
        with mock.patch.object(main, "ask_yes_no", return_value=True):
            await main.clear_temporary_urls(self.path)
        self.assertIn("# currently watching", self.read())
        self.assertEqual(self.urls(), ["https://keepme"])

    async def test_a_file_with_no_tags_clears_everything_but_warns_first(self):
        self.write("https://a", "https://b")
        printed = []
        with (
            mock.patch.object(main, "ask_yes_no", return_value=True),
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        ):
            await main.clear_temporary_urls(self.path)
        self.assertTrue(any("tagged permanent" in line for line in printed), "the user was not warned")
        self.assertEqual(self.urls(), [])

    async def test_the_urls_it_will_remove_are_shown_before_asking(self):
        self.write("https://a", "https://b", self.KEEP, "https://keepme")
        printed = []
        with (
            mock.patch.object(main, "ask_yes_no", return_value=False),
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        ):
            await main.clear_temporary_urls(self.path)
        body = "\n".join(printed)
        self.assertIn("https://a", body)
        self.assertIn("https://b", body)
        self.assertNotIn("https://keepme", body, "a kept URL was listed as doomed")

    async def test_scattered_tags_are_all_respected(self):
        self.write("https://a", self.KEEP, "https://b", "https://c", self.KEEP, "https://d")
        with mock.patch.object(main, "ask_yes_no", return_value=True):
            await main.clear_temporary_urls(self.path)
        self.assertEqual(self.urls(), ["https://b", "https://d"])


class TestBatchRewritersKeepThePermanentSection(SectionCase, unittest.IsolatedAsyncioTestCase):
    """Both of these replace the batch file wholesale. Before tagged entries
    existed that was fine; now a full truncate would delete exactly the
    entries the user tagged permanent."""

    async def test_retry_replaces_only_the_working_list(self):
        self.write("https://old", self.KEEP, "https://keepme")
        with (
            mock.patch.object(main, "_load_failed_urls", return_value=["https://failed-one"]),
            mock.patch.object(main, "ask_yes_no", return_value=True),
        ):
            await main.retry_failed_urls(self.path)

        self.assertEqual(self.urls(), ["https://keepme", "https://failed-one"])
        self.assertIn(self.KEEP, self.read())

    async def test_retry_declined_leaves_the_file_alone(self):
        self.write("https://old", self.KEEP, "https://keepme")
        before = Path(self.path).read_text(encoding="utf-8")
        with (
            mock.patch.object(main, "_load_failed_urls", return_value=["https://failed-one"]),
            mock.patch.object(main, "ask_yes_no", return_value=False),
        ):
            await main.retry_failed_urls(self.path)
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), before)

    async def test_retry_with_nothing_recorded_does_not_touch_the_file(self):
        self.write("https://old", self.KEEP, "https://keepme")
        before = Path(self.path).read_text(encoding="utf-8")
        with mock.patch.object(main, "_load_failed_urls", return_value=[]):
            await main.retry_failed_urls(self.path)
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), before)

    async def test_pasting_a_url_replaces_only_the_working_list(self):
        self.write("https://old", self.KEEP, "https://serienstream.to/serie/keepme")
        pasted = "https://serienstream.to/serie/fresh"
        with (
            mock.patch.object(main, "DEFAULT_BATCH_FILE", self.path),
            mock.patch("builtins.input", return_value=pasted),
        ):
            await main._detect_and_add_input(self.path)

        self.assertEqual(self.urls(), ["https://serienstream.to/serie/keepme", pasted])
        self.assertIn(self.KEEP, self.read())

    async def test_pasting_an_unsupported_url_changes_nothing(self):
        self.write("https://old", self.KEEP, "https://keepme")
        before = Path(self.path).read_text(encoding="utf-8")
        with (
            mock.patch.object(main, "DEFAULT_BATCH_FILE", self.path),
            mock.patch("builtins.input", return_value="https://example.com/not-a-series"),
        ):
            await main._detect_and_add_input(self.path)
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), before)

    async def test_pressing_enter_cancels_without_touching_the_file(self):
        self.write("https://old", self.KEEP, "https://keepme")
        before = Path(self.path).read_text(encoding="utf-8")
        with (
            mock.patch.object(main, "DEFAULT_BATCH_FILE", self.path),
            mock.patch("builtins.input", return_value=""),
        ):
            result = await main._detect_and_add_input(self.path)
        self.assertEqual(result, self.path)
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), before)


# ==================== failed URLs are per (url, action) ====================
class FailedStoreCase(unittest.TestCase):
    """A failure is (url, action), not a url.

    Keying on the URL alone meant a run that successfully marked a series
    *unwatched* deleted the record that marking the same series *watched*
    had failed -- a real failure, silently forgotten and never retried.
    """

    X = "https://serienstream.to/serie/x"
    Y = "https://serienstream.to/serie/y"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = os.path.join(self.dir.name, "failed.json")
        patcher = mock.patch.object(main, "FAILED_URLS_FILE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, action, outcomes):
        report = main.RunReport(total_urls=len(outcomes))
        report.failed_urls = [u for u, ok in outcomes.items() if not ok]
        report.failed = len(report.failed_urls)
        report.successful = len(outcomes) - report.failed
        main._persist_failed_urls(report, set(outcomes), action)

    def stored(self):
        return {(e["url"], e["action"]) for e in main._load_failed_entries()}

    def write_raw(self, payload):
        Path(self.store).write_text(json.dumps(payload), encoding="utf-8")


class TestFailedStore(FailedStoreCase):
    def test_a_failure_records_the_action_it_happened_under(self):
        self.record(ACTION_WATCHED, {self.X: False})
        self.assertEqual(self.stored(), {(self.X, ACTION_WATCHED)})

    def test_a_success_under_the_other_action_does_not_clear_it(self):
        self.record(ACTION_WATCHED, {self.X: False})
        self.record(ACTION_UNWATCHED, {self.X: True})
        self.assertEqual(
            self.stored(),
            {(self.X, ACTION_WATCHED)},
            "marking it unwatched erased the record that marking it watched had failed",
        )

    def test_a_success_under_the_same_action_does_clear_it(self):
        self.record(ACTION_WATCHED, {self.X: False})
        self.record(ACTION_WATCHED, {self.X: True})
        self.assertEqual(self.stored(), set())

    def test_failing_under_both_actions_keeps_both(self):
        self.record(ACTION_WATCHED, {self.X: False})
        self.record(ACTION_UNWATCHED, {self.X: False})
        self.assertEqual(self.stored(), {(self.X, ACTION_WATCHED), (self.X, ACTION_UNWATCHED)})

    def test_failing_twice_under_one_action_is_still_one_entry(self):
        self.record(ACTION_WATCHED, {self.X: False})
        self.record(ACTION_WATCHED, {self.X: False})
        self.assertEqual(self.stored(), {(self.X, ACTION_WATCHED)})

    def test_a_url_this_run_never_attempted_is_left_alone(self):
        self.record(ACTION_WATCHED, {self.X: False, self.Y: False})
        self.record(ACTION_WATCHED, {self.X: True})
        self.assertEqual(self.stored(), {(self.Y, ACTION_WATCHED)})

    def test_the_batch_written_for_retry_lists_a_url_once(self):
        """Two actions failing is two records but one line to re-run."""
        self.record(ACTION_WATCHED, {self.X: False})
        self.record(ACTION_UNWATCHED, {self.X: False})
        self.assertEqual(main._load_failed_urls(), [self.X])


class TestLegacyFailedFile(FailedStoreCase):
    """Files written before the action was recorded hold bare URL strings."""

    def test_bare_strings_are_read_with_an_unknown_action(self):
        self.write_raw([self.X, self.Y])
        self.assertEqual(self.stored(), {(self.X, ""), (self.Y, "")})

    def test_an_unknown_entry_is_cleared_by_whichever_action_succeeds(self):
        """Exactly how it behaved before, so upgrading loses nothing."""
        self.write_raw([self.X])
        self.record(ACTION_UNWATCHED, {self.X: True})
        self.assertEqual(self.stored(), set())

    def test_an_unknown_entry_that_fails_again_gains_its_action(self):
        self.write_raw([self.X])
        self.record(ACTION_WATCHED, {self.X: False})
        self.assertEqual(self.stored(), {(self.X, ACTION_WATCHED)})

    def test_a_mixed_file_of_old_and_new_entries_reads_cleanly(self):
        self.write_raw([self.X, {"url": self.Y, "action": ACTION_WATCHED}])
        self.assertEqual(self.stored(), {(self.X, ""), (self.Y, ACTION_WATCHED)})

    def test_junk_entries_are_skipped_rather_than_crashing(self):
        self.write_raw([self.X, None, 42, {}, {"action": "watched"}])
        self.assertEqual(self.stored(), {(self.X, "")})

    def test_a_corrupt_file_is_reported_as_empty(self):
        Path(self.store).write_text("{ not json", encoding="utf-8")
        self.assertEqual(main._load_failed_entries(), [])

    def test_a_json_object_instead_of_a_list_is_reported_as_empty(self):
        self.write_raw({"urls": [self.X]})
        self.assertEqual(main._load_failed_entries(), [])


class TestRetryShowsTheAction(FailedStoreCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.batch = os.path.join(self.dir.name, "series_urls.txt")
        Path(self.batch).write_text("", encoding="utf-8")

    async def _retry_output(self):
        printed = []
        with (
            mock.patch.object(main, "ask_yes_no", return_value=False),
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        ):
            await main.retry_failed_urls(self.batch)
        return "\n".join(printed)

    async def test_it_names_the_action_and_the_option_to_use(self):
        self.record(ACTION_WATCHED, {self.X: False})
        body = await self._retry_output()
        self.assertIn("WATCHED", body)
        self.assertIn("option 1", body)

    async def test_unwatched_failures_point_at_option_2(self):
        self.record(ACTION_UNWATCHED, {self.X: False})
        body = await self._retry_output()
        self.assertIn("UNWATCHED", body)
        self.assertIn("option 2", body)

    async def test_a_mixed_list_warns_that_both_options_are_needed(self):
        self.record(ACTION_WATCHED, {self.X: False})
        self.record(ACTION_UNWATCHED, {self.Y: False})
        body = await self._retry_output()
        self.assertIn("different actions", body)

if __name__ == "__main__":
    unittest.main()
