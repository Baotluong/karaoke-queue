"""Keeps the TV playing whatever is at the front of the queue."""

from __future__ import annotations

import asyncio
import logging

from .store import Store
from .tv import Screen

log = logging.getLogger(__name__)


class Player:
    def __init__(self, store: Store, screen: Screen) -> None:
        self.store = store
        self.screen = screen
        self.screen.on_ended = self._on_ended
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._sent: int | None = None  # request id last sent to the TV
        store.on_change(self._wake.set)

    async def start(self) -> None:
        await self.screen.start()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.screen.stop()

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
                await self._send(current.id, current.video_id)
            return
        nxt = self.store.next_up()
        if nxt:
            self.store.mark_playing(nxt.id)
            await self._send(nxt.id, nxt.video_id)

    async def _send(self, request_id: int, video_id: str) -> None:
        self._sent = request_id
        if not await self.screen.play_video(video_id):
            log.warning("TV refused play_video(%s); will retry", video_id)
            self._sent = None

    async def _on_ended(self, video_id: str) -> None:
        current = self.store.now_playing()
        if current and current.video_id == video_id:
            self.store.mark_finished(current.id, "done")

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
