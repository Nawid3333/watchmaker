"""Only the answers a prompt offers count; nothing is assumed.

watchmaker's y/n prompts took Enter as their default ("[y/N]") and accepted
"yes" and "no" besides y and n; the add/overwrite/run question took whole
words, and Enter cancelled it and the option 5 input. End of input raised an
EOFError traceback out of the whole program, and a wrong answer was asked
again for ever.

Every prompt now goes through term.confirm or term.ask, the same helpers the
sibling scrapers use: y or n in either case, or a listed option, and anything
else is asked again with what is allowed. There are no defaults. End of
input, or MAX_UNRECOGNIZED wrong answers in a row, gives the answer that
changes nothing.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
import term

REPO = Path(__file__).resolve().parent.parent

# The functions allowed to call input() themselves: term.cinput is the one
# every prompt goes through, and option 5 takes free text (a URL or a path)
# in a loop bounded by MAX_UNRECOGNIZED.
INPUT_CALLERS = {"term.py": {"cinput"}, "main.py": {"_detect_and_add_input"}}


class _Script:
    """Hand out *answers* one per prompt and record every prompt shown.

    Running out is a failure, not an end of input: a prompt that asks more
    often than the test expects is exactly what these tests are here to see.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, prompt=""):
        self.asked.append(prompt)
        if not self.answers:
            raise AssertionError(f"asked again after the scripted answers ran out: {prompt!r}")
        return self.answers.pop(0)


@contextlib.contextmanager
def _captured():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


def _confirm(*answers):
    script = _Script(*answers)
    with mock.patch("builtins.input", script), _captured() as out:
        result = term.confirm("Go? (y/n): ")
    return result, script.asked, term.strip_ansi(out.getvalue())


def _ask(*answers):
    script = _Script(*answers)
    with mock.patch("builtins.input", script), _captured() as out:
        result = term.ask("Choose (a/o/c): ", ("a", "o", "c"), safe="c")
    return result, script.asked, term.strip_ansi(out.getvalue())


class ConfirmTests(unittest.TestCase):
    def test_y_and_n_answer_in_either_case(self):
        for answer, expected in (("y", True), ("Y", True), ("n", False), ("N", False)):
            with self.subTest(answer=answer):
                result, asked, _ = _confirm(answer)
                self.assertIs(result, expected)
                self.assertEqual(len(asked), 1)

    def test_nothing_but_y_or_n_is_accepted(self):
        for wrong in ("yes", "no", "j", "ja", "nein", "yn", "1", "0", "q"):
            for then, expected in (("y", True), ("n", False)):
                with self.subTest(wrong=wrong, then=then):
                    result, asked, shown = _confirm(wrong, then)
                    self.assertIs(result, expected)
                    self.assertEqual(len(asked), 2, "a wrong answer was taken instead of asked again")
                    self.assertIn(f"{wrong!r} is not an option - type y or n.", shown)

    def test_enter_alone_is_not_an_answer(self):
        for blank in ("", "   "):
            with self.subTest(blank=repr(blank)):
                result, asked, shown = _confirm(blank, "n")
                self.assertFalse(result)
                self.assertEqual(len(asked), 2)
                self.assertIn("No answer - type y or n.", shown)

    def test_a_right_answer_after_several_wrong_ones_still_counts(self):
        result, asked, _ = _confirm(*["x"] * (term.MAX_UNRECOGNIZED - 1), "y")
        self.assertTrue(result)
        self.assertEqual(len(asked), term.MAX_UNRECOGNIZED)

    def test_end_of_input_answers_no(self):
        with mock.patch("builtins.input", side_effect=EOFError), _captured():
            self.assertFalse(term.confirm("Go? (y/n): "))

    def test_endless_wrong_answers_stop_and_answer_no(self):
        feed = mock.Mock(return_value="x")
        with mock.patch("builtins.input", feed), _captured() as out:
            self.assertFalse(term.confirm("Go? (y/n): "))
        self.assertEqual(feed.call_count, term.MAX_UNRECOGNIZED)
        self.assertIn(f"No usable answer after {term.MAX_UNRECOGNIZED} tries", term.strip_ansi(out.getvalue()))


class AskTests(unittest.TestCase):
    def test_a_listed_option_is_returned_in_either_case(self):
        for answer, expected in (("a", "a"), ("O", "o"), ("c", "c")):
            with self.subTest(answer=answer):
                self.assertEqual(_ask(answer)[0], expected)

    def test_an_unlisted_answer_is_asked_again_and_told_what_is_allowed(self):
        for wrong in ("add", "overwrite", "x", "r"):
            with self.subTest(wrong=wrong):
                result, asked, shown = _ask(wrong, "a")
                self.assertEqual((result, len(asked)), ("a", 2))
                self.assertIn(f"{wrong!r} is not an option - type one of a, c, o.", shown)

    def test_enter_is_never_an_answer(self):
        result, asked, shown = _ask("", "o")
        self.assertEqual((result, len(asked)), ("o", 2))
        self.assertIn("No answer", shown)

    def test_end_of_input_gives_the_safe_answer(self):
        with mock.patch("builtins.input", side_effect=EOFError), _captured():
            self.assertEqual(term.ask("? ", ("a", "c"), safe="c"), "c")

    def test_endless_wrong_answers_give_the_safe_answer(self):
        feed = mock.Mock(return_value="x")
        with mock.patch("builtins.input", feed), _captured():
            self.assertEqual(term.ask("? ", ("a", "c"), safe="c"), "c")
        self.assertEqual(feed.call_count, term.MAX_UNRECOGNIZED)


class MainMenuTests(unittest.TestCase):
    """The menu itself: end of input used to end the program in a traceback."""

    def _run_menu(self, feed):
        home = Path(tempfile.mkdtemp())
        batch = home / "series_urls.txt"
        batch.write_text("", encoding="utf-8")
        with (
            mock.patch.object(main, "DEFAULT_BATCH_FILE", str(batch)),
            mock.patch.object(main, "FAILED_URLS_FILE", str(home / "failed.json")),
            mock.patch.object(main, "setup_logging"),
            mock.patch.object(main, "run_action", mock.AsyncMock()) as run,
            mock.patch("builtins.input", feed),
            _captured() as out,
        ):
            asyncio.run(main.main())
        return run, term.strip_ansi(out.getvalue())

    def test_end_of_input_at_the_menu_exits_cleanly(self):
        run, shown = self._run_menu(mock.Mock(side_effect=EOFError))
        run.assert_not_awaited()
        self.assertIn("exiting.", shown)

    def test_endless_typos_at_the_menu_exit_without_doing_anything(self):
        feed = mock.Mock(return_value="9")
        run, shown = self._run_menu(feed)
        self.assertEqual(feed.call_count, term.MAX_UNRECOGNIZED)
        run.assert_not_awaited()
        self.assertIn("'9' is not an option - type a number from 0 to 7.", shown)


def _docstring_ids(tree):
    """Return the ids of every docstring node; a docstring may describe a prompt."""
    ids = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            ids.add(id(body[0].value))
    return ids


def _strings(path, *, exempt_confirm: bool):
    """Yield (node, text) for each string constant in *path* that is not a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    exempt = _docstring_ids(tree)
    if exempt_confirm:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "confirm":
                exempt.update(id(inner) for inner in ast.walk(node))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in exempt:
            yield node, node.value


def _yn_prompts_outside_confirm(path):
    """Return "file:line" for each "(y/n)" string not handed to term.confirm."""
    return [f"{path.name}:{node.lineno}" for node, text in _strings(path, exempt_confirm=True) if "(y/n)" in text]


# How a prompt used to offer an answer for Enter.
_DEFAULT_MARKERS = ("[default", "[y/N]", "[Y/n]", "[n]", "[y]", "Enter cancels", "Enter = cancel", "Press Enter")


def _offered_defaults(path):
    """Return "file:line" for each string that offers Enter an answer."""
    return [
        f"{path.name}:{node.lineno}"
        for node, text in _strings(path, exempt_confirm=False)
        if any(marker in text for marker in _DEFAULT_MARKERS)
    ]


def _input_calls_outside(path, allowed):
    """Return "file:line" for each input() call made outside the *allowed* functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            inside = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "input"
                and function not in allowed
            ):
                hits.append(f"{path.name}:{child.lineno}")
            visit(child, inside)

    visit(tree, None)
    return hits


class SourceGuardTests(unittest.TestCase):
    """None of the rules can quietly come back in a later change."""

    sources = [REPO / "main.py", REPO / "term.py"]

    def test_every_y_n_prompt_uses_term_confirm(self):
        offenders = [hit for path in self.sources for hit in _yn_prompts_outside_confirm(path)]
        self.assertEqual(offenders, [], "a y/n prompt reads input directly; use term.confirm")

    def test_no_prompt_offers_a_default(self):
        offenders = [hit for path in self.sources for hit in _offered_defaults(path)]
        self.assertEqual(offenders, [], "a prompt offers Enter an answer; make every answer typed")

    def test_every_other_prompt_goes_through_term(self):
        offenders = [hit for path in self.sources for hit in _input_calls_outside(path, INPUT_CALLERS[path.name])]
        self.assertEqual(offenders, [], "a prompt calls input() itself; use term.ask or term.confirm")

    def test_the_guards_see_what_they_are_meant_to(self):
        planted = Path(tempfile.mkdtemp()) / "planted.py"
        planted.write_text(
            'def f():\n    """Asks (y/n) [y/N] in a docstring - allowed."""\n'
            '    return input("Delete all? (y/n) [y/N]: ") == "y"\n',
            encoding="utf-8",
        )
        self.assertEqual(_yn_prompts_outside_confirm(planted), ["planted.py:3"])
        self.assertEqual(_offered_defaults(planted), ["planted.py:3"])
        self.assertEqual(_input_calls_outside(planted, set()), ["planted.py:3"])
        self.assertEqual(_input_calls_outside(planted, {"f"}), [])


if __name__ == "__main__":
    unittest.main()
