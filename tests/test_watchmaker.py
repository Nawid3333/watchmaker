"""Unit tests for watchmaker's pure logic.

Run with:  python -m unittest discover -s tests

These cover the parts where a silent mistake would corrupt data or report a
wrong result: URL classification, batch-file rewriting, season discovery,
episode counting, and the post-mark verification.
"""

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


if __name__ == "__main__":
    unittest.main()
