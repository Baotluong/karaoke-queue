"""Screens the player can drive.

LoungeScreen talks to the real YouTube TV app over the Lounge API and keeps
the connection alive. FakeScreen pretends to be a TV for development and tests.
Both call `on_started(video_id)` once a video actually starts playing and
`on_ended(video_id)` when it finishes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

from pyytlounge import EventListener, YtLoungeApi
from pyytlounge.events import DisconnectedEvent, NowPlayingEvent, PlaybackStateEvent
from pyytlounge.models import State

log = logging.getLogger(__name__)

EndedCallback = Callable[[str], Awaitable[None]]


class Screen:
    """Interface the player uses."""

    on_ended: EndedCallback | None = None
    on_started: Callable[[str], None] | None = None
    on_status: Callable[[], None] | None = None  # connection or pause state changed

    def _video_started(self, video_id: str) -> None:
        if self.on_started:
            self.on_started(video_id)

    def _status_changed(self) -> None:
        if self.on_status:
            self.on_status()

    @property
    def ready(self) -> bool:
        raise NotImplementedError

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def play_video(self, video_id: str) -> bool:
        raise NotImplementedError

    async def pause(self) -> bool:
        raise NotImplementedError

    async def resume(self) -> bool:
        raise NotImplementedError

    async def pair(self, code: str) -> bool:
        """Link to a TV using the code from its 'Link with TV code' screen."""
        raise NotImplementedError

    def status(self) -> dict:
        return {"ready": self.ready}


class FakeScreen(Screen):
    """A pretend TV. Songs end after `song_seconds`, or when `finish()` is called."""

    def __init__(self, song_seconds: float | None = None, broken: set[str] | None = None) -> None:
        self.song_seconds = song_seconds
        self.broken = broken or set()  # video IDs that never start (like a blocked video)
        self.paired_code: str | None = None
        self.played: list[str] = []
        self.current: str | None = None
        self.paused = False
        self._timer: asyncio.Task | None = None

    @property
    def ready(self) -> bool:
        return True

    async def play_video(self, video_id: str) -> bool:
        self.played.append(video_id)
        self.current = video_id
        if self._timer:
            self._timer.cancel()
        if video_id in self.broken:
            log.info("[fake tv] %s won't play", video_id)
            return True
        self._video_started(video_id)
        if self.song_seconds is not None:
            self._timer = asyncio.create_task(self._auto_end(video_id))
        log.info("[fake tv] playing %s", video_id)
        return True

    async def pair(self, code: str) -> bool:
        code = code.replace(" ", "")
        if not (code.isdigit() and len(code) == 12):
            return False
        self.paired_code = code
        return True

    async def pause(self) -> bool:
        self.paused = True
        self._status_changed()
        return True

    async def resume(self) -> bool:
        self.paused = False
        self._status_changed()
        return True

    async def _auto_end(self, video_id: str) -> None:
        await asyncio.sleep(self.song_seconds or 0)
        await self.finish(video_id)

    async def finish(self, video_id: str | None = None) -> None:
        vid = video_id or self.current
        if vid and vid == self.current:
            self.current = None
            if self.on_ended:
                await self.on_ended(vid)

    async def stop(self) -> None:
        if self._timer:
            self._timer.cancel()

    def status(self) -> dict:
        return {"ready": True, "name": "Fake TV", "fake": True, "paused": self.paused}


class _LoungeEvents(EventListener):
    def __init__(self, screen: "LoungeScreen") -> None:
        self.screen = screen

    async def now_playing_changed(self, event: NowPlayingEvent) -> None:
        if event.video_id:
            self.screen._tv_video = event.video_id
        await self.screen._on_state(event.state, event.current_time, event.duration)

    async def playback_state_changed(self, event: PlaybackStateEvent) -> None:
        await self.screen._on_state(event.state, event.current_time, event.duration)

    async def disconnected(self, event: DisconnectedEvent) -> None:
        log.warning("TV disconnected us: %s", event.reason)


class LoungeScreen(Screen):
    """Drives the YouTube app on a TV paired with 'Link with TV code'."""

    RETRY_SECONDS = 5

    def __init__(self, auth_file: Path, device_name: str = "Karaoke Queue") -> None:
        self.auth_file = auth_file
        self.device_name = device_name
        self.api: YtLoungeApi | None = None
        self._task: asyncio.Task | None = None
        self._expected: str | None = None  # video we asked the TV to play
        self._tv_video: str | None = None  # video the TV says it is on
        self._seen_playing = False
        self._paused = False
        self._last_error: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.api and self.api.connected())

    def status(self) -> dict:
        return {
            "ready": self.ready,
            "name": self.api.screen_name if self.api else None,
            "paired": self.auth_file.exists(),
            "error": None if self.ready else self._last_error,
            "paused": self._paused,
        }

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def pair(self, code: str) -> bool:
        """Pair with a (new) TV, save the login, and reconnect to it."""
        code = "".join(ch for ch in code if ch.isdigit())
        lib_log = logging.getLogger("pyytlounge")
        async with YtLoungeApi(self.device_name, logger=lib_log) as api:
            try:
                ok = await api.pair(code)
            except Exception:  # bad or expired codes come back as an HTML 404
                log.debug("pairing failed", exc_info=True)
                ok = False
            if not ok:
                return False
            self.auth_file.write_text(json.dumps(api.store_auth_state(), indent=2))
        log.info("Paired with a TV; reconnecting")
        await self.stop()  # drop the old connection and start fresh with the new login
        self.api = None
        await self.start()
        return True

    async def _run(self) -> None:
        """Connect, listen for events, and reconnect forever."""
        lib_log = logging.getLogger("pyytlounge")
        async with YtLoungeApi(self.device_name, _LoungeEvents(self), logger=lib_log) as api:
            self.api = api
            while True:
                try:
                    await self._connect_once(api)
                    if api.connected():
                        self._status_changed()
                        await api.subscribe()  # returns when the connection drops
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # network blips, expired tokens, TV off
                    self._last_error = str(exc) or type(exc).__name__
                    log.warning("TV connection error: %s", self._last_error)
                self._status_changed()
                await asyncio.sleep(self.RETRY_SECONDS)

    async def _connect_once(self, api: YtLoungeApi) -> None:
        if not self.auth_file.exists():
            self._last_error = "Not paired. Pair the TV on the admin page."
            return
        if not api.linked():
            api.load_auth_state(json.loads(self.auth_file.read_text()))
        if not await api.is_available():
            self._last_error = "TV is offline (open the YouTube app on the TV)"
            return
        if not (await api.connect() or (await api.refresh_auth() and await api.connect())):
            self._last_error = "Could not connect to the TV"
            return
        self.auth_file.write_text(json.dumps(api.store_auth_state(), indent=2))
        self._last_error = None
        log.info("Connected to TV '%s'", api.screen_name)
        # Stop the TV from wandering off to a recommended video after each song.
        try:
            await api.set_auto_play_mode(False)
        except Exception:
            log.debug("could not disable autoplay", exc_info=True)

    async def play_video(self, video_id: str) -> bool:
        if not self.ready or not self.api:
            return False
        self._expected = video_id
        self._seen_playing = False
        return await self.api.play_video(video_id)

    async def pause(self) -> bool:
        return bool(self.ready and self.api and await self.api.pause())

    async def resume(self) -> bool:
        return bool(self.ready and self.api and await self.api.play())

    async def _on_state(self, state: State, current: float | None, duration: float | None) -> None:
        if state in (State.Playing, State.Paused) and self._paused != (state == State.Paused):
            self._paused = state == State.Paused
            self._status_changed()
        if not self._expected or self._tv_video != self._expected:
            return
        if state in (State.Playing, State.Advertisement) and not self._seen_playing:
            self._seen_playing = True
            self._video_started(self._expected)
        if state == State.Playing:
            near_end = bool(duration and current is not None and current >= duration - 0.5)
            if not near_end:
                return
        elif state != State.Buffering:  # pyytlounge's "Buffering" (0) is YouTube's "ended"
            return
        if not self._seen_playing:
            return
        ended, self._expected = self._expected, None
        log.info("Video ended: %s", ended)
        if self.on_ended:
            await self.on_ended(ended)
