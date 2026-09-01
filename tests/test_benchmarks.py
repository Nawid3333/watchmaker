"""Timing benchmarks for watchmaker's batch and page-parsing paths.

Skipped unless ``--benchmark`` is passed. See ``tests/bench.py`` for the
harness, the tolerance, and how to re-record the baseline.

What belongs here
-----------------
Work that scales with the size of the batch file or of a page: parsing and
grouping the batch, rewriting it, and the per-page parsers a run calls once
per season. Everything else in watchmaker is dominated by network waits,
which are not this harness's business.

Every benchmark builds its input once, outside the timed callable.
"""

from __future__ import annotations

import pytest

import main as wm

SEASON_ROWS = 24


def _batch_lines(count: int) -> list[str]:
    """A realistic batch: three families, comments, and a keep section."""
    hosts = ["serienstream.to", "aniworld.to", "burningseries.ac"]
    lines = ["# my working list"]
    for n in range(count):
        host = hosts[n % 3]
        path = "/anime/stream/" if host.startswith("aniworld") else "/serie/"
        lines.append(f"https://{host}{path}show-{n:05d}")
    lines.append(wm.KEEP_MARKER)
    lines.append("https://serienstream.to/serie/pinned-forever")
    return lines


def _series_page(seasons: int = 12) -> str:
    """A page carrying the season navigation the discovery step reads."""
    links = "".join(f'<li><a href="/serie/demo/staffel-{n}">Staffel {n}</a></li>' for n in range(1, seasons + 1))
    pills = "".join(f'<a data-season-pill="{n}" href="/serie/demo/staffel-{n}">{n}</a>' for n in range(1, seasons + 1))
    return (
        "<html><head><title>Demo</title></head><body>"
        f'<h1 class="fw-bold">Demo Series</h1>'
        f'<div id="stream"><ul>{links}</ul></div>'
        f'<div id="season-nav">{pills}</div>'
        "</body></html>"
    )


def _season_page(rows: int = SEASON_ROWS, watched: int = 12) -> str:
    """A season page with an episode table, half of it marked seen."""
    body = "".join(
        f'<tr class="episode-row{" seen" if n <= watched else ""}" data-episode-id="{n}">'
        f"<td>{n}</td><td>Episode {n}</td></tr>"
        for n in range(1, rows + 1)
    )
    return f'<html><body><div class="episode-table"><table><tbody>{body}</tbody></table></div></body></html>'


@pytest.mark.benchmark
def test_loading_a_large_batch_file(bench, tmp_path):
    """Parsed on startup and again after every menu action that changes it."""
    path = tmp_path / "series_urls.txt"
    path.write_text("\n".join(_batch_lines(2000)) + "\n", encoding="utf-8")
    bench("load_url_batches/2000_urls", lambda: wm.load_url_batches(str(path)))


@pytest.mark.benchmark
def test_classifying_batch_lines(bench):
    """Walks every line to decide what option 7 may clear."""
    lines = _batch_lines(2000)
    bench("classify_batch_lines/2000_urls", lambda: wm._classify_batch_lines(lines))


@pytest.mark.benchmark
def test_rewriting_a_batch_file_after_migration(bench, tmp_path):
    """Runs when a family's mirror changes; touches every line of the file."""
    path = tmp_path / "series_urls.txt"
    path.write_text("\n".join(_batch_lines(2000)) + "\n", encoding="utf-8")
    mapping = {
        f"https://serienstream.to/serie/show-{n:05d}": f"https://serienstream.cx/serie/show-{n:05d}"
        for n in range(0, 2000, 3)
    }
    bench("rewrite_batch_urls/2000_urls", lambda: wm._rewrite_batch_urls(str(path), mapping), repeats=3)


@pytest.mark.benchmark
def test_classifying_a_url(bench):
    """Called for every line of the batch, several times per run."""
    url = "https://serienstream.to/serie/some-show/staffel-3"
    bench("classify_url/single", lambda: [wm.classify_url(url) for _ in range(1000)])


@pytest.mark.benchmark
def test_parsing_a_season_page(bench):
    """One per season per series -- the parse a marking run repeats most."""
    html = _season_page()
    soup = wm._soup(html)
    worker = wm.DomainWorker.__new__(wm.DomainWorker)
    worker.family = "sto"
    assert worker._count_episodes(soup) == (12, SEASON_ROWS), "fixture must parse before it is timed"
    bench("count_episodes/24_rows", lambda: worker._count_episodes(wm._soup(html)))


@pytest.mark.benchmark
def test_discovering_seasons_on_a_series_page(bench):
    html = _series_page()
    worker = wm.DomainWorker.__new__(wm.DomainWorker)
    worker.family = "sto"
    assert len(worker.discover_seasons(wm._soup(html), "demo")) == 12
    bench("discover_seasons/12_seasons", lambda: worker.discover_seasons(wm._soup(html), "demo"))


@pytest.mark.benchmark
def test_soup_build(bench):
    """The parser choice underneath every page read; lxml vs stdlib shows here."""
    html = _season_page(rows=200)
    bench("soup/200_row_season_page", lambda: wm._soup(html))
