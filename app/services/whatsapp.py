"""Async helpers for sending messages via the Meta WhatsApp Cloud API."""

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

META_GRAPH_URL = "https://graph.facebook.com/v22.0"


def _headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }


def _messages_url() -> str:
    settings = get_settings()
    return f"{META_GRAPH_URL}/{settings.whatsapp_phone_number_id}/messages"


async def send_template_message(
    client: httpx.AsyncClient,
    to_phone: str,
    customer_name: str,
    business_name: str,
) -> dict[str, Any]:
    """Send a WhatsApp template message requesting a review.

    The template is expected to have body parameters:
        {{1}} = customer first name
        {{2}} = business name
    And two quick-reply buttons:
        index 0 — "Great!" (payload: review_great)
        index 1 — "Could be better" (payload: could_be_better)
    """
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": "review_request",
            "language": {"code": "en"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": customer_name},
                        {"type": "text", "text": business_name},
                    ],
                },
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "0",
                    "parameters": [{"type": "payload", "payload": "review_great"}],
                },
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": "1",
                    "parameters": [{"type": "payload", "payload": "could_be_better"}],
                },
            ],
        },
    }

    response = await client.post(_messages_url(), headers=_headers(), json=payload)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    logger.info("Template sent to %s — wamid: %s", to_phone, data.get("messages"))
    return data


async def send_text_message(
    client: httpx.AsyncClient,
    to_phone: str,
    body: str,
) -> dict[str, Any]:
    """Send a plain text WhatsApp message."""
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": body},
    }

    response = await client.post(_messages_url(), headers=_headers(), json=payload)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    logger.info("Text sent to %s", to_phone)
    return data


async def send_interactive_buttons(
    client: httpx.AsyncClient,
    to_phone: str,
    body_text: str,
    buttons: list[dict[str, str]],
) -> dict[str, Any]:
    """Send an interactive button message (used for Approve / Reject draft replies).

    Each button dict: {"id": "approve_<draft_id>", "title": "Approve"}
    """
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": btn} for btn in buttons[:3]
                ]
            },
        },
    }

    response = await client.post(_messages_url(), headers=_headers(), json=payload)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    logger.info("Interactive msg sent to %s", to_phone)
    return data


async def upload_media(
    client: httpx.AsyncClient,
    file_bytes: bytes,
    mime_type: str = "application/pdf",
    filename: str = "document.pdf",
) -> str:
    """Upload a file to WhatsApp media API and return the media ID."""
    settings = get_settings()
    url = f"{META_GRAPH_URL}/{settings.whatsapp_phone_number_id}/media"
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}

    response = await client.post(
        url,
        headers=headers,
        data={"messaging_product": "whatsapp", "type": mime_type},
        files={"file": (filename, file_bytes, mime_type)},
    )
    response.raise_for_status()
    data = response.json()
    media_id = data.get("id", "")
    logger.info("Media uploaded — id: %s", media_id)
    return media_id


async def send_interactive_list(
    client: httpx.AsyncClient,
    to_phone: str,
    body_text: str,
    button_text: str,
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    """Send an interactive list message (for selecting from many options).

    Each section: {"title": "…", "rows": [{"id": "…", "title": "…", "description": "…"}, …]}
    """
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_text,
                "sections": sections,
            },
        },
    }

    response = await client.post(_messages_url(), headers=_headers(), json=payload)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    logger.info("List msg sent to %s", to_phone)
    return data


async def send_document_message(
    client: httpx.AsyncClient,
    to_phone: str,
    media_id: str,
    filename: str = "document.pdf",
    caption: str = "",
) -> dict[str, Any]:
    """Send a document via WhatsApp using a previously uploaded media ID."""
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "document",
        "document": {
            "id": media_id,
            "filename": filename,
            "caption": caption,
        },
    }

    response = await client.post(_messages_url(), headers=_headers(), json=payload)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    logger.info("Document sent to %s — filename: %s", to_phone, filename)
    return data


async def download_media(client: httpx.AsyncClient, media_id: str) -> tuple[bytes, str]:
    """Download media from WhatsApp by media ID. Returns (file_bytes, mime_type)."""
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}

    # Step 1: Get the media URL
    meta_resp = await client.get(f"{META_GRAPH_URL}/{media_id}", headers=headers)
    meta_resp.raise_for_status()
    media_url = meta_resp.json().get("url", "")
    mime_type = meta_resp.json().get("mime_type", "image/jpeg")

    # Step 2: Download the actual file
    file_resp = await client.get(media_url, headers=headers)
    file_resp.raise_for_status()
    logger.info("Media downloaded — id: %s, mime: %s, size: %d bytes", media_id, mime_type, len(file_resp.content))
    return file_resp.content, mime_type
