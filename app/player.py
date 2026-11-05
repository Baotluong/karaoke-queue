"""Keeps the TV playing whatever is at the front of the queue."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from typing import Callable

from .store import SongRequest, Store
from .tv import Screen

log = logging.getLogger(__name__)

START_TIMEOUT = 30.0  # seconds for a video to start before we give up on it
NOTICE_SECONDS = 10 * 60  # how long notices stay visible


class Player:
    def __init__(self, store: Store, screen: Screen, start_timeout: float = START_TIMEOUT) -> None:
        self.store = store
        self.screen = screen
        self.start_timeout = start_timeout
        self.screen.on_ended = self._on_ended
        self.screen.on_started = self._on_started
        self.on_notice: Callable[[], None] | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._sent: int | None = None  # request id last sent to the TV
        self._started: int | None = None  # request id the TV confirmed is playing
        self._notices: deque[dict] = deque(maxlen=10)
        self._notice_ids = itertools.count(1)
        store.on_change(self._wake.set)

    async def start(self) -> None:
        await self.screen.start()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for task in (self._task, self._watchdog):
            if task:
                task.cancel()
        await self.screen.stop()

    # --- notices (e.g. "a song wouldn't play") --------------------------------

    def notices(self) -> list[dict]:
        cutoff = time.time() - NOTICE_SECONDS
        return [n for n in self._notices if n["time"] >= cutoff]

    def _notify(self, text: str, singer_id: int | None = None) -> None:
        self._notices.append({"id": next(self._notice_ids), "time": time.time(), "text": text, "singer_id": singer_id})
        if self.on_notice:
            self.on_notice()

    # --- main loop -------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self._advance()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("player loop error")
            self._wake.clear()
            try:
                # Re-check periodically too, e.g. when the TV comes back online.
                await asyncio.wait_for(self._wake.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def _advance(self) -> None:
        """If nothing is playing, start the next song."""
        if not self.screen.ready:
            return
        current = self.store.now_playing()
        if current:
            if self._sent != current.id:
                # Recovered after a restart or reconnect: (re)send it to the TV.
                await self._send(current)
            return
        nxt = self.store.next_up()
        if nxt:
            self.store.mark_playing(nxt.id)
            await self._send(nxt)

    async def _send(self, song: SongRequest) -> None:
        self._sent = song.id
        self._started = None
        if not await self.screen.play_video(song.video_id):
            log.warning("TV refused play_video(%s); will retry", song.video_id)
            self._sent = None
            return
        if self._started != song.id:  # the fake TV confirms instantly
            if self._watchdog:
                self._watchdog.cancel()
            self._watchdog = asyncio.create_task(self._watch(song))

    async def _watch(self, song: SongRequest) -> None:
        """Skip a song that never starts (removed, region-blocked, age-restricted...)."""
        while True:
            await asyncio.sleep(self.start_timeout)
            current = self.store.now_playing()
            if not current or current.id != song.id or self._started == song.id:
                return
            if not self.screen.ready:
                continue  # TV dropped off; give it another window once it's back
            log.warning("Video %s never started; skipping", song.video_id)
            self.store.mark_finished(song.id, "skipped")
            self._notify(
                f'Skipped "{song.title}" ({song.singer_name}): it wouldn\'t play on the TV. '
                "Try a different version of the song.",
                singer_id=song.singer_id,
            )
            return

    def _on_started(self, video_id: str) -> None:
        current = self.store.now_playing()
        if current and current.video_id == video_id and current.id == self._sent:
            self._started = current.id
            if self._watchdog:
                self._watchdog.cancel()
                self._watchdog = None

    async def _on_ended(self, video_id: str) -> None:
        current = self.store.now_playing()
        if current and current.video_id == video_id:
            self.store.mark_finished(current.id, "done")

    # --- admin controls ----------------------------------------------------------

    async def skip(self) -> None:
        current = self.store.now_playing()
        if current:
            self.store.mark_finished(current.id, "skipped")

    async def play_now(self, request_id: int) -> bool:
        """Jump a waiting song to the front and cut off whatever is playing."""
        if not self.store.move(request_id, 0):
            return False
        await self.skip()
        return True

    async def pause(self) -> bool:
        return await self.screen.pause()

    async def resume(self) -> bool:
        return await self.screen.resume()
