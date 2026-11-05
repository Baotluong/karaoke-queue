"""SQLite-backed singers and song queue.

Everything runs on the asyncio event loop thread, so a single connection is
fine. Every change calls the registered listeners so the web layer can push
the new queue to phones.
"""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

SCHEMA = """
CREATE TABLE IF NOT EXISTS singers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY,
    singer_id   INTEGER NOT NULL REFERENCES singers(id),
    video_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT '',
    thumbnail   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL CHECK (status IN ('queued', 'playing', 'done', 'skipped')),
    position    REAL NOT NULL,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_requests_status_pos ON requests(status, position);
"""


@dataclass
class Singer:
    id: int
    name: str
    token: str


@dataclass
class Video:
    video_id: str
    title: str
    channel: str = ""
    thumbnail: str = ""


@dataclass
class SongRequest:
    id: int
    singer_id: int
    singer_name: str
    video_id: str
    title: str
    channel: str
    thumbnail: str
    status: str
    position: float

    def public(self) -> dict:
        return asdict(self)


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._listeners: list[Callable[[], None]] = []
        self._recover_interrupted()

    # --- change notification -------------------------------------------------

    def on_change(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        self.db.commit()
        for fn in self._listeners:
            fn()

    def _recover_interrupted(self) -> None:
        """A song left 'playing' by a crash goes back to the front of the queue."""
        row = self.db.execute("SELECT MIN(position) FROM requests WHERE status='queued'").fetchone()
        front = (row[0] if row[0] is not None else 1.0) - 1
        self.db.execute("UPDATE requests SET status='queued', position=? WHERE status='playing'", (front,))
        self.db.commit()

    # --- singers -------------------------------------------------------------

    def join(self, name: str) -> Singer:
        name = name.strip()[:40]
        if not name:
            raise ValueError("name is required")
        token = secrets.token_urlsafe(24)
        cur = self.db.execute(
            "INSERT INTO singers (name, token, created_at) VALUES (?, ?, ?)",
            (name, token, time.time()),
        )
        self.db.commit()
        return Singer(cur.lastrowid, name, token)

    def singer_by_token(self, token: str | None) -> Singer | None:
        if not token:
            return None
        row = self.db.execute("SELECT id, name, token FROM singers WHERE token=?", (token,)).fetchone()
        return Singer(**row) if row else None

    # --- queue ---------------------------------------------------------------

    _SELECT = """
        SELECT r.id, r.singer_id, s.name AS singer_name, r.video_id, r.title,
               r.channel, r.thumbnail, r.status, r.position
        FROM requests r JOIN singers s ON s.id = r.singer_id
    """

    def add(self, singer: Singer, video: Video) -> SongRequest:
        row = self.db.execute("SELECT MAX(position) FROM requests WHERE status='queued'").fetchone()
        position = (row[0] or 0) + 1
        cur = self.db.execute(
            """INSERT INTO requests
               (singer_id, video_id, title, channel, thumbnail, status, position, created_at)
               VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)""",
            (singer.id, video.video_id, video.title, video.channel, video.thumbnail, position, time.time()),
        )
        self._changed()
        return self.get(cur.lastrowid)  # type: ignore[return-value]

    def get(self, request_id: int) -> SongRequest | None:
        row = self.db.execute(self._SELECT + " WHERE r.id=?", (request_id,)).fetchone()
        return SongRequest(**row) if row else None

    def upcoming(self) -> list[SongRequest]:
        rows = self.db.execute(self._SELECT + " WHERE r.status='queued' ORDER BY r.position, r.id")
        return [SongRequest(**r) for r in rows]

    def now_playing(self) -> SongRequest | None:
        row = self.db.execute(self._SELECT + " WHERE r.status='playing' LIMIT 1").fetchone()
        return SongRequest(**row) if row else None

    def next_up(self) -> SongRequest | None:
        upcoming = self.upcoming()
        return upcoming[0] if upcoming else None

    def mark_playing(self, request_id: int) -> None:
        self.db.execute(
            "UPDATE requests SET status='playing', started_at=? WHERE id=?", (time.time(), request_id)
        )
        self._changed()

    def mark_finished(self, request_id: int, status: str = "done") -> None:
        assert status in ("done", "skipped")
        self.db.execute(
            "UPDATE requests SET status=?, finished_at=? WHERE id=? AND status='playing'",
            (status, time.time(), request_id),
        )
        self._changed()

    def snapshot(self) -> dict:
        now = self.now_playing()
        return {
            "now_playing": now.public() if now else None,
            "upcoming": [r.public() for r in self.upcoming()],
        }
