from __future__ import annotations

import base64
import binascii
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable


MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 5 * 60
LOCKOUT_SECONDS = 5 * 60


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    status_code: int = 200
    retry_after: int | None = None


class AdminAuthenticator:
    """Validate HTTP Basic credentials with bounded per-client lockout."""

    def __init__(self, password_loader: Callable[[], bytes | None]) -> None:
        self.password_loader = password_loader
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _credentials(header: str | None) -> tuple[str, str] | None:
        if not header or not header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError):
            return None
        if ":" not in decoded:
            return None
        username, password = decoded.split(":", 1)
        return username, password

    def _record_failure(self, client: str, now: float) -> AuthDecision:
        with self._lock:
            failures = self._failures[client]
            while failures and failures[0] <= now - FAILURE_WINDOW_SECONDS:
                failures.popleft()
            failures.append(now)
            if len(failures) >= MAX_FAILURES:
                self._locked_until[client] = now + LOCKOUT_SECONDS
                failures.clear()
                return AuthDecision(False, 429, LOCKOUT_SECONDS)
        return AuthDecision(False, 401)

    def authenticate(self, client: str, header: str | None, *, now: float | None = None) -> AuthDecision:
        password = self.password_loader()
        if password is None:
            return AuthDecision(True)
        now = time.monotonic() if now is None else now
        with self._lock:
            locked_until = self._locked_until.get(client, 0)
            if locked_until > now:
                return AuthDecision(False, 429, max(1, round(locked_until - now)))
            self._locked_until.pop(client, None)
        credentials = self._credentials(header)
        if credentials is None:
            return self._record_failure(client, now)
        username, candidate = credentials
        allowed = secrets.compare_digest(username.encode(), b"admin") and secrets.compare_digest(
            candidate.encode(), password
        )
        if not allowed:
            return self._record_failure(client, now)
        with self._lock:
            self._failures.pop(client, None)
            self._locked_until.pop(client, None)
        return AuthDecision(True)
