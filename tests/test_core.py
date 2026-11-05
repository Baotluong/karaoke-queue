import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.player import Player
from app.search import Searcher, extract_video_id
from app.store import Store, Video
from app.tv import FakeScreen


def vid(n: int) -> Video:
    return Video(video_id=f"video{n:06d}", title=f"Song {n}")


# --- search -----------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ&t=30s&list=abc",
        "https://youtu.be/dQw4w9WgXcQ?si=xyz",
        "m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "  https://www.youtube.com/embed/dQw4w9WgXcQ  ",
    ],
)
def test_extract_video_id(text):
    assert extract_video_id(text) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("text", ["bohemian rhapsody", "https://example.com/watch?v=dQw4w9WgXcQ", "short"])
def test_extract_video_id_rejects(text):
    assert extract_video_id(text) is None


# --- store ------------------------------------------------------------------

def test_queue_is_fifo_and_tracks_status():
    s = Store()
    a, b = s.join("Ana"), s.join("Ben")
    r1, r2, r3 = s.add(a, vid(1)), s.add(b, vid(2)), s.add(a, vid(3))
    assert [r.id for r in s.upcoming()] == [r1.id, r2.id, r3.id]
    assert s.upcoming()[1].singer_name == "Ben"

    s.mark_playing(r1.id)
    assert s.now_playing().id == r1.id
    assert s.next_up().id == r2.id
    s.mark_finished(r1.id)
    assert s.now_playing() is None


def test_join_validates_and_tokens_identify_singers():
    s = Store()
    with pytest.raises(ValueError):
        s.join("   ")
    ana = s.join("  Ana  ")
    assert ana.name == "Ana"
    assert s.singer_by_token(ana.token).id == ana.id
    assert s.singer_by_token("nope") is None
    assert s.singer_by_token(None) is None


def test_song_playing_at_crash_goes_back_to_front(tmp_path):
    db = tmp_path / "k.db"
    s = Store(db)
    a = s.join("Ana")
    r1, r2 = s.add(a, vid(1)), s.add(a, vid(2))
    s.mark_playing(r1.id)
    s.db.close()

    s2 = Store(db)
    assert s2.now_playing() is None
    assert [r.id for r in s2.upcoming()] == [r1.id, r2.id]


# --- player -----------------------------------------------------------------

async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_player_plays_queue_in_order_and_skips():
    store, tv = Store(), FakeScreen()
    player = Player(store, tv)
    await player.start()
    try:
        a = store.join("Ana")
        store.add(a, vid(1))
        store.add(a, vid(2))
        store.add(a, vid(3))
        await settle()
        assert tv.played == ["video000001"]

        await tv.finish()  # song ends on the TV
        await settle()
        assert tv.played == ["video000001", "video000002"]

        await player.skip()
        await settle()
        assert tv.played[-1] == "video000003"

        await tv.finish()
        await settle()
        assert store.now_playing() is None and store.upcoming() == []
    finally:
        await player.stop()


async def test_player_ignores_end_of_a_different_video():
    store, tv = Store(), FakeScreen()
    player = Player(store, tv)
    await player.start()
    try:
        a = store.join("Ana")
        store.add(a, vid(1))
        store.add(a, vid(2))
        await settle()
        await player._on_ended("somethingElse")
        await settle()
        assert store.now_playing().video_id == "video000001"
    finally:
        await player.stop()


# --- HTTP API ---------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    async def fake_lookup(self, video_id):
        return Video(video_id=video_id, title=f"Title of {video_id}", channel="Chan")

    monkeypatch.setattr(Searcher, "lookup", fake_lookup)
    tv = FakeScreen()
    app = create_app(Settings(db_path=":memory:", yt_api_key=None), screen=tv)
    with TestClient(app) as c:
        c.tv = tv
        yield c


def test_api_join_request_and_live_updates(client):
    assert client.get("/").status_code == 200
    state = client.get("/api/state").json()
    assert state["me"] is None and state["can_search"] is False and state["tv"]["ready"]

    # Must join before requesting.
    assert client.post("/api/requests", json={"video_id": "dQw4w9WgXcQ"}).status_code == 401

    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["upcoming"] == []
        assert client.post("/api/join", json={"name": "Ana"}).json()["name"] == "Ana"
        r = client.post("/api/requests", json={"video_id": "https://youtu.be/dQw4w9WgXcQ"})
        assert r.status_code == 200
        assert r.json()["title"] == "Title of dQw4w9WgXcQ"  # server-side lookup, not client title

        # Pushed updates: added, then started playing on the TV.
        for _ in range(5):
            msg = ws.receive_json()
            if msg["now_playing"]:
                break
        assert msg["now_playing"]["singer_name"] == "Ana"
    assert client.tv.played == ["dQw4w9WgXcQ"]
    assert client.get("/api/state").json()["me"]["name"] == "Ana"


def test_api_rejects_non_youtube_and_search_without_key(client):
    client.post("/api/join", json={"name": "Ben"})
    assert client.post("/api/requests", json={"video_id": "not a video"}).status_code == 400
    r = client.get("/api/search", params={"q": "bohemian rhapsody"})
    assert r.status_code == 400 and "Paste a YouTube link" in r.json()["detail"]
    r = client.get("/api/search", params={"q": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.json()["results"][0]["video_id"] == "dQw4w9WgXcQ"
