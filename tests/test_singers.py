import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app
from app.search import Searcher
from app.store import Store, Video
from app.tv import FakeScreen
from conftest import scan_qr

PIN = "123456"


def vid(tag: str) -> Video:
    return Video(video_id=tag.ljust(11, "x"), title=tag)


# --- store ------------------------------------------------------------------

def test_remove_singer_signs_them_out_and_drops_waiting_songs():
    s = Store()
    ana, ben = s.join("Ana"), s.join("Ben")
    a1 = s.add(ana, vid("A1"))
    s.add(ben, vid("B1"))
    s.add(ana, vid("A2"))
    s.mark_playing(a1.id)

    assert s.remove_singer(ana.id)
    assert s.singer_by_token(ana.token) is None  # signed out
    assert [x["name"] for x in s.singers()] == ["Ben"]
    assert [r.title for r in s.upcoming()] == ["B1"]  # A2 dropped
    assert s.now_playing().id == a1.id  # their current song isn't cut off
    assert not s.remove_singer(ana.id)  # already removed

    again = s.join("Ana")  # can rejoin as a new singer
    assert again.id != ana.id and s.singer_by_token(again.token)


def test_inactive_singers_are_flagged(monkeypatch):
    s = Store()
    t = [1_000_000.0]
    monkeypatch.setattr("app.store.time.time", lambda: t[0])
    old, waiting = s.join("Old"), s.join("Waiting")
    r = s.add(old, vid("O1"))
    s.mark_playing(r.id)
    s.mark_finished(r.id)
    s.add(waiting, vid("W1"))

    t[0] += 4 * 3600  # four hours later
    fresh = s.join("Fresh")
    active = {x["name"]: x["active"] for x in s.singers()}
    assert active == {"Old": False, "Waiting": True, "Fresh": True}


def test_new_night_clears_everything_but_settings():
    s = Store()
    s.set_max_waiting(5)
    s.set_fair_turns(False)
    code = s.join_code
    ana = s.join("Ana")
    r = s.add(ana, vid("A1"))
    s.mark_playing(r.id)
    s.add(ana, vid("A2"))

    s.new_night()
    assert s.singers() == [] and s.upcoming() == [] and s.now_playing() is None
    assert s.singer_by_token(ana.token) is None
    assert s.join_code != code
    assert s.max_waiting == 5 and s.fair_turns is False


def test_upgrades_database_without_removed_column(tmp_path):
    db = tmp_path / "phase5.db"
    Store(db).db.close()
    con = sqlite3.connect(db)  # rebuild singers the way Phase 2-5 created it
    con.executescript(
        """
        DROP TABLE singers;
        CREATE TABLE singers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL, blocked INTEGER NOT NULL DEFAULT 0);
        INSERT INTO singers VALUES (1, 'Ana', 'tok', 0, 0);
        """
    )
    con.commit()
    con.close()
    s = Store(db)
    assert [x["name"] for x in s.singers()] == ["Ana"]
    assert s.remove_singer(1)
    assert s.singers() == []


# --- API --------------------------------------------------------------------

@pytest.fixture
def c(monkeypatch):
    async def fake_lookup(self, video_id):
        return Video(video_id=video_id, title=video_id)

    monkeypatch.setattr(Searcher, "lookup", fake_lookup)
    tv = FakeScreen()
    app = create_app(Settings(db_path=":memory:", admin_pin=PIN), screen=tv)
    with TestClient(app) as client:
        client.tv = tv
        yield client


def test_admin_removes_a_singer(c):
    guest = scan_qr(TestClient(c.app))
    guest.post("/api/join", json={"name": "Spammer"})
    c.post("/api/admin/login", json={"pin": PIN})
    sid = c.get("/api/admin/state").json()["singers"][0]["id"]

    assert guest.delete(f"/api/admin/singers/{sid}").status_code == 401  # guests can't
    assert c.delete(f"/api/admin/singers/{sid}").status_code == 200
    assert c.delete(f"/api/admin/singers/{sid}").status_code == 404
    assert c.get("/api/admin/state").json()["singers"] == []
    assert guest.get("/api/state").json()["me"] is None  # signed out
    assert guest.post("/api/requests", json={"video_id": "aaaaaaaaaaa"}).status_code == 401


def test_new_night(c):
    scan_qr(c)
    c.post("/api/join", json={"name": "Ana"})
    c.post("/api/requests", json={"video_id": "aaaaaaaaaaa"})
    c.post("/api/requests", json={"video_id": "bbbbbbbbbbb"})

    admin = TestClient(c.app)
    assert admin.post("/api/admin/new-night").status_code == 401
    admin.post("/api/admin/login", json={"pin": PIN})
    with c.websocket_connect("/ws") as ws:
        ws.receive_json()
        info = admin.post("/api/admin/new-night").json()
        with pytest.raises(WebSocketDisconnect):
            for _ in range(5):
                ws.receive_json()  # guest is kicked: old link is dead
    state = admin.get("/api/admin/state").json()
    assert state["singers"] == [] and state["upcoming"] == [] and state["now_playing"] is None
    assert info["join_url"].endswith(c.app.state.store.join_code)
    assert c.tv.paused  # the TV stopped mid-song
    assert c.get("/api/state").status_code == 403  # must scan the new QR code
