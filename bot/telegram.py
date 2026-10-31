"""A very small Telegram Bot API client.

Only what this project needs. The transport is a plain function so the tests
can swap in a fake and no test ever touches the network.

The bot token is part of every Telegram URL, so errors raised here never
contain the URL or the token.

Standard library only.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """Telegram refused a call or could not be reached. The message never holds the token."""


Transport = Callable[[str, Dict[str, Any]], Dict[str, Any]]


def http_transport(token: str) -> Transport:
    """The real transport: POST JSON to the Bot API and return the parsed answer."""

    def call(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{API}/bot{token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as err:
            try:
                body = json.loads(err.read().decode() or "{}")
            except Exception:  # an unreadable error body just means no description
                body = {}
            raise TelegramError(f"{method}: HTTP {err.code} {body.get('description', '')}".strip())
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise TelegramError(f"{method}: {type(err).__name__}")

    return call


class Telegram:
    def __init__(self, chat_id: str, transport: Transport):
        self.chat_id = str(chat_id)
        self._call = transport

    def _ok(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        answer = self._call(method, payload)
        if not answer.get("ok"):
            raise TelegramError(f"{method}: {answer.get('description', 'not ok')}")
        return answer

    def post_message(self, text: str, keyboard: Optional[Dict] = None, html: bool = True, silent: bool = False) -> int:
        """Post a message to the chat. Returns the Telegram message id. silent: no sound or buzz."""
        payload: Dict[str, Any] = {"chat_id": self.chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if silent:
            payload["disable_notification"] = True
        if html:
            payload["parse_mode"] = "HTML"
        if keyboard:
            payload["reply_markup"] = keyboard
        return int(self._ok("sendMessage", payload)["result"]["message_id"])

    def get_me(self) -> Dict[str, Any]:
        """Who the bot is. A wrong token fails here."""
        return self._ok("getMe", {})["result"]

    def set_keyboard(self, message_id: int, keyboard: Dict) -> None:
        self._ok("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": message_id, "reply_markup": keyboard})

    def answer_tap(self, callback_id: str, text: str) -> None:
        self._ok("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})

    def set_webhook(self, url: str, secret: str) -> None:
        self._ok("setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["callback_query"]})
