"""Content moderation via OpenAI Moderation API (free endpoint)."""

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

MODERATION_URL = "https://api.openai.com/v1/moderations"


async def check_content(text: str) -> dict[str, Any]:
    """Check text against OpenAI moderation. Returns dict with 'flagged' bool and 'categories'."""
    settings = get_settings()
    if not settings.openai_api_key or not text.strip():
        return {"flagged": False, "categories": {}}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                MODERATION_URL,
                headers={
                    "Authorization": f"Bearer {settings.openai_api_key}",
                    "Content-Type": "application/json",
                },
                json={"input": text},
            )
            resp.raise_for_status()
            data = resp.json()
            result = data["results"][0]
            if result["flagged"]:
                cats = [k for k, v in result["categories"].items() if v]
                logger.warning("Content flagged: %s — categories: %s", text[:100], cats)
            return {
                "flagged": result["flagged"],
                "categories": result["categories"],
            }
    except Exception:
        logger.exception("Moderation API call failed — allowing content through")
        return {"flagged": False, "categories": {}}


async def moderate_outbound(text: str) -> str | None:
    """Check outbound customer-facing text. Returns warning string if blocked, None if OK."""
    result = await check_content(text)
    if result["flagged"]:
        flagged_cats = [k.replace("/", " / ") for k, v in result["categories"].items() if v]
        return (
            "⚠️ *Message blocked* — your message was flagged for: "
            + ", ".join(flagged_cats)
            + ".\n\nPlease rewrite it and try again. "
            "All outbound messages are checked to protect your account."
        )
    return None
