"""Pin the lxml behaviours watchmaker depends on.

These tests exist so that upgrading lxml (or any dependency that pulls a
different lxml) is a checkable change: if a new release alters how
``document_fromstring`` wraps fragments, how ``itertext()`` yields text
nodes, how ``xpath()`` reports an empty match, or how the XPath string
functions behave, one of these fails and names the exact primitive that
moved.

They are deliberately about lxml's *contract*, not about the site's markup:
the site-specific selectors are covered by ``test_watchmaker.py``. What is
pinned here is the layer underneath them.

Run with:  python -m unittest discover -s tests
"""

import unittest

import main as wm


class TestMakeDoc(unittest.TestCase):
    """document_fromstring must wrap fragments the way BeautifulSoup did."""

    def test_a_bare_table_fragment_is_wrapped_in_html_body(self):
        """A fragment whose outermost element is the table must still be
        searchable with .//table. lxml.html.fromstring returns a bare
        fragment root and would fail this; document_fromstring must not."""
        doc = wm.make_doc("<table class='seasonEpisodesList'><tr></tr></table>")
        self.assertIsNotNone(doc)
        self.assertTrue(doc.xpath(".//table"), "fragment was not wrapped; .//table matched nothing")

    def test_a_full_page_parses(self):
        doc = wm.make_doc("<html><body><h1>Title</h1></body></html>")
        self.assertIsNotNone(doc)
        self.assertEqual(wm._stripped_text(wm._first(doc, ".//h1")), "Title")

    def test_empty_body_is_none(self):
        self.assertIsNone(wm.make_doc(""))

    def test_whitespace_only_body_is_none(self):
        self.assertIsNone(wm.make_doc("   \n\t  "))


class TestItertext(unittest.TestCase):
    """_stripped_text and _spaced_text depend on itertext() yielding each
    text node separately, so per-node strip() works. If lxml ever merged
    adjacent text nodes, titles would glue into one word."""

    def test_stripped_text_glues_inline_markup(self):
        doc = wm.make_doc("<strong>Hello <em> World </em></strong>")
        self.assertEqual(wm._stripped_text(wm._first(doc, ".//strong")), "HelloWorld")

    def test_spaced_text_separates_inline_markup(self):
        doc = wm.make_doc("<h1>Harry Potter<small>Specials</small></h1>")
        self.assertEqual(wm._spaced_text(wm._first(doc, ".//h1")), "Harry Potter Specials")

    def test_stripped_text_strips_each_text_node(self):
        """itertext() yields one node per text run separated by child
        elements; each is stripped before joining. A single text run keeps
        its internal whitespace (lxml does not collapse it), which is why
        the readers rely on inline markup, not whitespace, to split titles."""
        doc = wm.make_doc("<p>  A <em> B </em> C  </p>")
        self.assertEqual(wm._stripped_text(wm._first(doc, ".//p")), "ABC")


class TestXPathContract(unittest.TestCase):
    """_first depends on xpath() returning a list, empty when nothing matches."""

    def test_first_returns_none_on_no_match(self):
        doc = wm.make_doc("<html><body></body></html>")
        self.assertIsNone(wm._first(doc, ".//h1"))

    def test_first_returns_the_first_match(self):
        doc = wm.make_doc("<html><body><h1>A</h1><h1>B</h1></body></html>")
        self.assertEqual(wm._stripped_text(wm._first(doc, ".//h1")), "A")

    def test_xpath_returns_a_list_not_a_scalar(self):
        doc = wm.make_doc("<html><body><h1>A</h1></body></html>")
        result = doc.xpath(".//h1")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)


class TestClassPredicate(unittest.TestCase):
    """_hc must match a whole class token, never a substring."""

    def _matches(self, class_attr: str, token: str) -> bool:
        doc = wm.make_doc(f"<div class='{class_attr}'></div>")
        return wm._first(doc, f".//*[{wm._hc(token)}]") is not None

    def test_exact_token_matches(self):
        self.assertTrue(self._matches("seen", "seen"))

    def test_substring_does_not_match(self):
        """contains(@class, 'seen') would match 'unseen'; the predicate must not."""
        self.assertFalse(self._matches("unseen", "seen"))

    def test_token_among_others_matches(self):
        self.assertTrue(self._matches("foo seen bar", "seen"))

    def test_whitespace_insensitive(self):
        self.assertTrue(self._matches("  seen  ", "seen"))


class TestClassTokens(unittest.TestCase):
    """_class_tokens splits the raw class string the way bs4 used to hand it
    back, so substring tests cannot match a longer token."""

    def test_splits_on_whitespace(self):
        doc = wm.make_doc("<div class='btn-glass-primary active'></div>")
        self.assertEqual(wm._class_tokens(wm._first(doc, ".//div")), ["btn-glass-primary", "active"])

    def test_missing_class_is_empty_list(self):
        doc = wm.make_doc("<div></div>")
        self.assertEqual(wm._class_tokens(wm._first(doc, ".//div")), [])


class TestErrorDetection(unittest.TestCase):
    """_check_error_page is a thin wrapper over xpath(); pin its lxml-level
    behaviour so a parser change cannot silently flip it."""

    def test_error_page_detected_from_title(self):
        self.assertEqual(wm._check_error_page(wm.make_doc("<title>404 Not Found</title>"), "sto"), "404")

    def test_error_page_detected_from_h2(self):
        self.assertEqual(wm._check_error_page(wm.make_doc("<h2>503</h2>"), "aniworld"), "503")

    def test_real_series_page_is_not_an_error(self):
        doc = wm.make_doc("<div id='season-nav'><a data-season-pill='1'>S1</a></div>")
        self.assertIsNone(wm._check_error_page(doc, "sto"))


if __name__ == "__main__":
    unittest.main()
