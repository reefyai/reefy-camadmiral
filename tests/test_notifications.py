import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from camadmiral.notifications import (
    TelegramClient,
    TelegramError,
    notification_text,
    pairing_message,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class TelegramNotificationTests(unittest.TestCase):
    def test_identity_and_send_use_https_bot_api(self) -> None:
        responses = [
            FakeResponse({"ok": True, "result": {"id": 123, "username": "synthetic_bot"}}),
            FakeResponse({"ok": True, "result": {"message_id": 44}}),
        ]
        with patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            client = TelegramClient("123:synthetic-token")
            self.assertEqual(client.identity()["username"], "synthetic_bot")
            self.assertEqual(client.send("987", "Safe alert"), "44")

        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertTrue(all(request.full_url.startswith("https://api.telegram.org/bot") for request in requests))
        sent = json.loads(requests[1].data)
        self.assertEqual(sent, {"chat_id": "987", "text": "Safe alert", "disable_web_page_preview": True})

    def test_rate_limit_is_sanitized_and_retryable(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.telegram.org/redacted",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(json.dumps({"parameters": {"retry_after": 12}}).encode()),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TelegramError) as raised:
                TelegramClient("123:synthetic-token").send("987", "Safe alert")
        self.assertEqual(raised.exception.code, "telegram_rate_limited")
        self.assertEqual(raised.exception.retry_after_seconds, 12)
        self.assertNotIn("synthetic-token", str(raised.exception))

    def test_pairing_accepts_only_matching_start_parameter(self) -> None:
        update = {
            "update_id": 9,
            "message": {
                "text": "/start expected-pairing",
                "chat": {"id": 987, "first_name": "Synthetic", "last_name": "Operator"},
            },
        }
        self.assertEqual(
            pairing_message(update, "expected-pairing"),
            ("987", "Synthetic Operator"),
        )
        self.assertIsNone(pairing_message(update, "different-pairing"))

    def test_alert_text_contains_no_network_or_media_details(self) -> None:
        message = notification_text(
            "incident_opened",
            {
                "camera_name": "Synthetic entrance",
                "kind": "media_offline",
                "observed_at": "2026-01-01T00:00:00+00:00",
            },
        )
        self.assertIn("Synthetic entrance: offline", message)
        self.assertNotIn("rtsp://", message)
        self.assertNotIn("MAC", message)

    def test_address_and_relay_restart_messages_are_explicit_and_secret_free(self) -> None:
        address_message = notification_text(
            "incident_opened",
            {
                "camera_name": "Synthetic entrance",
                "kind": "camera_address_changed",
                "observed_at": "2026-01-01T00:00:00+00:00",
            },
        )
        restart_message = notification_text(
            "relay_restarted",
            {
                "reason": "camera_address_recovery",
                "camera_count": 2,
                "observed_at": "2026-01-01T00:00:01+00:00",
            },
        )

        self.assertIn("Synthetic entrance: address changed", address_message)
        self.assertIn("media relay restarted", restart_message)
        self.assertIn("Recovered cameras: 2", restart_message)
        self.assertIn("Camera streams are reconnecting", restart_message)
        self.assertNotIn("rtsp://", restart_message)


if __name__ == "__main__":
    unittest.main()
