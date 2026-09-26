"""Shared pytest setup for this repo's tests.

Puts the repo root on ``sys.path`` once, here, so a new test module can import
the code under test without repeating a ``sys.path.insert`` prologue, and
registers the benchmark flags described below.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ── benchmark wiring ────────────────────────────────────────────────────────
# Timing tests live in tests/test_benchmarks.py and are skipped in a normal
# run so the everyday suite stays fast. See tests/bench.py for the harness,
# the tolerance, and how to re-record the baseline.


def pytest_addoption(parser):
    parser.addoption(
        "--benchmark",
        action="store_true",
        default=False,
        help="Run the timing benchmarks in tests/test_benchmarks.py.",
    )
    parser.addoption(
        "--benchmark-update",
        action="store_true",
        default=False,
        help="Run the benchmarks and rewrite tests/benchmark_baseline.json with the new timings.",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "benchmark: a timing test, skipped unless --benchmark is passed")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--benchmark") or config.getoption("--benchmark-update"):
        return
    skip = pytest.mark.skip(reason="timing test; pass --benchmark to run")
    for item in items:
        if "benchmark" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolated_site_state(monkeypatch):
    """Keep every test off state that outlives it.

    The sibling scrapers' real .ignored_seasons.json files would make a test's
    outcome depend on this machine's data, and the per-family POST windows are
    module-level, so POSTs from earlier tests would make a later one sleep out
    a real 61-second window.
    """
    import main

    monkeypatch.setattr(main, "IGNORED_SEASONS_FILES", dict.fromkeys(main.IGNORED_SEASONS_FILES))
    monkeypatch.setattr(main, "_POST_WINDOWS", {})


@pytest.fixture(scope="session")
def bench(request):
    """Session-wide timing recorder; see tests/bench.py for the contract."""
    from tests.bench import Recorder

    recorder = Recorder(update=request.config.getoption("--benchmark-update"))
    yield recorder
    recorder.flush()
