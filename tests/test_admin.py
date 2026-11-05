import contextlib
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app import admin as admin_mod
from app.config import Settings
from app.main import create_app
from app.search import Searcher
from app.store import Store, Video
from app.tv import FakeScreen

PIN = "4321"


def vid(n: int) -> Video:
    return Video(video_id=f"video{n:06d}", title=f"Song {n}")


# --- store ------------------------------------------------------------------

def test_move_and_remove():
    s = Store()
    a = s.join("Ana")
    r1, r2, r3 = (s.add(a, vid(i)) for i in (1, 2, 3))
    assert s.move(r3.id, 0)
    assert [r.id for r in s.upcoming()] == [r3.id, r1.id, r2.id]
    assert s.move(r3.id, 99)  # clamps to the end
    assert [r.id for r in s.upcoming()] == [r1.id, r2.id, r3.id]
    assert s.remove(r2.id)
    assert [r.id for r in s.upcoming()] == [r1.id, r3.id]
    assert not s.remove(r2.id)  # already gone
    assert not s.move(r2.id, 0)


def test_block_removes_waiting_songs_and_unblock_restores_adding():
    s = Store()
    a, b = s.join("Ana"), s.join("Ben")
    s.add(a, vid(1))
    s.add(b, vid(2))
    s.add(b, vid(3))
    s.set_blocked(b.id, True)
    assert [r.singer_name for r in s.upcoming()] == ["Ana"]
    assert s.singer_by_token(b.token).blocked
    singers = {x["name"]: x for x in s.singers()}
    assert singers["Ben"]["blocked"] and singers["Ben"]["queued"] == 0
    assert singers["Ana"]["queued"] == 1
    s.set_blocked(b.id, False)
    assert not s.singer_by_token(b.token).blocked


def test_upgrades_phase1_database(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE singers (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE, created_at REAL NOT NULL);
        CREATE TABLE requests (id INTEGER PRIMARY KEY, singer_id INTEGER NOT NULL REFERENCES singers(id),
            video_id TEXT NOT NULL, title TEXT NOT NULL, channel TEXT NOT NULL DEFAULT '',
            thumbnail TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN ('queued', 'playing', 'done', 'skipped')),
            position REAL NOT NULL, created_at REAL NOT NULL, started_at REAL, finished_at REAL);
        CREATE INDEX idx_requests_status_pos ON requests(status, position);
        INSERT INTO singers VALUES (1, 'Ana', 'tok', 0);
        INSERT INTO requests (id, singer_id, video_id, title, status, position, created_at)
            VALUES (1, 1, 'aaaaaaaaaaa', 'Old song', 'done', 1, 0),
                   (2, 1, 'bbbbbbbbbbb', 'Waiting song', 'queued', 2, 0);
        """
    )
    con.commit()
    con.close()

    s = Store(db)
    assert [r.title for r in s.upcoming()] == ["Waiting song"]
    assert s.remove(2)  # 'removed' status is now allowed
    assert s.singers()[0] == {"id": 1, "name": "Ana", "blocked": False, "queued": 0, "sung": 1}
    Store(db)  # opening again is a no-op


# --- API --------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    async def fake_lookup(self, video_id):
        return Video(video_id=video_id, title=f"Title of {video_id}")

    monkeypatch.setattr(Searcher, "lookup", fake_lookup)
    tv = FakeScreen()
    app = create_app(Settings(db_path=":memory:", admin_pin=PIN), screen=tv)
    with TestClient(app) as c:
        c.tv, c.store = tv, app.state.store
        yield c


def wait_for(cond, timeout=2.0):
    """The player runs in the background; give it a moment to act."""
    end = time.time() + timeout
    while not cond():
        assert time.time() < end, "timed out"
        time.sleep(0.01)


class Guest:
    """Another phone. Shares the app's event loop (one TestClient) but has its own cookie."""

    def __init__(self, client, name):
        self.client = client
        self.token = None
        self.token = self.post("/api/join", json={"name": name}).cookies["kq_token"]

    @contextlib.contextmanager
    def _as_me(self):
        saved = dict(self.client.cookies)
        self.client.cookies.clear()
        if self.token:
            self.client.cookies.set("kq_token", self.token)
        try:
            yield
        finally:
            self.client.cookies.clear()
            self.client.cookies.update(saved)

    def post(self, path, **kw):
        with self._as_me():
            return self.client.post(path, **kw)


guest = Guest


def test_admin_endpoints_require_login(client):
    for method, path in [
        ("get", "/api/admin/state"),
        ("post", "/api/admin/skip"),
        ("post", "/api/admin/pause"),
        ("delete", "/api/admin/requests/1"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path
    client.cookies.set("kq_admin", "forged")
    assert client.get("/api/admin/state").status_code == 401


def test_wrong_pin_locks_out_after_five_tries(client, monkeypatch):
    for _ in range(admin_mod.MAX_FAILURES):
        assert client.post("/api/admin/login", json={"pin": "0000"}).status_code == 401
    r = client.post("/api/admin/login", json={"pin": PIN})
    assert r.status_code == 429  # even the right PIN waits out the lockout

    now = admin_mod.time.time()
    monkeypatch.setattr(admin_mod.time, "time", lambda: now + admin_mod.LOCKOUT_SECONDS + 1)
    assert client.post("/api/admin/login", json={"pin": PIN}).status_code == 200


def test_admin_controls_queue(client):
    ana, ben = guest(client, "Ana"), guest(client, "Ben")
    ids = []
    for g, v in [(ana, "aaaaaaaaaaa"), (ben, "bbbbbbbbbbb"), (ana, "ccccccccccc"), (ben, "ddddddddddd")]:
        ids.append(g.post("/api/requests", json={"video_id": v}).json()["id"])
    # First song went straight to the TV.
    wait_for(lambda: client.tv.played == ["aaaaaaaaaaa"])

    assert client.post("/api/admin/login", json={"pin": PIN}).status_code == 200
    state = client.get("/api/admin/state").json()
    assert [u["id"] for u in state["upcoming"]] == ids[1:]
    assert {s["name"] for s in state["singers"]} == {"Ana", "Ben"}

    # Reorder, remove.
    assert client.post(f"/api/admin/requests/{ids[3]}/move", json={"index": 0}).status_code == 200
    assert client.delete(f"/api/admin/requests/{ids[2]}").status_code == 200
    assert client.delete(f"/api/admin/requests/{ids[2]}").status_code == 404
    assert [u["id"] for u in client.get("/api/admin/state").json()["upcoming"]] == [ids[3], ids[1]]

    # Play now cuts off the current song.
    assert client.post(f"/api/admin/requests/{ids[1]}/play-now").status_code == 200
    wait_for(lambda: client.tv.played[-1] == "bbbbbbbbbbb")
    assert client.store.now_playing().id == ids[1]

    # Skip moves on to the next one.
    assert client.post("/api/admin/skip").status_code == 200
    wait_for(lambda: client.tv.played[-1] == "ddddddddddd")

    # Pause / resume reach the TV and show up in the state.
    client.post("/api/admin/pause")
    assert client.get("/api/state").json()["tv"]["paused"] is True
    client.post("/api/admin/resume")
    assert client.get("/api/state").json()["tv"]["paused"] is False


def test_blocked_singer_cannot_add(client):
    ben = guest(client, "Ben")
    ben.post("/api/requests", json={"video_id": "aaaaaaaaaaa"})
    ben.post("/api/requests", json={"video_id": "bbbbbbbbbbb"})
    client.post("/api/admin/login", json={"pin": PIN})
    ben_id = client.get("/api/admin/state").json()["singers"][0]["id"]

    client.post(f"/api/admin/singers/{ben_id}/block", json={"blocked": True})
    assert client.get("/api/state").json()["upcoming"] == []  # waiting song removed
    r = ben.post("/api/requests", json={"video_id": "ccccccccccc"})
    assert r.status_code == 403

    client.post(f"/api/admin/singers/{ben_id}/block", json={"blocked": False})
    assert ben.post("/api/requests", json={"video_id": "ccccccccccc"}).status_code == 200


def test_logout_and_admin_page(client):
    assert client.get("/admin").status_code == 200
    client.post("/api/admin/login", json={"pin": PIN})
    assert client.get("/api/admin/state").status_code == 200
    client.post("/api/admin/logout")
    assert client.get("/api/admin/state").status_code == 401
