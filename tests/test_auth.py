from __future__ import annotations

import base64
import unittest

from camadmiral.auth import AdminAuthenticator, LOCKOUT_SECONDS, MAX_FAILURES


def basic(username: str, password: str) -> str:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


class AdminAuthenticatorTests(unittest.TestCase):
    def test_missing_secret_keeps_proxy_managed_installation_compatible(self) -> None:
        authenticator = AdminAuthenticator(lambda: None)
        self.assertTrue(authenticator.authenticate("client", None).allowed)

    def test_valid_admin_credentials_are_accepted(self) -> None:
        authenticator = AdminAuthenticator(lambda: b"synthetic-password")
        self.assertTrue(
            authenticator.authenticate(
                "client", basic("admin", "synthetic-password"), now=1
            ).allowed
        )

    def test_failures_are_rate_limited_per_client(self) -> None:
        authenticator = AdminAuthenticator(lambda: b"synthetic-password")
        for attempt in range(MAX_FAILURES - 1):
            decision = authenticator.authenticate("client", basic("admin", "wrong"), now=attempt)
            self.assertEqual(decision.status_code, 401)
        locked = authenticator.authenticate("client", basic("admin", "wrong"), now=10)
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(locked.retry_after, LOCKOUT_SECONDS)
        still_locked = authenticator.authenticate(
            "client", basic("admin", "synthetic-password"), now=11
        )
        self.assertEqual(still_locked.status_code, 429)
        recovered = authenticator.authenticate(
            "client", basic("admin", "synthetic-password"), now=10 + LOCKOUT_SECONDS
        )
        self.assertTrue(recovered.allowed)


if __name__ == "__main__":
    unittest.main()
