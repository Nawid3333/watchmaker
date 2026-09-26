"""Unit tests for watchmaker's pure logic.

Run with:  python -m unittest discover -s tests

These cover the parts where a silent mistake would corrupt data or report a
wrong result: URL classification, batch-file rewriting, season discovery,
episode counting, and the post-mark verification.
"""

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

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


def soup(html: str):
    return main.make_doc(html)


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

    def test_one_series_spelled_two_ways_is_collapsed(self):
        # A URL copied from the address bar arrives decoded, one from a scraper
        # list percent-encoded. The scrapers' slug_key folds the two into one
        # slug, so this has to as well.
        path = self.write(
            "b.txt",
            "https://serienstream.to/serie/25%20Years%20of%20You\nhttps://serienstream.to/serie/25 years of you\n",
        )
        grouped, _ = load_url_batches(path)
        self.assertEqual(grouped["serienstream.to"], ["https://serienstream.to/serie/25%20Years%20of%20You"])

    def test_different_series_are_not_collapsed(self):
        path = self.write(
            "b.txt",
            "https://serienstream.to/serie/waldern\nhttps://serienstream.to/serie/wldern\n",
        )
        grouped, _ = load_url_batches(path)
        self.assertEqual(len(grouped["serienstream.to"]), 2)

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
        # Strict on purpose: a read the script did not plan for is an
        # IndexError, so an extra request cannot slip in unnoticed. An
        # exception in the script is raised at that read.
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return soup(page)

    async def _issue_mark(self, doc, season_url, slug, season, action):
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
        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 0), episodes(5, 0)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.note, "expected 5/5 after watched, got 0 (marked twice)")

    async def test_partial_mark_is_a_failure(self):
        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 3), episodes(5, 3)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.note, "expected 5/5 after watched, got 3 (marked twice)")

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
            async def _issue_mark(self, doc, season_url, slug, season, action):
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
            async def _issue_mark(self, doc, season_url, slug, season, action):
                raise main.ControlMissingError("No #season-mark control")

            async def _recover_session(self):
                return False

        w = Missing("serienstream.to", [episodes(5, 0)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertIn("season-mark", outcome.note)


class TestSecondMark(unittest.IsolatedAsyncioTestCase):
    """A mark the site answers with 200 does not always land. A season a mark
    left off target is marked once more and read back again before it counts
    as failed -- once, never a third time, and never for a season that needed
    no mark to begin with."""

    async def test_a_mark_that_did_not_stick_is_sent_once_more_and_can_succeed(self):
        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 0), episodes(5, 5)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual(w.marks, [("x", 1, ACTION_WATCHED)] * 2)
        self.assertEqual((outcome.marks, outcome.watched_after), (2, 5))
        self.assertEqual(outcome.note, "stuck on the second mark")

    async def test_a_mark_that_fails_twice_is_a_failure_after_exactly_two_marks(self):
        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 2), episodes(5, 4)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertEqual(len(w.marks), 2, "never a third mark")
        self.assertEqual(outcome.note, "expected 5/5 after watched, got 4 (marked twice)")
        self.assertEqual(outcome.watched_after, 4, "the last read-back is what is reported")

    async def test_unwatching_gets_the_second_mark_too(self):
        w = FakeWorker("serienstream.to", [episodes(5, 5), episodes(5, 5), episodes(5, 0)])
        outcome = await w.mark_season("x", 1, ACTION_UNWATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(w.marks), 2)

    async def test_a_mark_that_stuck_the_first_time_is_sent_once(self):
        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 5)])  # a third read would raise
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual((len(w.marks), outcome.marks, outcome.note), (1, 1, ""))

    async def test_a_season_that_needed_no_mark_is_not_marked_on_a_bad_read_back(self):
        w = FakeWorker("serienstream.to", [episodes(5, 5), episodes(5, 2)])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertEqual(w.marks, [], "a changed page is reported, not answered with a mark")

    async def test_a_known_placeholder_does_not_trigger_the_second_mark(self):
        # Episode 0 is listed, so it is not counted: its refusal to stick
        # leaves the season on target, and costs no second POST.
        w = FakeWorker("serienstream.to", [numbered((0, False), (1, False)), numbered((0, False), (1, True))])
        w._ignored_seasons = frozenset({("x", "1")})
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(w.marks), 1)

    async def test_a_refused_second_mark_is_reported_as_such(self):
        calls = []

        def refuse_second():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("POST refused")

        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 0)], mark_effect=refuse_second)
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.note, "POST refused")
        self.assertEqual(outcome.marks, 1, "only a mark that went through is counted")

    async def test_an_unreadable_page_after_the_second_mark_is_unverified(self):
        w = FakeWorker("serienstream.to", [episodes(5, 0), episodes(5, 0), RuntimeError("error page 502")])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.note, "unverified: error page 502")
        self.assertEqual(outcome.marks, 2)


# ==================== episode 0 placeholders ====================
def numbered(*rows):
    """A season page from (episode number, watched) pairs, in s.to's markup."""
    cells = "".join(
        f'<tr class="episode-row{" seen" if seen else ""}"><th class="episode-number-cell">{n}</th></tr>'
        for n, seen in rows
    )
    return f'<table class="episode-table"><tbody>{cells}</tbody></table>'


class TestEpisodeZero(unittest.IsolatedAsyncioTestCase):
    """s.to answers a mark on an episode 0 placeholder with ok, seen, 1/1 --
    and the page still shows it unwatched. marry-my-husband season 0 failed
    every run on it (2026-09-26); the S.to scraper already ignores it."""

    def worker(self, pages, ignored=()):
        w = FakeWorker("serienstream.to", pages)
        w._ignored_seasons = frozenset(ignored)
        return w

    def test_counting_can_leave_episode_zero_out(self):
        w = DomainWorker("serienstream.to")
        page = soup(numbered((0, False), (1, True), (2, False)))
        self.assertEqual(w._count_episodes(page), (1, 3))
        self.assertEqual(w._count_episodes(page, skip_episode_zero=True), (1, 2))

    def test_aniworld_numbers_come_from_the_episode_meta(self):
        w = DomainWorker("aniworld.to")
        rows = "".join(
            f'<tr data-episode-id="{n}"><td><meta itemprop="episodeNumber" content="{n}"></td></tr>' for n in (0, 1)
        )
        page = soup(f'<table class="seasonEpisodesList"><tbody>{rows}</tbody></table>')
        self.assertEqual(w._count_episodes(page, skip_episode_zero=True), (0, 1))

    def test_a_row_without_a_number_is_never_taken_for_episode_zero(self):
        w = DomainWorker("serienstream.to")
        self.assertEqual(w._count_episodes(soup(episodes(3, 1)), skip_episode_zero=True), (1, 3))

    async def test_a_season_holding_only_the_ignored_placeholder_is_still_marked_and_checked(self):
        w = self.worker([numbered((0, False)), numbered((0, False))], ignored={("marry-my-husband", "0")})
        outcome = await w.mark_season("marry-my-husband", 0, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.placeholder_only)
        self.assertEqual(w.marks, [("marry-my-husband", 0, ACTION_WATCHED)])
        self.assertEqual(outcome.episode_zero(ACTION_WATCHED).kind, "placeholder")
        self.assertFalse(outcome.episode_zero(ACTION_WATCHED).attention)

    async def test_a_listed_episode_zero_is_marked_even_when_every_counted_episode_is_done(self):
        w = self.worker([numbered((0, False), (1, True)), numbered((0, False), (1, True))], ignored={("x", "1")})
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual(w.marks, [("x", 1, ACTION_WATCHED)])
        self.assertIn("known placeholder", outcome.episode_zero(ACTION_WATCHED).text)

    async def test_an_ignored_placeholder_does_not_fail_verification(self):
        w = self.worker(
            [numbered((0, False), (1, False)), numbered((0, False), (1, True))],
            ignored={("x", "1")},
        )
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual((outcome.watched_after, outcome.total), (1, 1))

    async def test_a_listed_episode_zero_that_now_sticks_is_flagged(self):
        w = self.worker([numbered((0, False), (1, False)), numbered((0, True), (1, True))], ignored={("x", "1")})
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        note = outcome.episode_zero(ACTION_WATCHED)
        self.assertEqual(note.kind, "sticks")
        self.assertTrue(note.attention)

    async def test_a_listed_season_without_an_episode_zero_is_flagged_as_stale(self):
        w = self.worker([numbered((1, False)), numbered((1, True))], ignored={("x", "1")})
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.episode_zero(ACTION_WATCHED).kind, "stale")

    async def test_a_listed_season_with_no_rows_is_still_a_failure(self):
        w = self.worker(["<html></html>"], ignored={("x", "1")})
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.note, "no episodes found")
        self.assertIsNone(outcome.episode_zero(ACTION_WATCHED), "an unread page is not a stale entry")

    async def test_unwatching_leaves_a_listed_placeholder_unreported(self):
        w = self.worker([numbered((0, False), (1, True)), numbered((0, False), (1, False))], ignored={("x", "1")})
        outcome = await w.mark_season("x", 1, ACTION_UNWATCHED)
        self.assertTrue(outcome.ok)
        self.assertIsNone(outcome.episode_zero(ACTION_UNWATCHED))

    async def test_the_ignore_list_is_per_season(self):
        w = self.worker([numbered((0, False), (1, False)), numbered((0, False), (1, True))], ignored={("x", "2")})
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)

    async def test_an_unlisted_placeholder_is_named_in_the_failure(self):
        w = self.worker([numbered((0, False), (1, False))] + [numbered((0, False), (1, True))] * 2)
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertFalse(outcome.ok)
        self.assertIn("episode 0 will not stay watched", outcome.note)
        note = outcome.episode_zero(ACTION_WATCHED)
        self.assertEqual(note.kind, "unlisted")
        self.assertTrue(note.attention)

    async def test_an_unlisted_episode_zero_that_sticks_says_nothing(self):
        w = self.worker([numbered((0, False), (1, False)), numbered((0, True), (1, True))])
        outcome = await w.mark_season("x", 1, ACTION_WATCHED)
        self.assertTrue(outcome.ok)
        self.assertIsNone(outcome.episode_zero(ACTION_WATCHED))


class TestOnlyEpisodeZeroIsOfferedForTheIgnoreList(unittest.IsolatedAsyncioTestCase):
    """The ignore list is for one thing: an episode 0 placeholder that takes a
    mark and never shows it. A normal episode that fails to mark, or a mark
    that fails as a whole, is a failure to see and retry -- never something
    to hide behind an ignore entry."""

    HINT = "if it is a placeholder, add"

    async def _mark(self, after, action=ACTION_WATCHED):
        before = [(n, action != ACTION_WATCHED) for n in range(8)]
        # The second mark sees the same page: it did not help either.
        w = FakeWorker("serienstream.to", [numbered(*before), numbered(*after), numbered(*after)])
        w._ignored_seasons = frozenset()
        outcome = await w.mark_season("show", 1, action)
        result = SeriesResult("serienstream.to", "sto", "u", "show", action=action, title="Show", seasons=[outcome])
        out = io.StringIO()
        with redirect_stdout(out):
            main._print_run_summary(main.RunReport(total_urls=1, failed=1), [result])
        return outcome, out.getvalue()

    async def test_a_normal_episode_that_did_not_stick_is_never_offered(self):
        outcome, summary = await self._mark([(n, n != 7) for n in range(8)])
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.note, "expected 8/8 after watched, got 7 (marked twice)")
        self.assertIsNone(outcome.episode_zero(ACTION_WATCHED))
        self.assertNotIn(self.HINT, summary)
        self.assertNotIn("EPISODE 0", summary)

    async def test_a_mark_that_failed_as_a_whole_is_not_offered(self):
        outcome, summary = await self._mark([(n, False) for n in range(8)])
        self.assertFalse(outcome.ok)
        self.assertNotIn("episode 0 will not stay watched", outcome.note)
        note = outcome.episode_zero(ACTION_WATCHED)
        self.assertEqual(note.kind, "mark failed")
        self.assertFalse(note.attention, "the season's own failure is the thing to look at")
        self.assertNotIn(self.HINT, summary)

    async def test_episode_zero_alone_not_sticking_is_offered(self):
        outcome, summary = await self._mark([(n, n != 0) for n in range(8)])
        self.assertIn("episode 0 will not stay watched", outcome.note)
        self.assertEqual(outcome.episode_zero(ACTION_WATCHED).kind, "unlisted")
        self.assertIn(self.HINT + ' {"slug": "show", "season": "1"}', summary)

    async def test_episode_zero_staying_watched_on_unwatch_is_not_offered(self):
        # A placeholder shows unwatched whatever is done to it, so this is
        # something else and an ignore entry would not explain it.
        outcome, summary = await self._mark([(n, n == 0) for n in range(8)], action=ACTION_UNWATCHED)
        note = outcome.episode_zero(ACTION_UNWATCHED)
        self.assertEqual(note.kind, "check")
        self.assertTrue(note.attention)
        self.assertNotIn(self.HINT, summary)


class TestEpisodeZeroInTheCli(unittest.TestCase):
    """Every episode 0 the run met is shown, not only logged."""

    @staticmethod
    def _result(*seasons, action=ACTION_WATCHED):
        return SeriesResult("serienstream.to", "sto", "u", "x", action=action, title="X", seasons=list(seasons))

    def test_a_listed_placeholder_alone_is_reason_to_mark(self):
        season = SeasonOutcome(season=1, total=1, watched_before=1, listed=True, ep0_before=False)
        self.assertTrue(season.needs_mark(ACTION_WATCHED))

    def test_the_preview_says_what_will_happen_to_episode_zero(self):
        r = self._result(
            SeasonOutcome(season=1, total=1, watched_before=1, listed=True, ep0_before=False),
            SeasonOutcome(season=2, total=2, watched_before=1, ep0_before=False),
        )
        lines = r.episode_zero_lines(planned=True)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("▶S1 E0:"))
        self.assertIn("marked anyway and checked", lines[0])
        self.assertIn("not on the ignore list", lines[1])

    def test_the_result_line_carries_a_short_tag(self):
        r = self._result(
            SeasonOutcome(
                season=0, total=0, watched_before=0, watched_after=0, listed=True, ep0_before=False, ep0_after=False
            )
        )
        self.assertTrue(r.line().startswith("✓"))
        self.assertIn(" · E0: S0 placeholder", r.line())

    def _summary(self, results, ignore_lists=()):
        report = main.RunReport(total_urls=len(results), successful=len(results))
        report.ignore_lists = list(ignore_lists)
        out = io.StringIO()
        with redirect_stdout(out):
            main._print_run_summary(report, results)
        return out.getvalue()

    def test_the_run_summary_lists_every_finding_in_full(self):
        listed = SeasonOutcome(
            season=1, total=1, watched_before=1, watched_after=1, listed=True, ep0_before=False, ep0_after=False
        )
        unlisted = SeasonOutcome(
            season=2, total=2, watched_before=1, watched_after=1, ep0_before=False, ep0_after=False
        )
        text = self._summary([self._result(listed, unlisted)])
        self.assertIn("EPISODE 0", text)
        self.assertIn("S1: episode 0 did not stay watched — a known placeholder", text)
        self.assertIn('add {"slug": "x", "season": "2"}', text)
        self.assertIn("1 known placeholder(s), 1 to check", text)

    def test_an_ignore_list_problem_is_repeated_in_the_summary(self):
        missing = main.IgnoreList("sto", "p", problem="not found: p — no episode 0 is ignored", warning=True)
        expected = main.IgnoreList("bs", "q", problem="none — its scraper keeps no ignore list")
        text = self._summary([self._result()], [missing, expected])
        self.assertIn("sto: not found: p", text)
        self.assertNotIn("bs:", text)

    def test_an_unread_listed_season_is_not_called_stale(self):
        """A page with no episode rows at all is a failed read, reported as
        one. The preview also called its ignore-list entry stale, which could
        get a correct entry removed."""
        season = SeasonOutcome(season=1, total=0, watched_before=0, listed=True, ep0_before=None)
        self.assertIsNone(season.episode_zero(ACTION_WATCHED, planned=True))
        self.assertEqual(self._result(season).episode_zero_lines(planned=True), [])

    def test_the_summary_says_which_entries_to_remove(self):
        sticks = SeasonOutcome(
            season=1, total=1, watched_before=0, watched_after=1, listed=True, ep0_before=False, ep0_after=True
        )
        stale = SeasonOutcome(season=2, total=3, watched_before=0, watched_after=3, listed=True)
        with mock.patch.object(main, "IGNORED_SEASONS_FILES", {"sto": "ignored.json"}):
            text = self._summary([self._result(sticks, stale)])
        self.assertIn("S1: episode 0 is watched although the ignore list names it a placeholder", text)
        self.assertIn("S2: on the ignore list, but this season has no episode 0", text)
        self.assertEqual(text.count("if so, remove it from ignored.json"), 2)
        self.assertIn("0 known placeholder(s), 2 to check", text)


class TestLoadIgnoreList(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, ".ignored_seasons.json")
        # Replaced, not patched in place: main and config share one dict, and
        # under plain unittest (no conftest copy) patching it would change the
        # config paths the last test below checks.
        patcher = mock.patch.object(main, "IGNORED_SEASONS_FILES", {"sto": self.path, "bs": self.path})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, text):
        Path(self.path).write_text(text, encoding="utf-8")

    def test_entries_are_read_with_slugs_folded_like_the_scraper_folds_them(self):
        self.write(json.dumps([{"slug": "Marry%20My-Husband", "season": "0"}, {"slug": "x", "season": 3}]))
        il = main.load_ignore_list("sto")
        self.assertEqual(il.seasons, {("marry my-husband", "0"), ("x", "3")})
        self.assertEqual(il.problem, "")
        self.assertTrue(il.status_line().startswith("✓ sto"))
        self.assertIn("2 season(s)", il.status_line())

    def test_entries_without_a_slug_are_counted_not_dropped_silently(self):
        self.write(json.dumps([{"slug": "x", "season": "1"}, {"season": "1"}]))
        il = main.load_ignore_list("sto")
        self.assertEqual(il.seasons, {("x", "1")})
        self.assertTrue(il.warning)
        self.assertIn("1 entr(y/ies) without a slug skipped", il.problem)

    def test_a_missing_file_ignores_nothing_and_says_so(self):
        il = main.load_ignore_list("sto")
        self.assertEqual(il.seasons, frozenset())
        self.assertTrue(il.warning)
        self.assertIn("not found", il.problem)
        self.assertTrue(il.status_line().startswith("⚠ sto"))

    def test_a_broken_file_ignores_nothing_and_says_so(self):
        self.write("{not json")
        il = main.load_ignore_list("sto")
        self.assertEqual(il.seasons, frozenset())
        self.assertTrue(il.warning)
        self.assertIn("could not read", il.problem)

    def test_a_file_that_is_not_a_list_ignores_nothing_and_says_so(self):
        self.write('{"slug": "x"}')
        il = main.load_ignore_list("sto")
        self.assertEqual(il.seasons, frozenset())
        self.assertIn("not a JSON list", il.problem)

    def test_bs_keeps_no_list_so_its_absence_is_only_noted(self):
        il = main.load_ignore_list("bs")
        self.assertFalse(il.warning)
        self.assertIn("keeps no ignore list", il.problem)

    def test_a_bs_file_that_does_exist_is_still_read(self):
        self.write(json.dumps([{"slug": "x", "season": "1"}]))
        self.assertEqual(main.load_ignore_list("bs").seasons, {("x", "1")})

    def test_a_family_without_a_scraper_ignores_nothing(self):
        with mock.patch.dict(main.IGNORED_SEASONS_FILES, {"bs": None, "sto": None}):
            self.assertEqual(main.load_ignore_list("bs").seasons, frozenset())
            self.assertFalse(main.load_ignore_list("bs").warning)
            self.assertTrue(main.load_ignore_list("sto").warning)

    def test_the_file_sits_in_the_scraper_data_folder_next_to_its_url_list(self):
        import config

        for family, path in config.IGNORED_SEASONS_FILES.items():
            export = config.SERIES_URLS_EXPORTS[family]
            if export:
                self.assertEqual(path, os.path.join(os.path.dirname(export), "data", ".ignored_seasons.json"))

    def test_every_family_said_to_keep_a_list_is_a_real_family(self):
        """A misspelt family here would quietly turn its missing list from a
        warning into a note."""
        import config

        self.assertLessEqual(config.IGNORE_LIST_FAMILIES, set(config.SUPPORTED_DOMAINS.values()))
        self.assertLessEqual(config.IGNORE_LIST_FAMILIES, set(config.IGNORED_SEASONS_FILES))

    def test_every_family_in_the_batch_is_shown_before_the_preview(self):
        grouped = {"serienstream.to": ["u1"], "burningseries.ac": ["u2"]}
        out = io.StringIO()
        with redirect_stdout(out):
            lists = main._print_ignore_lists(grouped)
        self.assertEqual([il.family for il in lists], ["bs", "sto"])
        text = out.getvalue()
        self.assertIn("episode 0 ignore lists", text)
        self.assertIn("⚠ sto", text)
        self.assertIn("· bs", text)


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
class _FakeResponse(httpx.Response):
    def __init__(self, status_code: int = 200, text: str = "") -> None:
        super().__init__(
            status_code=status_code,
            text=text,
            request=httpx.Request("GET", "https://example.com/"),
        )


class _FakeClient:
    """Stands in for httpx.AsyncClient and records what was fetched."""

    def __init__(self, response: httpx.Response | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.requested: list[str] = []

    async def get(self, url: str, **kwargs) -> httpx.Response:
        self.requested.append(str(url))
        if self._exc is not None:
            raise self._exc
        if self._response is None:
            raise RuntimeError("_FakeClient.get called with no response configured")
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

    def test_a_login_form_identifies_a_login_page_without_a_password_field(self):
        """A form posting at /login is the one accepted alternative.

        It is a claim about the page's structure, which is what separates a
        real login page from a page that merely mentions logging in.
        """
        self.assertTrue(main._looks_like_login_page('<form action="/login" method="post">'))
        self.assertTrue(main._looks_like_login_page("<form action='https://x.tld/login'>"))

    def test_wording_alone_is_not_enough(self):
        """The word is not evidence -- it is what the impostors have too.

        This check decides which mirror becomes active, and that choice is
        written into the batch file on disk before any login is attempted.
        A parked domain and a Cloudflare block page both carry the word in
        their nav or body, so accepting it defeated the whole check.
        """
        for html in (
            "<body>Bitte anmelden</body>",
            "<body>Please Login</body>",
            "<body>Site offline<script>var loginUrl='/x'</script></body>",
            "<html><title>Attention Required! | Cloudflare</title>"
            "<body>Error 1020<a href='/login'>Login</a></body></html>",
        ):
            with self.subTest(html):
                self.assertFalse(main._looks_like_login_page(html))

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

    async def test_one_series_reached_by_two_mirrors_is_marked_once(self):
        """Duplicates must be collapsed after the rewrite, not only before it.

        load_url_batches collapses by (host, slug), which runs while the two
        URLs still sit on different hosts and therefore looks like two
        different series. Once both are rewritten onto the active mirror they
        name the same show, and the exact-string dedupe that followed kept
        both -- so every season was fetched, marked and verified twice, and
        the run summary counted the series twice.
        """
        path = self._batch(
            "https://serienstream.to/serie/some-show",
            "https://serienstream.cx/serie/some-show/staffel-3",
        )

        with mock.patch.object(main, "check_hosts", self._statuses({"serienstream.to", "serienstream.cx"})):
            resolved, _statuses, active = await main.resolve_active_hosts(path)

        self.assertEqual(active.get("sto"), "serienstream.to")
        queued = [url for urls in resolved.values() for url in urls]
        slugs = [main.slug_for(url, "sto") for url in queued]
        self.assertEqual(slugs, ["some-show"], f"one series must be queued once, got {queued}")

    async def test_genuinely_different_series_on_two_mirrors_both_survive(self):
        path = self._batch(
            "https://serienstream.to/serie/show-one",
            "https://serienstream.cx/serie/show-two",
        )

        with mock.patch.object(main, "check_hosts", self._statuses({"serienstream.to", "serienstream.cx"})):
            resolved, _statuses, _active = await main.resolve_active_hosts(path)

        slugs = sorted(main.slug_for(u, "sto") for urls in resolved.values() for u in urls)
        self.assertEqual(slugs, ["show-one", "show-two"])


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

    async def _post(self, url, data=None, *, json=None, headers=None):
        return _FakeResponse(200, "")


class FlakyLoginWorker(LoginRecordingWorker):
    """Each login POST takes the next scripted outcome: an exception (the
    site erroring), True (credentials accepted) or False (refused)."""

    def __init__(self, outcomes, *, logged_in_after_error=False):
        super().__init__("burningseries.ac", BS_LOGGED_OUT_HOME)
        self.outcomes = list(outcomes)
        self.logged_in_after_error = logged_in_after_error
        self.session = False
        self.posts = 0

    async def _get_soup(self, url):
        self.fetched.append(url)
        if url.endswith("/login"):
            return soup(self.login_page)
        return soup(BS_LOGGED_IN_HOME if self.session else BS_LOGGED_OUT_HOME)

    async def _post(self, url, data=None, *, json=None, headers=None):
        self.posts += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            self.session = self.logged_in_after_error
            raise outcome
        self.session = outcome
        return _FakeResponse(200, "")


class TestLoginRecovery(unittest.IsolatedAsyncioTestCase):
    """2026-09-26: s.to answered a login POST with 500, the blind resend of
    the same form got 419 (expired CSRF token), and the whole run failed."""

    def setUp(self):
        patcher = mock.patch.object(main.asyncio, "sleep", new=mock.AsyncMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_a_login_that_errored_but_went_through_is_kept(self):
        w = FlakyLoginWorker([RuntimeError("500")], logged_in_after_error=True)
        self.assertTrue(await w.login())
        self.assertEqual(w.posts, 1)

    async def test_an_error_is_retried_from_a_fresh_login_page(self):
        w = FlakyLoginWorker([RuntimeError("500"), True])
        self.assertTrue(await w.login())
        self.assertEqual(w.posts, 2)
        self.assertEqual(w.fetched.count("https://burningseries.ac/login"), 2)

    async def test_wrong_credentials_are_not_sent_again(self):
        w = FlakyLoginWorker([False])
        self.assertFalse(await w.login())
        self.assertEqual(w.posts, 1)

    async def test_it_gives_up_after_the_last_attempt(self):
        w = FlakyLoginWorker([RuntimeError("500")] * main._LOGIN_ATTEMPTS)
        self.assertFalse(await w.login())
        self.assertEqual(w.posts, main._LOGIN_ATTEMPTS)


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


class TestPreviewStillTriesEpisodeZero(HostFlowCase):
    """A listed episode 0 is tried on every run, even for a series whose
    counted episodes are all done: a placeholder that starts to stick is a
    change on the site worth hearing about, and one that does not is still
    reported, so it is always known which series carry one."""

    GROUPED = {"serienstream.to": ["https://serienstream.to/serie/one"]}

    async def _preview_with(self, season):
        def result(worker, url, action, slug):
            return SeriesResult(worker.host, worker.family, url, slug, action=action, seasons=[season], title=slug)

        with mock.patch.object(ScriptedWorker, "_result", result), redirect_stdout(io.StringIO()) as out:
            todo, done, broken = await main._preview(ACTION_WATCHED, dict(self.GROUPED))
        return todo, done, out.getvalue()

    async def test_a_series_at_target_with_a_listed_episode_zero_is_still_marked(self):
        season = SeasonOutcome(
            season=1, total=5, watched_before=5, watched_after=5, listed=True, ep0_before=False, ep0_after=False
        )
        todo, done, out = await self._preview_with(season)
        self.assertEqual((len(todo), len(done)), (1, 0), "it has to go to the marking pass")
        self.assertIn("E0: episode 0 is not watched — a known placeholder (ignore list); marked anyway", out)

    async def test_a_listed_episode_zero_that_already_stays_watched_is_flagged(self):
        season = SeasonOutcome(
            season=1, total=5, watched_before=5, watched_after=5, listed=True, ep0_before=True, ep0_after=True
        )
        todo, done, out = await self._preview_with(season)
        self.assertEqual((len(todo), len(done)), (0, 1), "nothing left to mark")
        self.assertIn("the entry may no longer be needed", out)


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
                main.SeriesPlan(f"https://serienstream.to/serie/{name}", "serienstream.to", "sto", name, [1], name)
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

        self.assertEqual(len(self.calls), 1, "resolve_active_hosts must not re-read a file the caller just parsed")

    async def test_without_a_preloaded_batch_the_file_is_still_read(self):
        """Callers that have not already parsed it must keep working."""
        await main.resolve_active_hosts("batch.txt")

        self.assertEqual(self.calls, ["batch.txt"])


# ==================== batch file sections / option 7 ====================
class SectionCase(unittest.TestCase):
    """A URL is permanent when it is on or below the KEEP marker, or when
    it is individually tagged with a leading '-' of its own."""

    KEEP = "# ===== KEEP BELOW (never cleared by option 7) ====="

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
    def test_a_file_using_neither_mechanism_is_entirely_temporary(self):
        """Every batch file written before this feature has neither."""
        permanent = main._classify_batch_lines(["https://a", "https://b"])
        self.assertEqual(permanent, [False, False])

    def test_the_marker_starts_the_keep_section_and_everything_below_is_permanent(self):
        permanent = main._classify_batch_lines(["https://a", self.KEEP, "https://b", "https://c"])
        self.assertEqual(permanent, [False, True, True, True])

    def test_the_marker_is_recognised_however_it_was_hand_edited(self):
        """It exists to be edited by hand, so spacing, case and the number of
        '=' signs must not matter."""
        for variant in ("# KEEP", "#keep", "#  ==== Keep below ====", "   # ===== KEEP =====", "# KEEP my stuff"):
            with self.subTest(variant=variant):
                permanent = main._classify_batch_lines(["https://a", variant, "https://b"])
                self.assertEqual(permanent, [False, True, True], f"{variant!r} not recognised")

    def test_an_ordinary_comment_is_not_mistaken_for_the_marker(self):
        permanent = main._classify_batch_lines(["# currently watching", "https://a"])
        self.assertEqual(permanent, [False, False])

    def test_a_dash_tags_a_single_url_wherever_it_is(self):
        permanent = main._classify_batch_lines(["https://a", "-https://b", "https://c"])
        self.assertEqual(permanent, [False, True, False])

    def test_a_dash_tagged_url_above_the_marker_is_still_permanent(self):
        permanent = main._classify_batch_lines(["-https://a", "https://b", self.KEEP, "https://c"])
        self.assertEqual(permanent, [True, False, True, True])

    def test_the_dash_works_with_or_without_a_space_after_it(self):
        permanent = main._classify_batch_lines(["- https://a", "-https://b"])
        self.assertEqual(permanent, [True, True])

    def test_several_dash_tagged_entries_can_be_scattered_through_the_file(self):
        permanent = main._classify_batch_lines(["https://a", "-https://b", "https://c", "-https://d"])
        self.assertEqual(permanent, [False, True, False, True])


class TestSectionAwareWriters(SectionCase):
    def test_added_urls_land_above_the_marker(self):
        """Appending to the end would drop them into the keep section and
        quietly make them permanent."""
        self.write("https://a", self.KEEP, "https://keepme")
        main._append_batch_urls(self.path, ["https://new"])

        lines = self.read()
        self.assertLess(lines.index("https://new"), lines.index(self.KEEP))
        self.assertIn("https://keepme", lines)

    def test_adding_to_a_file_with_no_marker_still_just_appends(self):
        self.write("https://a")
        main._append_batch_urls(self.path, ["https://new"])
        self.assertEqual(self.urls(), ["https://a", "https://new"])

    def test_replacing_the_working_list_keeps_the_permanent_block(self):
        self.write("https://old1", "https://old2", self.KEEP, "https://keepme")
        main._replace_batch_urls(self.path, ["https://fresh"])

        self.assertEqual(self.urls(), ["https://fresh", "https://keepme"])
        self.assertIn(self.KEEP, self.read())

    def test_replacing_also_keeps_a_dash_tagged_line_above_the_marker(self):
        self.write("https://old", "-https://pinned", self.KEEP, "https://keepme")
        main._replace_batch_urls(self.path, ["https://fresh"])

        # New URLs land after what survives, so a pinned entry keeps its place.
        self.assertEqual(self.urls(), ["-https://pinned", "https://fresh", "https://keepme"])

    def test_replacing_a_file_with_no_marker_still_keeps_a_dash_tagged_line(self):
        self.write("https://old1", "-https://pinned")
        main._replace_batch_urls(self.path, ["https://fresh"])
        self.assertEqual(self.urls(), ["-https://pinned", "https://fresh"])

    def test_replacing_a_file_using_neither_mechanism_replaces_everything(self):
        self.write("https://old1", "https://old2")
        main._replace_batch_urls(self.path, ["https://fresh"])
        self.assertEqual(self.urls(), ["https://fresh"])

    def test_section_counts(self):
        self.write("https://a", "https://b", "# a note", self.KEEP, "https://keepme")
        self.assertEqual(main._batch_section_counts(self.path), (2, 1))

    def test_section_counts_with_a_dash_tag_above_the_marker(self):
        self.write("https://a", "-https://b", self.KEEP, "https://keepme")
        self.assertEqual(main._batch_section_counts(self.path), (1, 2))


class TestLoadUrlBatchesWithSections(SectionCase):
    def test_permanent_entries_below_the_marker_are_loaded_like_any_other(self):
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

    def test_the_marker_is_not_reported_as_an_unsupported_line(self):
        self.write("https://serienstream.to/serie/one", self.KEEP)
        _grouped, rejected = main.load_url_batches(self.path)
        self.assertEqual(rejected, [])

    def test_a_dash_tagged_url_loads_with_the_dash_stripped(self):
        self.write("-https://serienstream.to/serie/pinned")
        grouped, rejected = main.load_url_batches(self.path)
        self.assertEqual(rejected, [])
        self.assertEqual(grouped["serienstream.to"], ["https://serienstream.to/serie/pinned"])

    def test_a_dash_with_a_space_before_the_url_also_loads(self):
        self.write("- https://serienstream.to/serie/pinned")
        grouped, _rejected = main.load_url_batches(self.path)
        self.assertEqual(grouped["serienstream.to"], ["https://serienstream.to/serie/pinned"])

    def test_a_bare_dash_with_nothing_usable_after_it_is_rejected_not_crashed(self):
        self.write("-not-a-url")
        _grouped, rejected = main.load_url_batches(self.path)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["reason"], "missing http(s)://")


class TestRewriteBatchUrlsPreservesDashTag(SectionCase):
    """A migrated URL keeps whatever tag it had, so a host failover does not
    silently un-pin a series the user marked permanent."""

    def test_a_dash_tagged_url_keeps_its_tag_after_migration(self):
        self.write("-https://old", "https://untouched")
        changed = main._rewrite_batch_urls(self.path, {"https://old": "https://new"})
        self.assertTrue(changed)
        self.assertEqual(self.read(), ["-https://new", "https://untouched"])

    def test_an_untagged_url_is_rewritten_plainly(self):
        self.write("https://old")
        changed = main._rewrite_batch_urls(self.path, {"https://old": "https://new"})
        self.assertTrue(changed)
        self.assertEqual(self.read(), ["https://new"])

    def test_no_match_leaves_the_dash_tagged_line_untouched(self):
        self.write("-https://old")
        changed = main._rewrite_batch_urls(self.path, {"https://other": "https://new"})
        self.assertFalse(changed)
        self.assertEqual(self.read(), ["-https://old"])


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

    async def test_a_dash_tagged_url_survives_with_no_marker_in_the_file(self):
        self.write("https://a", "-https://pinned")
        with mock.patch.object(main, "ask_yes_no", return_value=True):
            await main.clear_temporary_urls(self.path)
        self.assertEqual(self.urls(), ["-https://pinned"])

    async def test_a_file_using_neither_mechanism_clears_everything_but_warns_first(self):
        self.write("https://a", "https://b")
        printed = []
        with (
            mock.patch.object(main, "ask_yes_no", return_value=True),
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        ):
            await main.clear_temporary_urls(self.path)
        self.assertTrue(any("nothing is protected" in line for line in printed), "the user was not warned")
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


class TestBatchRewritersKeepThePermanentSection(SectionCase, unittest.IsolatedAsyncioTestCase):
    """Retrying now uses a separate retry batch file, so the user's main
    batch file is no longer overwritten."""

    async def test_retry_does_not_modify_the_main_batch_file(self):
        self.write("https://old", self.KEEP, "https://keepme")
        before = self.read()
        retry_path = os.path.join(self.dir.name, "retry.txt")
        with (
            mock.patch.object(main, "_load_failed_urls", return_value=["https://failed-one"]),
            mock.patch.object(main, "RETRY_BATCH_FILE", retry_path),
            mock.patch.object(main, "ask_yes_no", return_value=True),
        ):
            result = await main.retry_failed_urls(self.path)
        self.assertEqual(self.read(), before)
        self.assertEqual(result, retry_path)
        self.assertTrue(os.path.exists(result))
        self.assertEqual(Path(result).read_text(encoding="utf-8").splitlines(), ["https://failed-one"])

    async def test_retry_canceled_leaves_the_main_batch_file_alone(self):
        self.write("https://old", self.KEEP, "https://keepme")
        before = self.read()
        retry_path = os.path.join(self.dir.name, "retry.txt")
        with (
            mock.patch.object(main, "_load_failed_urls", return_value=["https://failed-one"]),
            mock.patch.object(main, "RETRY_BATCH_FILE", retry_path),
            mock.patch.object(main, "ask_yes_no", return_value=False),
        ):
            result = await main.retry_failed_urls(self.path)
        self.assertEqual(self.read(), before)
        self.assertEqual(result, self.path)
        self.assertFalse(os.path.exists(retry_path))

    async def test_retry_with_nothing_recorded_does_not_touch_any_file(self):
        self.write("https://old", self.KEEP, "https://keepme")
        before = self.read()
        retry_path = os.path.join(self.dir.name, "retry.txt")
        with (
            mock.patch.object(main, "_load_failed_urls", return_value=[]),
            mock.patch.object(main, "RETRY_BATCH_FILE", retry_path),
        ):
            result = await main.retry_failed_urls(self.path)
        self.assertEqual(self.read(), before)
        self.assertEqual(result, self.path)
        self.assertFalse(os.path.exists(retry_path))

    async def _paste(self, *answers):
        with (
            mock.patch.object(main, "DEFAULT_BATCH_FILE", self.path),
            mock.patch("builtins.input", side_effect=list(answers)),
        ):
            return await main._detect_and_add_input(self.path)

    async def test_overwriting_replaces_only_the_working_list(self):
        self.write("https://old", self.KEEP, "https://serienstream.to/serie/keepme")
        pasted = "https://serienstream.to/serie/fresh"
        await self._paste(pasted, "o")

        self.assertEqual(self.urls(), [pasted, "https://serienstream.to/serie/keepme"])
        self.assertIn(self.KEEP, self.read())

    async def test_adding_keeps_the_working_list_and_goes_above_the_marker(self):
        self.write("https://serienstream.to/serie/old", self.KEEP, "https://serienstream.to/serie/keepme")
        pasted = "https://serienstream.to/serie/fresh"
        await self._paste(pasted, "a")

        self.assertEqual(
            self.urls(),
            ["https://serienstream.to/serie/old", pasted, "https://serienstream.to/serie/keepme"],
        )
        self.assertIn(self.KEEP, self.read())

    async def test_adding_a_series_already_in_the_batch_changes_nothing(self):
        # Same series, other season and other mirror: still the same series.
        self.write("https://serienstream.to/serie/old/staffel-2", self.KEEP, "https://serienstream.to/serie/keepme")
        before = self.read()
        await self._paste("https://serienstream.cx/serie/OLD/staffel-5", "a")
        self.assertEqual(self.read(), before)

    async def test_enter_at_the_add_or_overwrite_question_cancels(self):
        self.write("https://serienstream.to/serie/old", self.KEEP, "https://serienstream.to/serie/keepme")
        before = self.read()
        result = await self._paste("https://serienstream.to/serie/fresh", "")
        self.assertEqual(self.read(), before)
        self.assertEqual(result, self.path)

    async def test_an_unclear_answer_is_asked_again(self):
        self.write("https://serienstream.to/serie/old")
        pasted = "https://serienstream.to/serie/fresh"
        await self._paste(pasted, "x", "a")
        self.assertEqual(self.urls(), ["https://serienstream.to/serie/old", pasted])

    async def test_with_no_temporary_urls_add_and_run_once_are_offered(self):
        self.write(self.KEEP, "https://serienstream.to/serie/keepme")
        pasted = "https://serienstream.to/serie/fresh"
        with redirect_stdout(io.StringIO()) as out:
            await self._paste(pasted, "a")
        self.assertEqual(self.urls(), [pasted, "https://serienstream.to/serie/keepme"])
        self.assertIn("r  run it once now", out.getvalue())
        self.assertNotIn("o  overwrite", out.getvalue(), "there is nothing to overwrite")

    async def test_overwrite_is_not_accepted_when_there_is_nothing_to_overwrite(self):
        self.write(self.KEEP, "https://serienstream.to/serie/keepme")
        before = self.read()
        with redirect_stdout(io.StringIO()):
            await self._paste("https://serienstream.to/serie/fresh", "o", "")
        self.assertEqual(self.read(), before)

    # ---- option 5, "run it once" ----

    async def _run_once(self, *answers, resolved=None):
        pasted = answers[0]
        resolved = {"serienstream.to": [pasted]} if resolved is None else resolved
        with (
            mock.patch.object(main, "_resolve_hosts", mock.AsyncMock(return_value=(resolved, {}, {}, {}))),
            mock.patch.object(main, "run_action", mock.AsyncMock()) as run,
            redirect_stdout(io.StringIO()) as out,
        ):
            result = await self._paste(*answers)
        return run, result, out.getvalue()

    async def test_run_once_marks_the_url_without_writing_it_anywhere(self):
        self.write("https://serienstream.to/serie/old", self.KEEP, "https://serienstream.to/serie/keepme")
        before = self.read()
        pasted = "https://serienstream.to/serie/fresh"
        run, result, _out = await self._run_once(pasted, "r", "w")
        run.assert_awaited_once_with(ACTION_WATCHED, {"serienstream.to": [pasted]}, [])
        self.assertEqual(self.read(), before, "the batch file is not touched")
        self.assertEqual(result, self.path, "the active batch stays what it was")

    async def test_run_once_can_mark_unwatched_and_works_with_an_empty_working_list(self):
        self.write(self.KEEP, "https://serienstream.to/serie/keepme")
        before = self.read()
        pasted = "https://serienstream.to/serie/fresh"
        run, _result, _out = await self._run_once(pasted, "r", "u")
        run.assert_awaited_once_with(ACTION_UNWATCHED, {"serienstream.to": [pasted]}, [])
        self.assertEqual(self.read(), before)

    async def test_enter_at_the_watched_or_unwatched_question_runs_nothing(self):
        self.write("https://serienstream.to/serie/old")
        before = self.read()
        run, _result, _out = await self._run_once("https://serienstream.to/serie/fresh", "r", "")
        run.assert_not_awaited()
        self.assertEqual(self.read(), before)

    async def test_run_once_with_no_reachable_mirror_marks_nothing(self):
        self.write("https://serienstream.to/serie/old")
        run, _result, out = await self._run_once("https://serienstream.to/serie/fresh", "r", "w", resolved={})
        run.assert_not_awaited()
        self.assertIn("no reachable sto mirror", out)

    async def test_run_once_moves_to_the_working_mirror_in_memory_only(self):
        self.write("https://serienstream.to/serie/old")
        before = self.read()
        pasted = "https://serienstream.cx/serie/fresh"
        with (
            mock.patch.object(
                main, "check_hosts", mock.AsyncMock(side_effect=lambda hosts: dict.fromkeys(hosts, "OK"))
            ),
            mock.patch.object(main, "run_action", mock.AsyncMock()) as run,
            redirect_stdout(io.StringIO()),
        ):
            await self._paste(pasted, "r", "w")
        run.assert_awaited_once_with(ACTION_WATCHED, {"serienstream.to": ["https://serienstream.to/serie/fresh"]}, [])
        self.assertEqual(self.read(), before)

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
        self.retry_batch = os.path.join(self.dir.name, "retry_batch.txt")
        Path(self.batch).write_text("", encoding="utf-8")

    def _with_retry_batch(self):
        return mock.patch.object(main, "RETRY_BATCH_FILE", self.retry_batch)

    async def _retry_output(self):
        printed = []
        with (
            self._with_retry_batch(),
            mock.patch.object(main, "ask_yes_no", return_value=False),
            mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(" ".join(str(x) for x in a))),
        ):
            await main.retry_failed_urls(self.batch)
        return "\n".join(printed)

    async def test_it_names_the_action_and_the_option_to_use(self):
        self.record(ACTION_WATCHED, {self.X: False})
        with self._with_retry_batch():
            body = await self._retry_output()
        self.assertIn("WATCHED", body)
        self.assertIn("option 1", body)

    async def test_unwatched_failures_point_at_option_2(self):
        self.record(ACTION_UNWATCHED, {self.X: False})
        with self._with_retry_batch():
            body = await self._retry_output()
        self.assertIn("UNWATCHED", body)
        self.assertIn("option 2", body)

    async def test_retry_creates_the_retry_batch_and_returns_it(self):
        self.record(ACTION_WATCHED, {self.X: False})
        with (
            self._with_retry_batch(),
            mock.patch.object(main, "ask_yes_no", return_value=True),
        ):
            result = await main.retry_failed_urls(self.batch)
        self.assertEqual(result, self.retry_batch)
        self.assertEqual(main._load_failed_urls(), [self.X])
        self.assertEqual(Path(self.retry_batch).read_text(encoding="utf-8").splitlines(), [self.X])

    async def test_retry_does_not_overwrite_the_original_batch_file(self):
        original_body = "# existing\nhttps://original\n"
        Path(self.batch).write_text(original_body, encoding="utf-8")
        self.record(ACTION_WATCHED, {self.X: False})
        with (
            self._with_retry_batch(),
            mock.patch.object(main, "ask_yes_no", return_value=True),
        ):
            await main.retry_failed_urls(self.batch)
        self.assertEqual(Path(self.batch).read_text(encoding="utf-8"), original_body)

    async def test_retry_canceled_leaves_files_alone(self):
        original_body = "# existing\nhttps://original\n"
        Path(self.batch).write_text(original_body, encoding="utf-8")
        self.record(ACTION_WATCHED, {self.X: False})
        with (
            self._with_retry_batch(),
            mock.patch.object(main, "ask_yes_no", return_value=False),
        ):
            result = await main.retry_failed_urls(self.batch)
        self.assertEqual(result, self.batch)
        self.assertEqual(Path(self.batch).read_text(encoding="utf-8"), original_body)
        self.assertFalse(os.path.exists(self.retry_batch))

    async def test_retry_with_nothing_recorded_does_not_touch_any_file(self):
        original_body = "# existing\nhttps://original\n"
        Path(self.batch).write_text(original_body, encoding="utf-8")
        with self._with_retry_batch():
            await main.retry_failed_urls(self.batch)
        self.assertEqual(Path(self.batch).read_text(encoding="utf-8"), original_body)
        self.assertFalse(os.path.exists(self.retry_batch))

    async def test_a_mixed_list_warns_that_both_options_are_needed(self):
        self.record(ACTION_WATCHED, {self.X: False})
        self.record(ACTION_UNWATCHED, {self.Y: False})
        body = await self._retry_output()
        self.assertIn("different actions", body)


if __name__ == "__main__":
    unittest.main()
