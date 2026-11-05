"""Admin PIN login: a shared PIN, a cookie derived from it, and brute-force lockout."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

log = logging.getLogger(__name__)

MAX_FAILURES = 5
LOCKOUT_SECONDS = 60


class AdminAuth:
    def __init__(self, pin: str | None) -> None:
        if not pin:
            pin = f"{secrets.randbelow(10**6):06d}"
            log.warning("KQ_ADMIN_PIN is not set. Admin PIN for this run: %s", pin)
        self.pin = pin
        # The cookie value is derived from the PIN, so admins stay logged in across
        # restarts, and changing the PIN logs everyone out.
        self.token = hmac.new(pin.encode(), b"karaoke-queue-admin-v1", hashlib.sha256).hexdigest()
        self._failures: dict[str, tuple[int, float]] = {}  # client -> (count, locked_until)

    def is_admin(self, cookie: str | None) -> bool:
        return bool(cookie) and hmac.compare_digest(cookie, self.token)

    def locked_for(self, client: str) -> float:
        _, until = self._failures.get(client, (0, 0.0))
        return max(0.0, until - time.time())

    def check_pin(self, client: str, pin: str) -> bool:
        if self.locked_for(client):
            return False
        if hmac.compare_digest(pin.strip().encode(), self.pin.encode()):
            self._failures.pop(client, None)
            return True
        count = self._failures.get(client, (0, 0.0))[0] + 1
        until = time.time() + LOCKOUT_SECONDS if count >= MAX_FAILURES else 0.0
        self._failures[client] = (0 if until else count, until)
        return False
