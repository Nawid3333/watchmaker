"""The requests that change watch state on the sites.

TestMarkSeason drives mark_season with _issue_mark stubbed out, so the request
each site actually receives was never checked. A swapped watched/unwatched
value there does the opposite of what was asked; the verification step only
notices afterwards, when the change has already been made on the account.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from main import ACTION_UNWATCHED, ACTION_WATCHED, ControlMissingError, DomainWorker, SeriesPlan  # noqa: E402

STO_SEASON = """<html><head><meta name="csrf-token" content="tok"></head><body>
<div id="season-mark" data-mark-url="/serie/foo/staffel-1/mark"></div></body></html>"""

ANIWORLD_SEASON = """<html><body><div class="add-series" data-series-id="42"></div>
<span class="clearAllEpisodesFromThisSeason" data-season-id="7"></span></body></html>"""


def _doc(html: str):
    doc = main.make_doc(html)
    assert doc is not None
    return doc


class RecordingWorker(DomainWorker):
    """A DomainWorker that records what it would send instead of sending it."""

    def __init__(self, host: str, response: httpx.Response | None = None, page: str = "<html><body></body></html>"):
        super().__init__(host)
        self.response = response or httpx.Response(200, json={"ok": True, "status": True})
        self.page = page
        self.posts: list[dict] = []
        self.gets: list[str] = []
        self.logged_in = True

    async def _post(self, url, data=None, *, json=None, headers=None):
        self.posts.append({"url": url, "data": data, "json": json, "headers": headers or {}})
        return self.response

    async def _get_soup(self, url):
        self.gets.append(url)
        return _doc(self.page)


class TestStoMarkRequest(unittest.IsolatedAsyncioTestCase):
    async def _mark(self, action, html=STO_SEASON, response=None):
        w = RecordingWorker("serienstream.to", response)
        await w._issue_mark(_doc(html), w.season_url("foo", 1), "foo", 1, action)
        return w

    async def test_watched_and_unwatched_send_opposite_actions(self):
        self.assertEqual((await self._mark(ACTION_WATCHED)).posts[0]["json"], {"action": "seen"})
        self.assertEqual((await self._mark(ACTION_UNWATCHED)).posts[0]["json"], {"action": "unseen"})

    async def test_the_request_goes_to_the_season_control_with_the_csrf_token(self):
        post = (await self._mark(ACTION_WATCHED)).posts[0]
        self.assertEqual(post["url"], "https://serienstream.to/serie/foo/staffel-1/mark")
        self.assertEqual(post["headers"]["X-CSRF-TOKEN"], "tok")

    async def test_a_missing_control_or_token_asks_for_a_re_login(self):
        for html in (STO_SEASON.replace('id="season-mark"', 'id="other"'), STO_SEASON.replace("csrf-token", "x")):
            with self.subTest(html=html[:60]), self.assertRaises(ControlMissingError):
                await self._mark(ACTION_WATCHED, html=html)

    async def test_a_refusal_is_a_failure_not_a_success(self):
        for response in (httpx.Response(200, json={"ok": False}), httpx.Response(500)):
            with self.subTest(status=response.status_code), self.assertRaises(RuntimeError):
                await self._mark(ACTION_WATCHED, response=response)


class TestAniworldMarkRequest(unittest.IsolatedAsyncioTestCase):
    async def _mark(self, action, html=ANIWORLD_SEASON, response=None):
        w = RecordingWorker("aniworld.to", response)
        await w._issue_mark(_doc(html), w.season_url("x", 1), "x", 1, action)
        return w

    async def test_watched_and_unwatched_send_opposite_flags(self):
        self.assertEqual(
            (await self._mark(ACTION_WATCHED)).posts[0]["data"], {"series": "42", "season": "7", "watch": "true"}
        )
        self.assertEqual(
            (await self._mark(ACTION_UNWATCHED)).posts[0]["data"], {"series": "42", "season": "7", "watch": "false"}
        )

    async def test_the_request_goes_to_the_watchseason_endpoint(self):
        self.assertEqual((await self._mark(ACTION_WATCHED)).posts[0]["url"], "https://aniworld.to/ajax/watchseason")

    async def test_missing_ids_ask_for_a_re_login(self):
        without_season = ANIWORLD_SEASON.replace("clearAllEpisodesFromThisSeason", "other")
        without_series = ANIWORLD_SEASON.replace("add-series", "other")
        for html in (without_season, without_series):
            with self.subTest(html=html[:60]), self.assertRaises(ControlMissingError):
                await self._mark(ACTION_WATCHED, html=html)

    async def test_a_refusal_is_a_failure_not_a_success(self):
        with self.assertRaises(RuntimeError):
            await self._mark(ACTION_WATCHED, response=httpx.Response(200, json={"status": False}))


class TestBsMarkRequest(unittest.IsolatedAsyncioTestCase):
    async def test_watched_and_unwatched_open_opposite_links(self):
        for action, verb in ((ACTION_WATCHED, "watch:all"), (ACTION_UNWATCHED, "unwatch:all")):
            with self.subTest(action=action):
                w = RecordingWorker("burningseries.ac")
                await w._issue_mark(_doc("<html></html>"), w.season_url("Foo", 3), "Foo", 3, action)
                self.assertEqual(w.gets, [f"https://burningseries.ac/serie/Foo/3/des/{verb}"])
                self.assertEqual(w.posts, [])


class _NoMarking(RecordingWorker):
    """Records mark_series' decisions without marking any season."""

    def __init__(self, host, *, login_ok=True):
        super().__init__(host)
        self.logged_in = False
        self.login_ok = login_ok
        self.subscribe_calls = 0
        self.seasons_marked: list = []

    async def login(self):
        self.logged_in = self.login_ok
        return self.login_ok

    async def ensure_subscribed(self, url, doc):
        self.subscribe_calls += 1
        return True

    async def mark_season(self, slug, season, action):
        self.seasons_marked.append(season)
        return main.SeasonOutcome(season=season, total=1, watched_before=0, watched_after=1)


def _plan(host, family):
    return SeriesPlan(
        url=f"https://{host}/serie/foo", host=host, family=family, slug="foo", seasons=[1, 2], title="Foo"
    )


class TestMarkSeries(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_login_marks_nothing(self):
        w = _NoMarking("serienstream.to", login_ok=False)
        result = await w.mark_series(_plan("serienstream.to", "sto"), ACTION_WATCHED)
        self.assertFalse(result.ok)
        self.assertEqual(result.note, "login failed")
        self.assertEqual(w.seasons_marked, [])

    async def test_only_marking_watched_subscribes(self):
        watched = _NoMarking("serienstream.to")
        await watched.mark_series(_plan("serienstream.to", "sto"), ACTION_WATCHED)
        unwatched = _NoMarking("serienstream.to")
        await unwatched.mark_series(_plan("serienstream.to", "sto"), ACTION_UNWATCHED)
        self.assertEqual((watched.subscribe_calls, unwatched.subscribe_calls), (1, 0))

    async def test_bs_has_no_subscription_to_touch(self):
        w = _NoMarking("burningseries.ac")
        await w.mark_series(_plan("burningseries.ac", "bs"), ACTION_WATCHED)
        self.assertEqual(w.subscribe_calls, 0)
        self.assertEqual(w.seasons_marked, [1, 2])


if __name__ == "__main__":
    unittest.main()
