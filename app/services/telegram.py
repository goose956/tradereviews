"""Async helpers for sending messages via the Telegram Bot API."""

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


def _base_url() -> str:
    token = get_settings().telegram_bot_token
    return f"https://api.telegram.org/bot{token}"


async def send_text(
    client: httpx.AsyncClient,
    chat_id: str | int,
    text: str,
    parse_mode: str = "Markdown",
    reply_markup: dict | None = None,
) -> dict[str, Any]:
    """Send a plain text message to a Telegram chat."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    response = await client.post(f"{_base_url()}/sendMessage", json=payload)
    if not response.is_success:
        logger.warning("Telegram sendMessage failed %s: %s", response.status_code, response.text)
    data: dict[str, Any] = response.json()
    return data


async def send_buttons(
    client: httpx.AsyncClient,
    chat_id: str | int,
    text: str,
    buttons: list[dict[str, str]],
    parse_mode: str = "Markdown",
) -> dict[str, Any]:
    """Send a message with inline keyboard buttons.

    Each button dict: {"id": "callback_data", "title": "Button Label"}
    Buttons are arranged one per row (max 3 per row for wider labels).
    """
    keyboard = [[{"text": btn["title"], "callback_data": btn["id"][:64]}] for btn in buttons]
    reply_markup = {"inline_keyboard": keyboard}
    return await send_text(client, chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)


async def send_list(
    client: httpx.AsyncClient,
    chat_id: str | int,
    text: str,
    button_text: str,
    sections: list[dict[str, Any]],
    parse_mode: str = "Markdown",
) -> dict[str, Any]:
    """Send a message with a list of options as inline keyboard buttons.

    Mirrors the WhatsApp interactive list interface.
    Each section: {"title": "…", "rows": [{"id": "…", "title": "…", "description": "…"}]}
    """
    keyboard = []
    for section in sections:
        for row in section.get("rows", []):
            label = row["title"]
            desc = row.get("description", "")
            # Telegram doesn't support descriptions natively — append inline if short
            if desc and len(label) + len(desc) < 50:
                label = f"{label} — {desc}"
            elif desc:
                label = label[:40]
            keyboard.append([{"text": label, "callback_data": row["id"][:64]}])

    reply_markup = {"inline_keyboard": keyboard}
    return await send_text(client, chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)


async def send_document(
    client: httpx.AsyncClient,
    chat_id: str | int,
    file_bytes: bytes,
    filename: str = "document.pdf",
    caption: str = "",
) -> dict[str, Any]:
    """Send a document (PDF) directly to a Telegram chat."""
    url = f"{_base_url()}/sendDocument"
    data: dict[str, Any] = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "Markdown"

    response = await client.post(
        url,
        data=data,
        files={"document": (filename, file_bytes, "application/pdf")},
    )
    if not response.is_success:
        logger.warning("Telegram sendDocument failed %s: %s", response.status_code, response.text)
    return response.json()


async def send_photo(
    client: httpx.AsyncClient,
    chat_id: str | int,
    photo_url: str,
    caption: str = "",
) -> dict[str, Any]:
    """Send a photo by URL."""
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "photo": photo_url,
    }
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "Markdown"
    response = await client.post(f"{_base_url()}/sendPhoto", json=payload)
    return response.json()


async def answer_callback_query(
    client: httpx.AsyncClient,
    callback_query_id: str,
    text: str = "",
) -> None:
    """Acknowledge a callback query (removes the loading spinner on the button)."""
    await client.post(
        f"{_base_url()}/answerCallbackQuery",
        json={"callback_query_id": callback_query_id, "text": text},
    )


async def set_webhook(
    client: httpx.AsyncClient,
    webhook_url: str,
    secret_token: str = "",
) -> dict[str, Any]:
    """Register the bot's webhook URL with Telegram."""
    payload: dict[str, Any] = {"url": webhook_url}
    if secret_token:
        payload["secret_token"] = secret_token
    response = await client.post(f"{_base_url()}/setWebhook", json=payload)
    return response.json()


async def get_file(
    client: httpx.AsyncClient,
    file_id: str,
) -> dict[str, Any]:
    """Get file info (path) for a Telegram file_id."""
    response = await client.get(f"{_base_url()}/getFile", params={"file_id": file_id})
    return response.json()


async def download_file(
    client: httpx.AsyncClient,
    file_path: str,
) -> bytes:
    """Download a file from Telegram servers given its file_path."""
    token = get_settings().telegram_bot_token
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    response = await client.get(url)
    response.raise_for_status()
    return response.content
