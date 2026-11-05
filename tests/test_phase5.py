import asyncio
import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.player import Player
from app.search import Searcher
from app.store import Store, Video
from app.tv import FakeScreen
from conftest import scan_qr

BAD = "badvideo000"
GOOD = "goodvideo00"


def vid(video_id: str) -> Video:
    return Video(video_id=video_id, title=f"Title {video_id}")


async def settle(seconds: float = 0.0) -> None:
    await asyncio.sleep(seconds)
    for _ in range(5):
        await asyncio.sleep(0)


# --- stuck-song watchdog ---------------------------------------------------------

async def test_song_that_never_starts_is_skipped_with_a_notice():
    store, tv = Store(), FakeScreen(broken={BAD})
    player = Player(store, tv, start_timeout=0.05)
    notices = []
    player.on_notice = lambda: notices.append(1)
    await player.start()
    try:
        ana, ben = store.join("Ana"), store.join("Ben")
        bad = store.add(ana, vid(BAD))
        store.add(ben, vid(GOOD))
        await settle()
        assert store.now_playing().id == bad.id

        await settle(0.15)
        assert tv.played == [BAD, GOOD]  # moved on by itself
        assert store.get(bad.id).status == "skipped"
        [notice] = player.notices()
        assert notice["singer_id"] == ana.id and "wouldn't play" in notice["text"]
        assert notices == [1]
    finally:
        await player.stop()


async def test_song_that_starts_is_never_skipped():
    store, tv = Store(), FakeScreen()
    player = Player(store, tv, start_timeout=0.05)
    await player.start()
    try:
        ana = store.join("Ana")
        song = store.add(ana, vid(GOOD))
        await settle(0.2)
        assert store.now_playing().id == song.id
        assert player.notices() == []
    finally:
        await player.stop()


class FlakyTV(FakeScreen):
    """A TV that can drop off the network."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.online = True

    @property
    def ready(self) -> bool:
        return self.online


async def test_watchdog_waits_while_the_tv_is_offline():
    store, tv = Store(), FlakyTV(broken={BAD})
    player = Player(store, tv, start_timeout=0.05)
    await player.start()
    try:
        song = store.add(store.join("Ana"), vid(BAD))
        await settle()
        tv.online = False  # e.g. Wi-Fi blip right after we sent the song
        await settle(0.2)
        assert store.now_playing().id == song.id  # not blamed on the song
        tv.online = True
        await settle(0.15)
        assert store.get(song.id).status == "skipped"
    finally:
        await player.stop()


async def test_skipping_by_hand_cancels_the_watchdog():
    store, tv = Store(), FakeScreen(broken={BAD})
    player = Player(store, tv, start_timeout=0.05)
    await player.start()
    try:
        store.add(store.join("Ana"), vid(BAD))
        await settle()
        await player.skip()
        await settle(0.15)
        assert player.notices() == []  # admin skipped it; no "wouldn't play" notice
    finally:
        await player.stop()


# --- pairing and notices over the API ---------------------------------------------

def make_app(monkeypatch, tv):
    async def fake_lookup(self, video_id):
        return vid(video_id)

    monkeypatch.setattr(Searcher, "lookup", fake_lookup)
    return create_app(Settings(db_path=":memory:", admin_pin="123456"), screen=tv)


def test_pair_tv_from_admin_page(monkeypatch):
    tv = FakeScreen()
    with TestClient(make_app(monkeypatch, tv)) as c:
        assert c.post("/api/admin/tv/pair", json={"code": "123 456 789 012"}).status_code == 401
        c.post("/api/admin/login", json={"pin": "123456"})
        r = c.post("/api/admin/tv/pair", json={"code": "123"})
        assert r.status_code == 400 and "expire" in r.json()["detail"]
        assert c.post("/api/admin/tv/pair", json={"code": "123 456 789 012"}).status_code == 200
        assert tv.paired_code == "123456789012"


def test_notices_reach_the_state(monkeypatch):
    tv = FakeScreen(broken={BAD})
    app = make_app(monkeypatch, tv)
    app.state.player.start_timeout = 0.05
    with TestClient(app) as c:
        scan_qr(c)
        c.post("/api/join", json={"name": "Ana"})
        c.post("/api/requests", json={"video_id": BAD})
        for _ in range(100):
            notices = c.get("/api/state").json()["notices"]
            if notices:
                break
            time.sleep(0.02)
        assert notices and "wouldn't play" in notices[0]["text"]
