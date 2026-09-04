"""Tests for watchmaker's small parsing, host and reporting helpers.

These sit under the DomainWorker rather than inside it, so they can be checked
without a network at all. Several of them decide whether a request is aimed at
the right host or whether a site's answer counts as a verdict, which makes
them worth pinning even though each is only a few lines.

Run with:  python -m unittest discover -s tests
"""

import io
import unittest
from contextlib import redirect_stdout

import httpx

import main as wm


class TestAttrCoercion(unittest.TestCase):
    """BeautifulSoup attributes are str, list[str] or absent; callers want one type."""

    def test_a_string_attribute_is_returned(self):
        self.assertEqual(wm._attr_str("value"), "value")

    def test_a_multi_valued_attribute_is_rejected(self):
        """A class attribute parses as a list; treating it as a str would crash later."""
        self.assertIsNone(wm._attr_str(["a", "b"]))

    def test_a_missing_attribute_is_none(self):
        self.assertIsNone(wm._attr_str(None))

    def test_a_numeric_string_attribute_parses(self):
        self.assertEqual(wm._attr_int("42"), 42)

    def test_an_int_passes_through(self):
        self.assertEqual(wm._attr_int(7), 7)

    def test_a_non_numeric_string_is_none_not_an_error(self):
        self.assertIsNone(wm._attr_int("not-a-number"))

    def test_a_list_attribute_is_none(self):
        self.assertIsNone(wm._attr_int(["1"]))

    def test_a_missing_attribute_is_none_as_an_int_too(self):
        self.assertIsNone(wm._attr_int(None))

    def test_a_negative_value_is_preserved(self):
        self.assertEqual(wm._attr_int("-3"), -3)


class TestJsonBody(unittest.TestCase):
    """None means "no verdict", never "refused" — the distinction matters."""

    def test_a_json_object_is_returned(self):
        self.assertEqual(wm._json_body(httpx.Response(200, json={"ok": True})), {"ok": True})

    def test_an_empty_body_is_no_verdict(self):
        self.assertIsNone(wm._json_body(httpx.Response(200, text="")))

    def test_a_whitespace_only_body_is_no_verdict(self):
        self.assertIsNone(wm._json_body(httpx.Response(200, text="   \n  ")))

    def test_a_plain_text_body_is_no_verdict(self):
        self.assertIsNone(wm._json_body(httpx.Response(200, text="OK")))

    def test_html_served_where_json_was_expected_is_no_verdict(self):
        self.assertIsNone(wm._json_body(httpx.Response(200, text="<html><body>nope</body></html>")))

    def test_a_json_array_is_not_an_object_so_no_verdict(self):
        """Callers index the result by key; a list would raise."""
        self.assertIsNone(wm._json_body(httpx.Response(200, json=[1, 2, 3])))

    def test_a_json_scalar_is_not_an_object(self):
        self.assertIsNone(wm._json_body(httpx.Response(200, json=5)))


class TestHostNormalisation(unittest.TestCase):
    def test_case_is_folded(self):
        self.assertEqual(wm._normalize_host("AniWorld.TO"), "aniworld.to")

    def test_a_www_prefix_is_dropped(self):
        self.assertEqual(wm._normalize_host("www.aniworld.to"), "aniworld.to")

    def test_a_www_prefix_is_dropped_after_case_folding(self):
        self.assertEqual(wm._normalize_host("WWW.Aniworld.to"), "aniworld.to")

    def test_a_host_merely_starting_with_w_is_untouched(self):
        self.assertEqual(wm._normalize_host("wwwe.example"), "wwwe.example")

    def test_an_already_normal_host_is_unchanged(self):
        self.assertEqual(wm._normalize_host("bs.to"), "bs.to")


class TestSchemeForHost(unittest.TestCase):
    """IP mirrors are http-only; domains are https."""

    def test_a_domain_gets_https(self):
        self.assertEqual(wm._scheme_for_host("aniworld.to"), "https")

    def test_a_raw_ip_gets_http(self):
        self.assertEqual(wm._scheme_for_host("186.2.175.111"), "http")

    def test_base_url_builds_from_the_scheme_rule(self):
        self.assertEqual(wm.base_url("aniworld.to"), "https://aniworld.to")
        self.assertEqual(wm.base_url("186.2.175.111"), "http://186.2.175.111")


class TestUrlForHost(unittest.TestCase):
    def test_the_path_is_kept_and_the_host_swapped(self):
        moved = wm._url_for_host("https://aniworld.to/anime/stream/one-piece", "aniworld.cc")
        self.assertEqual(moved, "https://aniworld.cc/anime/stream/one-piece")

    def test_moving_to_an_ip_mirror_downgrades_the_scheme(self):
        moved = wm._url_for_host("https://aniworld.to/anime/stream/x", "186.2.175.111")
        self.assertIsNotNone(moved)
        assert moved is not None  # narrows for the type checker
        self.assertTrue(moved.startswith("http://"))

    def test_moving_from_an_ip_mirror_to_a_domain_restores_https(self):
        """The scheme follows the target, not the source, or a domain gets downgraded."""
        moved = wm._url_for_host("http://186.2.175.111/anime/stream/x", "aniworld.to")
        self.assertEqual(moved, "https://aniworld.to/anime/stream/x")

    def test_a_query_string_survives_the_move(self):
        moved = wm._url_for_host("https://bs.to/serie/x?s=2", "bs.to")
        self.assertIsNotNone(moved)
        assert moved is not None  # narrows for the type checker
        self.assertIn("?s=2", moved)

    def test_a_url_with_no_host_cannot_be_moved(self):
        self.assertIsNone(wm._url_for_host("/anime/stream/x", "aniworld.to"))


class TestFailedResult(unittest.TestCase):
    """The placeholder built when a series never got as far as being marked."""

    def test_it_is_marked_not_ok_and_carries_the_note(self):
        result = wm._failed_result("aniworld.to", "aniworld", "https://aniworld.to/anime/stream/x", "watched", "boom")
        self.assertFalse(result.ok)
        self.assertEqual(result.note, "boom")
        self.assertEqual(result.action, "watched")

    def test_the_slug_is_extracted_when_the_url_allows_it(self):
        result = wm._failed_result("aniworld.to", "aniworld", "https://aniworld.to/anime/stream/one-piece", "a", "n")
        self.assertEqual(result.slug, "one-piece")

    def test_an_unparseable_url_falls_back_to_the_url_itself(self):
        """Better a long label than an exception while reporting a failure."""
        result = wm._failed_result("aniworld.to", "aniworld", "not-a-url", "a", "n")
        self.assertEqual(result.slug, "not-a-url")

    def test_a_failed_result_is_never_at_target(self):
        result = wm._failed_result("bs.to", "bsto", "https://bs.to/serie/x", "watched", "n")
        self.assertFalse(result.at_target)


class TestPrintBanner(unittest.TestCase):
    def test_the_banner_names_the_tool(self):
        out = io.StringIO()
        with redirect_stdout(out):
            wm.print_banner()
        self.assertIn("watchmaker", out.getvalue())


class TestPrintRunSummary(unittest.TestCase):
    """The last thing printed after a run; its numbers are the whole report."""

    def _result(self, slug, total, after, ok=True):
        return wm.SeriesResult(
            host="aniworld.to",
            family="aniworld",
            url=f"https://aniworld.to/anime/stream/{slug}",
            slug=slug,
            action=wm.ACTION_WATCHED,
            seasons=[wm.SeasonOutcome(season=1, total=total, watched_before=0, watched_after=after)],
            ok=ok,
        )

    def _render(self, report, results):
        out = io.StringIO()
        with redirect_stdout(out):
            wm._print_run_summary(report, results)
        return out.getvalue()

    def test_the_headline_counts_are_printed(self):
        report = wm.RunReport(total_urls=2, successful=1, failed=1)
        out = self._render(report, [self._result("a", 10, 10), self._result("b", 10, 3)])
        self.assertIn("RUN SUMMARY", out)
        self.assertIn("Series processed", out)
        self.assertIn("Successful", out)
        self.assertIn("Failed", out)

    def test_episode_totals_are_summed_across_every_series(self):
        report = wm.RunReport(total_urls=2, successful=2)
        out = self._render(report, [self._result("a", 10, 10), self._result("b", 5, 5)])
        self.assertIn("15/15", out)

    def test_every_series_appears_in_the_table(self):
        report = wm.RunReport(total_urls=2, successful=2)
        out = self._render(report, [self._result("alpha", 1, 1), self._result("beta", 1, 1)])
        self.assertIn("alpha", out)
        self.assertIn("beta", out)

    def test_a_run_with_no_results_still_prints_its_metrics(self):
        """An all-rejected batch must not render an empty, meaningless box."""
        out = self._render(wm.RunReport(total_urls=0), [])
        self.assertIn("Series processed", out)
        self.assertIn("0/0", out)

    def test_the_failed_list_path_is_shown_only_when_something_failed(self):
        clean = self._render(wm.RunReport(total_urls=1, successful=1), [self._result("a", 1, 1)])
        self.assertNotIn("Failed list", clean)

        failed = self._render(wm.RunReport(total_urls=1, failed=1), [self._result("a", 1, 0, ok=False)])
        self.assertIn("Failed list", failed)

    def test_unsupported_lines_are_counted_only_when_present(self):
        report = wm.RunReport(total_urls=1, successful=1, rejected=[{"line": "nonsense"}])
        self.assertIn("Unsupported lines", self._render(report, [self._result("a", 1, 1)]))


if __name__ == "__main__":
    unittest.main()
