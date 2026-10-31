"""Secrets and paths, read from environment variables only.

Nothing here is ever printed with its value; errors name the variable.

Standard library only.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional

MIN_KEY_LENGTH = 16


class SettingsError(ValueError):
    """An environment variable is missing or unusable. The message names it, never its value."""


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    chat_id: str
    api_key: str
    webhook_secret: str
    config_path: str
    state_path: str
    host: str
    port: int

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        missing = [n for n in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "JAA_API_KEY", "TELEGRAM_WEBHOOK_SECRET") if not env.get(n)]
        if missing:
            raise SettingsError("missing environment variables: " + ", ".join(missing))
        if not re.fullmatch(r"-?\d{1,20}", env["TELEGRAM_CHAT_ID"]):
            raise SettingsError("TELEGRAM_CHAT_ID must be a number")
        for name in ("JAA_API_KEY", "TELEGRAM_WEBHOOK_SECRET"):
            if len(env[name]) < MIN_KEY_LENGTH:
                raise SettingsError(f"{name} must be at least {MIN_KEY_LENGTH} characters")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", env["TELEGRAM_WEBHOOK_SECRET"]):
            raise SettingsError("TELEGRAM_WEBHOOK_SECRET may only hold letters, digits, _ and - (Telegram's rule)")
        try:
            port = int(env.get("JAA_PORT", "8080"))
        except ValueError:
            raise SettingsError("JAA_PORT must be a number")
        return cls(
            telegram_token=env["TELEGRAM_BOT_TOKEN"],
            chat_id=env["TELEGRAM_CHAT_ID"],
            api_key=env["JAA_API_KEY"],
            webhook_secret=env["TELEGRAM_WEBHOOK_SECRET"],
            config_path=env.get("JAA_CONFIG", "config.json"),
            state_path=env.get("JAA_STATE_FILE", "state.json"),
            host=env.get("JAA_HOST", "127.0.0.1"),
            port=port,
        )
