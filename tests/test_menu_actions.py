"""The menu actions that read and write the user's files.

Import, export and the printed summaries were the largest untested block in
main.py. They matter because they are the paths that *write*: export appends
to the sibling scrapers' series_urls.txt files, and import appends to the
batch file. A mistake here silently edits a list the user maintains by hand.

Everything runs against tmp_path; nothing here can reach a real scraper list.

Style note for future edits
---------------------------
``_answers`` scripts ``input()`` and records the prompts. Its ``default`` is
the *safe* answer, so a prompt added later cannot make an old test start
approving a write it never meant to.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io

import pytest

import main as wm


@contextlib.contextmanager
def _answers(*scripted: str, default: str = "n"):
    remaining = list(scripted)
    asked: list[str] = []

    def fake_input(prompt: str = "") -> str:
        asked.append(prompt)
        return remaining.pop(0) if remaining else default

    real = builtins.input
    builtins.input = fake_input
    try:
        yield asked
    finally:
        builtins.input = real


@contextlib.contextmanager
def _captured():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


@pytest.fixture
def batch(tmp_path):
    """A batch file with one series per family."""
    path = tmp_path / "series_urls.txt"
    path.write_text(
        "https://serienstream.to/serie/sto-show\n"
        "https://aniworld.to/anime/stream/ani-show\n"
        "https://burningseries.ac/serie/bs-show\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def exports(tmp_path, monkeypatch):
    """Point every family's export target at tmp_path and return the paths."""
    paths = {family: tmp_path / f"{family}_series_urls.txt" for family in ("aniworld", "bs", "sto")}
    monkeypatch.setattr(wm, "SERIES_URLS_EXPORTS", {k: str(v) for k, v in paths.items()})
    return paths


# ── grouping ────────────────────────────────────────────────────────────────


class TestUrlsByFamily:
    def test_each_host_lands_under_its_family(self, batch):
        grouped, _rejected = wm.load_url_batches(str(batch))
        by_family = wm._urls_by_family(grouped)
        assert set(by_family) == {"sto", "aniworld", "bs"}
        assert all(len(urls) == 1 for urls in by_family.values())

    def test_an_unknown_host_is_not_invented_into_a_family(self):
        assert wm._urls_by_family({"nowhere.example": ["https://nowhere.example/serie/x"]}) == {}


# ── export ──────────────────────────────────────────────────────────────────


class TestAppendUrlsToScraperLists:
    def test_new_urls_are_appended_to_an_existing_list(self, exports):
        exports["sto"].write_text("https://serienstream.to/serie/already-there\n", encoding="utf-8")
        with _captured():
            wm.append_urls_to_scraper_lists({"sto": {"https://serienstream.to/serie/new-one"}})
        written = exports["sto"].read_text(encoding="utf-8")
        assert "already-there" in written and "new-one" in written

    def test_a_url_already_present_is_not_duplicated(self, exports):
        exports["sto"].write_text("https://serienstream.to/serie/dup\n", encoding="utf-8")
        with _captured() as out:
            wm.append_urls_to_scraper_lists({"sto": {"https://serienstream.to/serie/dup"}})
        assert exports["sto"].read_text(encoding="utf-8").count("dup") == 1
        assert "nothing new" in out.getvalue()

    def test_a_missing_export_file_is_only_created_with_permission(self, exports):
        assert not exports["sto"].exists()
        with _captured() as out, _answers("n", default="n"):
            wm.append_urls_to_scraper_lists({"sto": {"https://serienstream.to/serie/x"}})
        assert not exports["sto"].exists(), "declining must not create the file"
        assert "skipped" in out.getvalue()

    def test_agreeing_creates_the_missing_export_file(self, exports):
        with _captured(), _answers("y", default="n"):
            wm.append_urls_to_scraper_lists({"sto": {"https://serienstream.to/serie/x"}})
        assert exports["sto"].exists()
        assert "serie/x" in exports["sto"].read_text(encoding="utf-8")

    def test_a_family_with_export_disabled_is_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setattr(wm, "SERIES_URLS_EXPORTS", {"sto": None})
        with _captured():
            wm.append_urls_to_scraper_lists({"sto": {"https://serienstream.to/serie/x"}})

    def test_appending_repairs_a_missing_trailing_newline(self, exports):
        """Without the repair the new URL splices onto the end of the last one."""
        exports["sto"].write_text("https://serienstream.to/serie/no-newline", encoding="utf-8")
        with _captured():
            wm.append_urls_to_scraper_lists({"sto": {"https://serienstream.to/serie/second"}})
        lines = [line for line in exports["sto"].read_text(encoding="utf-8").splitlines() if line.strip()]
        assert lines == ["https://serienstream.to/serie/no-newline", "https://serienstream.to/serie/second"]


class TestExportUrls:
    def test_declining_writes_nothing(self, batch, exports):
        with _captured(), _answers("n", default="n"):
            asyncio.run(wm.export_urls(str(batch)))
        assert not any(path.exists() for path in exports.values())

    def test_accepting_writes_every_family(self, batch, exports):
        with _captured(), _answers("y", "y", "y", "y", default="y"):
            asyncio.run(wm.export_urls(str(batch)))
        assert all(path.exists() for path in exports.values())

    def test_an_empty_batch_reports_nothing_to_export(self, tmp_path, exports):
        empty = tmp_path / "empty.txt"
        empty.write_text("# only a comment\n", encoding="utf-8")
        with _captured() as out:
            asyncio.run(wm.export_urls(str(empty)))
        assert "0 series" in out.getvalue()


# ── import ──────────────────────────────────────────────────────────────────


class TestImportUrls:
    def test_urls_are_imported_into_the_batch(self, tmp_path, exports):
        exports["sto"].write_text("https://serienstream.to/serie/imported-one\n", encoding="utf-8")
        batch = tmp_path / "batch.txt"
        batch.write_text("", encoding="utf-8")
        with _captured(), _answers("y", default="y"):
            asyncio.run(wm.import_urls(str(batch)))
        assert "imported-one" in batch.read_text(encoding="utf-8")

    def test_declining_leaves_the_batch_untouched(self, tmp_path, exports):
        exports["sto"].write_text("https://serienstream.to/serie/imported-one\n", encoding="utf-8")
        batch = tmp_path / "batch.txt"
        batch.write_text("# mine\n", encoding="utf-8")
        with _captured(), _answers("n", default="n"):
            asyncio.run(wm.import_urls(str(batch)))
        assert batch.read_text(encoding="utf-8") == "# mine\n"

    def test_a_series_already_in_the_batch_is_not_imported_twice(self, tmp_path, exports):
        """Deduplicated by series identity, so /serie/x and /serie/x/staffel-2 are one."""
        exports["sto"].write_text("https://serienstream.to/serie/dup/staffel-2\n", encoding="utf-8")
        batch = tmp_path / "batch.txt"
        batch.write_text("https://serienstream.to/serie/dup\n", encoding="utf-8")
        with _captured() as out, _answers("y", default="y"):
            asyncio.run(wm.import_urls(str(batch)))
        assert batch.read_text(encoding="utf-8").count("serie/dup") == 1
        assert "no new URLs" in out.getvalue()

    def test_a_url_from_the_wrong_family_is_not_imported(self, tmp_path, exports):
        """An aniworld URL sitting in the sto list is a mistake, not an import."""
        exports["sto"].write_text("https://aniworld.to/anime/stream/wrong-family\n", encoding="utf-8")
        batch = tmp_path / "batch.txt"
        batch.write_text("", encoding="utf-8")
        with _captured() as out:
            asyncio.run(wm.import_urls(str(batch)))
        assert "0 series" in out.getvalue()

    def test_missing_scraper_lists_are_named_rather_than_ignored(self, tmp_path, exports):
        batch = tmp_path / "batch.txt"
        batch.write_text("", encoding="utf-8")
        with _captured() as out:
            asyncio.run(wm.import_urls(str(batch)))
        printed = out.getvalue()
        assert "not found" in printed

    def test_comments_in_a_scraper_list_are_skipped(self, tmp_path, exports):
        exports["sto"].write_text(
            "# a note\nhttps://serienstream.to/serie/real  # trailing note\n", encoding="utf-8"
        )
        batch = tmp_path / "batch.txt"
        batch.write_text("", encoding="utf-8")
        with _captured(), _answers("y", default="y"):
            asyncio.run(wm.import_urls(str(batch)))
        written = batch.read_text(encoding="utf-8")
        assert "serie/real" in written and "a note" not in written


# ── printed summaries ───────────────────────────────────────────────────────


class TestPrintedSummaries:
    def test_the_batch_summary_counts_and_lists_hosts(self, batch):
        grouped, rejected = wm.load_url_batches(str(batch))
        with _captured() as out:
            wm.print_batch_summary(grouped, action="watched", rejected=rejected)
        printed = out.getvalue()
        assert "3 series" in printed
        assert "serienstream.to" in printed

    def test_a_long_host_list_is_truncated_with_a_count(self, tmp_path):
        path = tmp_path / "many.txt"
        path.write_text(
            "".join(f"https://serienstream.to/serie/show-{n}\n" for n in range(25)), encoding="utf-8"
        )
        grouped, _ = wm.load_url_batches(str(path))
        with _captured() as out:
            wm.print_batch_summary(grouped, max_urls_per_host=5)
        assert "and 20 more" in out.getvalue()

    def test_rejected_lines_are_reported_with_their_reason(self, tmp_path):
        path = tmp_path / "bad.txt"
        path.write_text("ftp://nope.example/serie/x\nhttps://unknown.example/serie/y\n", encoding="utf-8")
        grouped, rejected = wm.load_url_batches(str(path))
        assert rejected
        with _captured() as out:
            wm.print_batch_summary(grouped, rejected=rejected)
        assert "skipped" in out.getvalue()

    def test_the_menu_shows_the_active_host_and_the_batch_counts(self, batch):
        statuses = {"serienstream.to": "OK (GET 200)", "serienstream.cx": "FAIL (no login form)"}
        with _captured() as out:
            wm.print_menu(str(batch), statuses, has_failed=True, active_host_by_family={"sto": "serienstream.to"})
        printed = out.getvalue()
        assert "ACTIVE" in printed
        assert "failed URLs available" in printed

    def test_the_menu_says_so_when_the_batch_is_empty(self, tmp_path):
        empty = tmp_path / "empty.txt"
        empty.write_text("", encoding="utf-8")
        with _captured() as out:
            wm.print_menu(str(empty), {}, has_failed=False)
        assert "empty" in out.getvalue()

    def test_a_table_with_no_rows_prints_nothing(self):
        with _captured() as out:
            wm._print_table(["A", "B"], [], [10, 10])
        assert out.getvalue() == ""


class TestAskYesNo:
    def test_yes_and_no_are_accepted(self):
        with _captured(), _answers("y"):
            assert wm.ask_yes_no("go?") is True
        with _captured(), _answers("no"):
            assert wm.ask_yes_no("go?", default=True) is False

    def test_enter_takes_the_default_and_the_prompt_says_which(self):
        with _captured(), _answers("", default="") as asked:
            assert wm.ask_yes_no("go?", default=True) is True
        assert "[Y/n]" in asked[0]

    def test_an_unrecognised_answer_is_re_asked(self):
        with _captured(), _answers("maybe", "y") as asked:
            assert wm.ask_yes_no("go?") is True
        assert len(asked) == 2


class TestCredentialValidation:
    def test_a_family_without_credentials_is_named(self, monkeypatch):
        monkeypatch.setattr(wm, "CREDENTIALS", {"sto": {"email": "", "password": ""}})
        assert wm.validate_credentials_for_batch({"serienstream.to": ["x"]}) == ["sto"]

    def test_a_family_with_credentials_is_not_named(self, monkeypatch):
        monkeypatch.setattr(wm, "CREDENTIALS", {"sto": {"email": "a@b.c", "password": "pw"}})
        assert wm.validate_credentials_for_batch({"serienstream.to": ["x"]}) == []
