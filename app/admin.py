"""Admin PIN login: a shared PIN, a cookie derived from it, and brute-force lockouts."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

from .limits import RateLimiter

log = logging.getLogger(__name__)

MAX_FAILURES = 5  # per client, then that client waits LOCKOUT_SECONDS
LOCKOUT_SECONDS = 60
GLOBAL_MAX_FAILURES = 30  # from everyone combined within GLOBAL_WINDOW: lock all logins
GLOBAL_WINDOW = 10 * 60


class AdminAuth:
    def __init__(self, pin: str | None) -> None:
        if not pin:
            pin = f"{secrets.randbelow(10**6):06d}"
            log.warning("KQ_ADMIN_PIN is not set. Admin PIN for this run: %s", pin)
        elif len(pin) < 6:
            log.warning("KQ_ADMIN_PIN is short. If the party is on the internet, use 6+ characters.")
        self.pin = pin
        # The cookie value is derived from the PIN, so admins stay logged in across
        # restarts, and changing the PIN logs everyone out.
        self.token = hmac.new(pin.encode(), b"karaoke-queue-admin-v1", hashlib.sha256).hexdigest()
        self._failures: dict[str, tuple[int, float]] = {}  # client -> (count, locked_until)
        # Someone guessing from many IP addresses gets past per-client lockouts,
        # so also stop all logins for a while after too many failures overall.
        self._global = RateLimiter(GLOBAL_MAX_FAILURES, GLOBAL_WINDOW)

    def is_admin(self, cookie: str | None) -> bool:
        return bool(cookie) and hmac.compare_digest(cookie, self.token)

    def locked_for(self, client: str) -> float:
        _, until = self._failures.get(client, (0, 0.0))
        return max(until - time.time(), self._global.retry_after("*"), 0.0)

    def check_pin(self, client: str, pin: str) -> bool:
        if self.locked_for(client):
            return False
        if hmac.compare_digest(pin.strip().encode(), self.pin.encode()):
            self._failures.pop(client, None)
            return True
        self._global.hit("*")
        count = self._failures.get(client, (0, 0.0))[0] + 1
        until = time.time() + LOCKOUT_SECONDS if count >= MAX_FAILURES else 0.0
        self._failures[client] = (0 if until else count, until)
        return False
