"""Phase 0 spike: do ads play when only our script is linked to a Premium TV?

Usage:
    python spike/ad_check.py pair            # once; asks for the TV code
    python spike/ad_check.py run             # plays test videos, reports ads
    python spike/ad_check.py run VIDEO_ID... # use your own video IDs

The TV must be signed into the Premium account in the YouTube app.
Pairing state is saved to .lounge_auth.json (git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

import certifi

# Homebrew/python.org Python on macOS may not find the system CA certificates,
# which makes HTTPS to youtube.com fail with CERTIFICATE_VERIFY_FAILED.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from pyytlounge import EventListener, YtLoungeApi
from pyytlounge.events import (
    AdPlayingEvent,
    AdStateEvent,
    DisconnectedEvent,
    NowPlayingEvent,
    PlaybackStateEvent,
)
from pyytlounge.models import State

AUTH_FILE = Path(__file__).resolve().parent.parent / ".lounge_auth.json"
DEVICE_NAME = "Karaoke Queue"

# Popular, monetized videos: the most likely to get a pre-roll ad if ads are on.
DEFAULT_VIDEOS = ["dQw4w9WgXcQ", "kJQP7kiw5Fk"]

log = logging.getLogger("ad_check")
LIB_LOG = logging.getLogger("pyytlounge")  # respects -v instead of always DEBUG


def ts() -> str:
    return time.strftime("%H:%M:%S")


class AdWatcher(EventListener):
    """Records what the TV reports: what's playing and whether an ad is on."""

    def __init__(self) -> None:
        self.ads: list[str] = []
        self.current_video: str | None = None
        self.state: State = State.Stopped
        self.video_started = asyncio.Event()
        self.disconnected = asyncio.Event()

    def _note_ad(self, detail: str) -> None:
        msg = f"{ts()}  AD on video {self.current_video}: {detail}"
        print(msg)
        self.ads.append(msg)

    async def now_playing_changed(self, event: NowPlayingEvent) -> None:
        self.current_video = event.video_id or self.current_video
        self._set_state(event.state)

    async def playback_state_changed(self, event: PlaybackStateEvent) -> None:
        self._set_state(event.state)

    def _set_state(self, state: State) -> None:
        if state != self.state:
            print(f"{ts()}  state: {state.name} (video {self.current_video})")
        self.state = state
        if state == State.Advertisement:
            self._note_ad("playback state = Advertisement")
        if state == State.Playing:
            self.video_started.set()

    async def ad_playing_changed(self, event: AdPlayingEvent) -> None:
        self._note_ad(
            f"'{event.ad_title}' ({event.duration:.0f}s, skippable={event.is_skippable})"
        )

    async def ad_state_changed(self, event: AdStateEvent) -> None:
        print(f"{ts()}  ad state: {event.ad_state.name}")

    async def disconnected(self, event: DisconnectedEvent) -> None:
        print(f"{ts()}  TV disconnected us: {event.reason}")
        self.disconnected.set()


def save_auth(api: YtLoungeApi) -> None:
    AUTH_FILE.write_text(json.dumps(api.store_auth_state(), indent=2))


async def cmd_pair() -> int:
    print("On the TV: YouTube app > Settings > Link with TV code.")
    code = input("Enter the TV code (digits only, spaces OK): ").replace(" ", "").strip()
    async with YtLoungeApi(DEVICE_NAME, logger=LIB_LOG) as api:
        try:
            ok = await api.pair(code)
        except Exception as exc:  # bad/expired codes come back as a 404 HTML page
            log.debug("pair error", exc_info=exc)
            ok = False
        if not ok:
            print("Pairing failed. Check the code (it expires after a few minutes).")
            return 1
        save_auth(api)
        print(f"Paired with '{api.screen_name}'. Saved to {AUTH_FILE.name}.")
    return 0


async def ensure_connected(api: YtLoungeApi) -> bool:
    if not api.linked() and not await api.refresh_auth():
        return False
    if await api.connect():
        return True
    # Lounge token may have expired; refresh once and retry.
    return await api.refresh_auth() and await api.connect()


async def cmd_run(videos: list[str], seconds: int) -> int:
    if not AUTH_FILE.exists():
        print("Not paired yet. Run: python spike/ad_check.py pair")
        return 1

    watcher = AdWatcher()
    async with YtLoungeApi(DEVICE_NAME, watcher, logger=LIB_LOG) as api:
        api.load_auth_state(json.loads(AUTH_FILE.read_text()))

        if not await api.is_available():
            print("TV is not online. Open the YouTube app on the TV and try again.")
            return 1
        if not await ensure_connected(api):
            print("Could not connect. Try pairing again.")
            return 1
        save_auth(api)  # tokens may have been refreshed
        print(f"{ts()}  connected to '{api.screen_name}'")

        listener = asyncio.create_task(api.subscribe())
        try:
            for vid in videos:
                print(f"\n{ts()}  >>> playing {vid} for {seconds}s")
                watcher.video_started.clear()
                await api.play_video(vid)
                try:
                    await asyncio.wait_for(watcher.video_started.wait(), timeout=90)
                except asyncio.TimeoutError:
                    print(f"{ts()}  video never reached Playing within 90s")
                await asyncio.sleep(seconds)
                if watcher.disconnected.is_set():
                    break
            await api.pause()
        finally:
            listener.cancel()
            await api.disconnect()

    print("\n=== Result ===")
    if watcher.ads:
        print(f"ADS SEEN: {len(watcher.ads)} ad event(s)")
        for line in watcher.ads:
            print("  " + line)
        print("The Lounge approach does NOT keep Premium ad-free here.")
    else:
        print("No ad events reported. Also confirm by eye that the TV showed no ads.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pair", help="pair with the TV using its 'Link with TV code'")
    run = sub.add_parser("run", help="play test videos and report ads")
    run.add_argument("videos", nargs="*", default=DEFAULT_VIDEOS, help="YouTube video IDs")
    run.add_argument("--seconds", type=int, default=40, help="how long to play each video")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    if not args.verbose:
        LIB_LOG.setLevel(logging.CRITICAL)  # we print our own friendly errors
    if args.cmd == "pair":
        return asyncio.run(cmd_pair())
    return asyncio.run(cmd_run(args.videos, args.seconds))


if __name__ == "__main__":
    sys.exit(main())
