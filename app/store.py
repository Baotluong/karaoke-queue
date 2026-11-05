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

STATUSES = ("queued", "playing", "done", "skipped", "removed")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS singers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    blocked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY,
    singer_id   INTEGER NOT NULL REFERENCES singers(id),
    video_id    TEXT NOT NULL,
    title       TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT '',
    thumbnail   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL CHECK (status IN {STATUSES!r}),
    position    REAL NOT NULL,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS idx_requests_status_pos ON requests(status, position);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_MAX_WAITING = 3


class QueueRuleError(Exception):
    """A request breaks a house rule (limit reached, duplicate song)."""


@dataclass
class Singer:
    id: int
    name: str
    token: str
    blocked: bool = False


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
        self._migrate()
        self.db.execute("PRAGMA foreign_keys = ON")
        self._listeners: list[Callable[[], None]] = []
        self._recover_interrupted()

    # --- schema / migrations --------------------------------------------------

    def _migrate(self) -> None:
        """Create tables, and upgrade databases made by older versions."""
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(singers)")}
        if "blocked" not in cols:  # Phase 1 database
            self.db.execute("ALTER TABLE singers ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        sql = self.db.execute("SELECT sql FROM sqlite_master WHERE name='requests'").fetchone()[0]
        if "'removed'" not in sql:  # Phase 1 CHECK constraint lacks 'removed'; rebuild the table
            self.db.executescript(
                """
                BEGIN;
                ALTER TABLE requests RENAME TO requests_old;
                DROP INDEX IF EXISTS idx_requests_status_pos;
                """
                + SCHEMA
                + """
                INSERT INTO requests SELECT * FROM requests_old;
                DROP TABLE requests_old;
                COMMIT;
                """
            )
        self.db.commit()

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
        row = self.db.execute(
            "SELECT id, name, token, blocked FROM singers WHERE token=?", (token,)
        ).fetchone()
        return Singer(row["id"], row["name"], row["token"], bool(row["blocked"])) if row else None

    def singers(self) -> list[dict]:
        """Everyone who has joined, with how many songs they have waiting and sung."""
        rows = self.db.execute(
            """SELECT s.id, s.name, s.blocked,
                      SUM(r.status = 'queued')  AS queued,
                      SUM(r.status IN ('playing', 'done')) AS sung
               FROM singers s LEFT JOIN requests r ON r.singer_id = s.id
               GROUP BY s.id ORDER BY s.blocked, s.name COLLATE NOCASE"""
        )
        return [
            {"id": r["id"], "name": r["name"], "blocked": bool(r["blocked"]),
             "queued": r["queued"] or 0, "sung": r["sung"] or 0}
            for r in rows
        ]

    def set_blocked(self, singer_id: int, blocked: bool) -> None:
        """Blocking also takes the singer's waiting songs out of the queue."""
        self.db.execute("UPDATE singers SET blocked=? WHERE id=?", (int(blocked), singer_id))
        if blocked:
            self.db.execute(
                "UPDATE requests SET status='removed', finished_at=? WHERE singer_id=? AND status='queued'",
                (time.time(), singer_id),
            )
        self._changed()

    # --- settings ------------------------------------------------------------

    def _get_setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_setting(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._changed()

    @property
    def max_waiting(self) -> int:
        """Most songs one person may have waiting at once. 0 means no limit."""
        value = self._get_setting("max_waiting")
        return int(value) if value is not None else DEFAULT_MAX_WAITING

    def set_max_waiting(self, value: int) -> None:
        if not 0 <= value <= 50:
            raise ValueError("limit must be between 0 and 50")
        self._set_setting("max_waiting", str(value))

    @property
    def fair_turns(self) -> bool:
        """On: new songs go in the singer's turn. Off: new songs go to the bottom."""
        return self._get_setting("fair_turns") != "0"

    def set_fair_turns(self, on: bool) -> None:
        self._set_setting("fair_turns", "1" if on else "0")

    def settings(self) -> dict:
        return {"max_waiting": self.max_waiting, "fair_turns": self.fair_turns, "party_open": self.party_open}

    # --- party access ----------------------------------------------------------

    _CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L

    @property
    def join_code(self) -> str:
        """Secret code in the party's join link / QR code. Created on first use."""
        code = self._get_setting("join_code")
        if code is None:
            code = self.rotate_join_code()
        return code

    def rotate_join_code(self) -> str:
        """New join link; everyone who joined with the old one must scan again."""
        code = "".join(secrets.choice(self._CODE_ALPHABET) for _ in range(8))
        self._set_setting("join_code", code)
        return code

    @property
    def party_open(self) -> bool:
        """Closed: nobody new can join and nobody can add songs (the queue keeps playing)."""
        return self._get_setting("party_open") != "0"

    def set_party_open(self, open_: bool) -> None:
        self._set_setting("party_open", "1" if open_ else "0")

    # --- queue ---------------------------------------------------------------

    _SELECT = """
        SELECT r.id, r.singer_id, s.name AS singer_name, r.video_id, r.title,
               r.channel, r.thumbnail, r.status, r.position
        FROM requests r JOIN singers s ON s.id = r.singer_id
    """

    def add(self, singer: Singer, video: Video) -> SongRequest:
        """Add a song in the singer's fair turn. Raises QueueRuleError if not allowed."""
        upcoming = self.upcoming()
        now = self.now_playing()
        if any(r.video_id == video.video_id for r in upcoming + ([now] if now else [])):
            raise QueueRuleError("That song is already in the queue")
        limit = self.max_waiting
        if limit and sum(r.singer_id == singer.id for r in upcoming) >= limit:
            raise QueueRuleError(
                f"You already have {limit} song{'s' if limit != 1 else ''} waiting. "
                "Add another after one of them plays."
            )

        index = self.fair_index(singer.id, upcoming, now) if self.fair_turns else len(upcoming)
        ids = [r.id for r in upcoming]
        cur = self.db.execute(
            """INSERT INTO requests
               (singer_id, video_id, title, channel, thumbnail, status, position, created_at)
               VALUES (?, ?, ?, ?, ?, 'queued', 0, ?)""",
            (singer.id, video.video_id, video.title, video.channel, video.thumbnail, time.time()),
        )
        ids.insert(index, cur.lastrowid)
        self._renumber(ids)
        self._changed()
        return self.get(cur.lastrowid)  # type: ignore[return-value]

    @staticmethod
    def fair_index(
        singer_id: int, upcoming: list[SongRequest], now: SongRequest | None
    ) -> int:
        """Where a new song from `singer_id` goes so everyone takes turns.

        Each person's 1st waiting song is in round 1, their 2nd in round 2, and
        so on; the song playing right now counts as its singer's round 1, so
        nobody sings twice in a row while others are waiting. The new song goes
        after the last song in its round or an earlier one. Because this reads
        the queue as it actually is, an admin's manual reordering is kept.
        """
        counts: dict[int, int] = {}
        if now:
            counts[now.singer_id] = 1
        my_round = counts.get(singer_id, 0) + sum(r.singer_id == singer_id for r in upcoming) + 1
        index = 0
        for i, r in enumerate(upcoming):
            counts[r.singer_id] = counts.get(r.singer_id, 0) + 1
            if counts[r.singer_id] <= my_round:
                index = i + 1
        return index

    def _renumber(self, ids: list[int]) -> None:
        self.db.executemany(
            "UPDATE requests SET position=? WHERE id=?", [(i + 1, rid) for i, rid in enumerate(ids)]
        )

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

    def move(self, request_id: int, index: int) -> bool:
        """Move a waiting song to `index` (0 = next up). Returns False if not queued."""
        ids = [r.id for r in self.upcoming()]
        if request_id not in ids:
            return False
        ids.remove(request_id)
        ids.insert(max(0, min(index, len(ids))), request_id)
        self._renumber(ids)
        self._changed()
        return True

    def remove(self, request_id: int, singer_id: int | None = None) -> bool:
        """Take a waiting song out. With `singer_id`, only if it's that singer's song."""
        sql = "UPDATE requests SET status='removed', finished_at=? WHERE id=? AND status='queued'"
        args: tuple = (time.time(), request_id)
        if singer_id is not None:
            sql += " AND singer_id=?"
            args += (singer_id,)
        cur = self.db.execute(sql, args)
        self._changed()
        return cur.rowcount > 0

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
