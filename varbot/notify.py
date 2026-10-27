"""Deliver a report to a Telegram chat through a bot (BotFather token)."""

from __future__ import annotations

from varbot.config import SETTINGS
from varbot.http import http

MAX_LEN = 4000  # Telegram caps a message at 4096 characters


def chunk(text: str, size: int = MAX_LEN) -> list[str]:
    parts: list[str] = []
    while text:
        if len(text) <= size:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


def send_telegram(text: str, *, chat_id: str | None = None, token: str | None = None, max_messages: int | None = None) -> int:
    token = token or SETTINGS.telegram_bot_token
    chat_id = chat_id or SETTINGS.telegram_chat_id
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required to notify")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    parts = chunk(text)
    if max_messages is not None and len(parts) > max_messages:
        parts = parts[:max_messages]
        parts[-1] = parts[-1][: MAX_LEN - 60].rstrip() + "\n… (truncated; full report in the reports directory)"
    n = 0
    for part in parts:
        http().post_json(url, {"chat_id": chat_id, "text": part, "disable_web_page_preview": True})
        n += 1
    return n
