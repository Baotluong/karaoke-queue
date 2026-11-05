"""Find songs: YouTube search (needs YT_API_KEY) or a pasted YouTube link (no key)."""

from __future__ import annotations

import html
import re
import time
from urllib.parse import parse_qs, urlparse

import aiohttp

from .store import Video

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
OEMBED_URL = "https://www.youtube.com/oembed"


class SearchError(Exception):
    pass


def extract_video_id(text: str) -> str | None:
    """Return the 11-char video ID from a YouTube URL or bare ID, else None."""
    text = text.strip()
    if _ID_RE.match(text):
        return text
    if not re.match(r"^https?://", text):
        text = "https://" + text
    url = urlparse(text)
    host = (url.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    candidate = None
    if host == "youtu.be":
        candidate = url.path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com"):
        if url.path == "/watch":
            candidate = parse_qs(url.query).get("v", [None])[0]
        else:
            parts = url.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
                candidate = parts[1]
    return candidate if candidate and _ID_RE.match(candidate) else None


CACHE_SECONDS = 6 * 60 * 60
CACHE_MAX = 500


class Searcher:
    def __init__(self, api_key: str | None, session: aiohttp.ClientSession) -> None:
        self.api_key = api_key
        self.session = session
        # Each YouTube search costs ~1% of the free daily quota, and party guests
        # search for the same songs a lot, so remember recent results.
        self._cache: dict[str, tuple[float, list[Video]]] = {}

    @property
    def can_search(self) -> bool:
        return bool(self.api_key)

    async def lookup(self, video_id: str) -> Video:
        """Title/thumbnail for one video via oEmbed (no API key needed)."""
        params = {"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"}
        async with self.session.get(OEMBED_URL, params=params) as resp:
            if resp.status != 200:
                raise SearchError("Video not found, or it can't be embedded/played on TV")
            data = await resp.json()
        return Video(
            video_id=video_id,
            title=data.get("title", video_id),
            channel=data.get("author_name", ""),
            thumbnail=f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        )

    async def search(self, query: str, limit: int = 10) -> list[Video]:
        query = query.strip()
        if not query:
            return []
        if vid := extract_video_id(query):
            return [await self.lookup(vid)]
        if not self.api_key:
            raise SearchError("Search isn't set up yet. Paste a YouTube link instead.")
        if "karaoke" not in query.lower():
            query += " karaoke"
        key = " ".join(query.lower().split())
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        params = {
            "part": "snippet",
            "type": "video",
            "videoEmbeddable": "true",
            "maxResults": str(limit),
            "q": query,
            "key": self.api_key,
        }
        async with self.session.get(SEARCH_URL, params=params) as resp:
            data = await resp.json()
            if resp.status != 200:
                msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
                raise SearchError(f"YouTube search failed: {msg}")
        results = []
        for item in data.get("items", []):
            sn = item["snippet"]
            results.append(
                Video(
                    video_id=item["id"]["videoId"],
                    title=html.unescape(sn["title"]),
                    channel=html.unescape(sn.get("channelTitle", "")),
                    thumbnail=sn.get("thumbnails", {}).get("medium", {}).get("url", ""),
                )
            )
        if len(self._cache) >= CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))  # drop the oldest
        self._cache[key] = (time.time(), results)
        return results
