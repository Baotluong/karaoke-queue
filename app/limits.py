"""Per-client rate limits, and finding the real client IP behind the tunnel."""

from __future__ import annotations

import time
from collections import deque

from starlette.requests import HTTPConnection

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def client_ip(conn: HTTPConnection) -> str:
    """The guest's IP address.

    cloudflared runs on this machine, so tunnel traffic arrives from loopback with
    the visitor's address in CF-Connecting-IP. Only trust that header from
    loopback: a phone on the Wi-Fi could otherwise fake it to dodge limits.
    """
    peer = conn.client.host if conn.client else "?"
    if peer in LOOPBACK:
        forwarded = conn.headers.get("cf-connecting-ip")
        if forwarded:
            return forwarded.strip()
    return peer


def is_https(conn: HTTPConnection) -> bool:
    if conn.url.scheme in ("https", "wss"):
        return True
    peer = conn.client.host if conn.client else "?"
    return peer in LOOPBACK and conn.headers.get("x-forwarded-proto") == "https"


class RateLimiter:
    """At most `limit` events per `window` seconds for each key (sliding window)."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._events: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._events.setdefault(key, deque())
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def retry_after(self, key: str) -> float:
        """Seconds until `key` may act again (0 if allowed now)."""
        now = time.time()
        q = self._prune(key, now)
        if len(q) < self.limit:
            return 0.0
        return q[0] + self.window - now

    def hit(self, key: str) -> float:
        """Record an event if allowed. Returns 0 if allowed, else seconds to wait."""
        wait = self.retry_after(key)
        if not wait:
            self._events[key].append(time.time())
        return wait
