"""Orchestrator — post approved review drafts to Google."""

import logging
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.security import decrypt
from app.db.supabase import get_supabase
from app.services.google import post_review_reply, refresh_access_token
from app.services.whatsapp import send_text_message

logger = logging.getLogger(__name__)


async def post_approved_replies(http_client: httpx.AsyncClient) -> int:
    """Find approved drafts and post them to Google, then mark as 'posted'.

    Returns the number of replies posted.
    """
    supabase = get_supabase()

    drafts_result = (
        supabase.table("review_drafts")
        .select("*, businesses(*)")
        .eq("status", "approved")
        .execute()
    )
    drafts = drafts_result.data or []
    posted = 0

    for draft in drafts:
        business = draft.get("businesses") or {}
        encrypted_refresh = business.get("google_refresh_token", "")
        account_id = business.get("google_account_id", "")
        location_id = business.get("google_location_id", "")

        if not (encrypted_refresh and account_id and location_id):
            continue

        try:
            refresh_token = decrypt(encrypted_refresh)
            access_token = await refresh_access_token(refresh_token)
        except Exception:
            logger.exception("Failed to refresh token for business %s", business.get("id"))
            continue

        review_name = (
            f"accounts/{account_id}/locations/{location_id}"
            f"/reviews/{draft['google_review_id']}"
        )

        try:
            await post_review_reply(access_token, review_name, draft["ai_draft_reply"])
            supabase.table("review_drafts").update({"status": "posted"}).eq(
                "id", draft["id"]
            ).execute()

            # Notify owner
            owner_phone = business.get("phone_number", "").lstrip("+")
            if owner_phone:
                await send_text_message(
                    http_client,
                    owner_phone,
                    f"Your approved reply to {draft.get('reviewer_name', 'a reviewer')} "
                    f"has been posted on Google.",
                )
            posted += 1
        except Exception:
            logger.exception("Failed to post reply for draft %s", draft["id"])

    logger.info("Posted %d approved replies", posted)
    return posted
    payload = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        response = await client.post(GOOGLE_TOKEN_URL, data=payload)
        response.raise_for_status()
        return response.json().get("access_token", "")
    except httpx.HTTPStatusError:
        logger.exception("Failed to refresh Google access token")
        return ""
