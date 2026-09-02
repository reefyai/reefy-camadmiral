from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TelegramError(Exception):
    code: str
    retry_after_seconds: int | None = None


class TelegramClient:
    def __init__(self, token: str, *, timeout: float = 10.0):
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"https://api.telegram.org/bot{urllib.parse.quote(self.token, safe=':')}/{method}"
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                result = {}
            parameters = result.get("parameters") if isinstance(result, dict) else None
            retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if exc.code in {401, 403, 404}:
                code = "invalid_bot_token" if exc.code == 401 else "telegram_rejected"
                raise TelegramError(code) from None
            raise TelegramError(
                "telegram_rate_limited" if exc.code == 429 else "telegram_unavailable",
                int(retry_after) if isinstance(retry_after, int) else 60,
            ) from None
        except (OSError, TimeoutError, ValueError, UnicodeDecodeError):
            raise TelegramError("telegram_unavailable", 60) from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise TelegramError("telegram_rejected")
        return result.get("result")

    def identity(self) -> dict[str, Any]:
        result = self._call("getMe")
        if not isinstance(result, dict) or not result.get("id") or not result.get("username"):
            raise TelegramError("invalid_bot_token")
        return result

    def webhook(self) -> dict[str, Any]:
        result = self._call("getWebhookInfo")
        return result if isinstance(result, dict) else {}

    def updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 0, "limit": 100, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        return [item for item in result if isinstance(item, dict)] if isinstance(result, list) else []

    def send(self, chat_id: str, text: str) -> str:
        result = self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        if not isinstance(result, dict) or result.get("message_id") is None:
            raise TelegramError("telegram_rejected")
        return str(result["message_id"])


def pairing_message(update: dict[str, Any], pairing_token: str) -> tuple[str, str] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = str(message.get("text") or "").strip()
    command, _, supplied = text.partition(" ")
    if command.split("@", 1)[0] != "/start" or supplied.strip() != pairing_token:
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("id") is None:
        return None
    label = (
        chat.get("title")
        or " ".join(value for value in (chat.get("first_name"), chat.get("last_name")) if value)
        or chat.get("username")
        or "Telegram chat"
    )
    return str(chat["id"]), str(label)[:160]


def notification_text(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "test":
        return "CamAdmiral test notification\n\nTelegram notifications are connected."
    if event_type == "relay_restarted":
        reason = str(payload.get("reason") or "unknown").replace("_", " ")
        camera_count = max(0, int(payload.get("camera_count") or 0))
        affected = (
            f"\nRecovered cameras: {camera_count}"
            if camera_count
            else ""
        )
        observed_at = str(payload.get("observed_at") or "")
        return (
            "CamAdmiral media relay restarted\n\n"
            f"Reason: {reason}{affected}\n"
            "Camera streams are reconnecting.\n"
            f"Observed: {observed_at}"
        )
    camera_name = str(payload.get("camera_name") or "Camera")
    kind = str(payload.get("kind") or "media_offline")
    observed_at = str(payload.get("observed_at") or "")
    if event_type == "incident_resolved":
        state = "recovered"
    elif kind == "camera_address_changed":
        state = "address changed"
    elif kind == "authentication_failed":
        state = "authentication failed"
    else:
        state = "offline"
    return f"CamAdmiral alert\n\n{camera_name}: {state}\nObserved: {observed_at}"
