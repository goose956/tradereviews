"""Cron-triggered endpoints for periodic background jobs."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request

from app.core.security import decrypt
from app.db.supabase import get_supabase
from app.services.google import get_reviews, post_review_reply, refresh_access_token
from app.services.message_log import log_message
from app.services.openai_service import generate_reply
from app.services.whatsapp import send_interactive_buttons, send_text_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cron", tags=["cron"])

_STAR_MAP: dict[str, int] = {
    "ONE": 1,
    "TWO": 2,
    "THREE": 3,
    "FOUR": 4,
    "FIVE": 5,
}


def _star_to_int(star_rating: str) -> int:
    return _STAR_MAP.get(star_rating, 5)


@router.post("/poll-reviews")
async def poll_reviews(request: Request) -> dict[str, Any]:
    """Poll Google reviews for every active business and generate AI drafts.

    Intended to be called by an external cron scheduler (e.g. cron-job.org,
    Supabase Edge Function cron, GitHub Actions, etc.).
    """
    supabase = get_supabase()
    http_client = request.app.state.http_client

    # Only process businesses with Google connected and an active subscription.
    result = (
        supabase.table("businesses")
        .select("*")
        .eq("subscription_status", "active")
        .neq("google_refresh_token", None)
        .execute()
    )
    businesses = result.data or []

    total_new = 0
    errors = 0

    for business in businesses:
        try:
            new = await _process_business(business, http_client)
            total_new += new
        except Exception:
            errors += 1
            logger.exception(
                "Error processing reviews for business %s", business["id"]
            )

    logger.info(
        "Poll complete — %d businesses, %d new drafts, %d errors",
        len(businesses),
        total_new,
        errors,
    )
    return {
        "businesses_processed": len(businesses),
        "new_drafts": total_new,
        "errors": errors,
    }


async def _process_business(
    business: dict[str, Any],
    http_client: Any,
) -> int:
    """Fetch reviews for one business, draft replies for new ones."""
    supabase = get_supabase()

    encrypted_refresh: str = business.get("google_refresh_token", "")
    account_id: str = business.get("google_account_id", "")
    location_id: str = business.get("google_location_id", "")

    if not (encrypted_refresh and account_id and location_id):
        logger.debug(
            "Skipping business %s — incomplete Google credentials", business["id"]
        )
        return 0

    # a) Decrypt refresh token
    refresh_token = decrypt(encrypted_refresh)

    # b) Exchange for fresh access token
    access_token = await refresh_access_token(refresh_token)

    # c) Fetch latest 5 reviews
    location_resource = f"accounts/{account_id}/locations/{location_id}"
    reviews = await get_reviews(access_token, location_resource, page_size=5)

    new_count = 0

    for review in reviews:
        google_review_id = review.get("reviewId", "")
        if not google_review_id:
            continue

        # d) Skip already-processed reviews
        existing = (
            supabase.table("review_drafts")
            .select("id")
            .eq("google_review_id", google_review_id)
            .execute()
        )
        if existing.data:
            continue

        # Skip reviews that already have an owner reply on Google
        if review.get("reviewReply"):
            continue

        reviewer_name = review.get("reviewer", {}).get("displayName", "A customer")
        review_text = review.get("comment", "")
        star_rating = _star_to_int(review.get("starRating", "FIVE"))

        # e) Generate AI draft
        draft_text = await generate_reply(
            business_name=business["business_name"],
            review_text=review_text,
            star_rating=star_rating,
        )

        auto_reply_enabled = business.get("auto_reply_enabled", 0)
        auto_reply_threshold = business.get("auto_reply_threshold", 4)

        # ── Auto-reply mode ──────────────────────────────────
        if auto_reply_enabled:
            if star_rating >= auto_reply_threshold:
                # Positive review → use custom or AI-generated reply
                positive_tpl = business.get("auto_reply_positive_msg") or ""
                if positive_tpl:
                    reply_text = positive_tpl.format(
                        reviewer_name=reviewer_name,
                        business_name=business["business_name"],
                    )
                else:
                    reply_text = draft_text
            else:
                # Below threshold → custom or default "we'll be in touch"
                negative_tpl = business.get("auto_reply_negative_msg") or (
                    "Thank you for your feedback, {reviewer_name}. "
                    "We're sorry your experience didn't meet expectations "
                    "and will be in touch to address any concerns."
                )
                reply_text = negative_tpl.format(
                    reviewer_name=reviewer_name,
                    business_name=business["business_name"],
                )

            # Post reply directly to Google
            review_resource = (
                f"accounts/{account_id}/locations/{location_id}"
                f"/reviews/{google_review_id}"
            )
            try:
                await post_review_reply(access_token, review_resource, reply_text)
                auto_status = "auto_replied"
            except Exception:
                logger.exception(
                    "Failed to auto-reply to review %s", google_review_id
                )
                auto_status = "auto_reply_failed"

            # Save record for audit
            supabase.table("review_drafts").insert(
                {
                    "business_id": business["id"],
                    "google_review_id": google_review_id,
                    "reviewer_name": reviewer_name,
                    "review_text": review_text,
                    "star_rating": star_rating,
                    "ai_draft_reply": reply_text,
                    "status": auto_status,
                    "sent_to_owner": True,
                }
            ).execute()

            # Notify owner via WhatsApp
            owner_phone = business["phone_number"].lstrip("+")
            notify_text = (
                f"🤖 Auto-replied to {star_rating}-star review from {reviewer_name}:\n\n"
                f'"{review_text[:300]}"\n\n'
                f"Reply posted:\n"
                f'"{reply_text}"'
            )
            await send_text_message(http_client, owner_phone, notify_text)

            log_message(
                business_id=business["id"],
                to_phone=business["phone_number"],
                message_body=f"Auto-replied to {star_rating}-star review from {reviewer_name}",
                message_type="auto_reply_notification",
            )

            new_count += 1
            logger.info(
                "Auto-replied to review %s (business %s, status=%s)",
                google_review_id,
                business["id"],
                auto_status,
            )
            continue

        # ── Manual approval mode (default) ───────────────────
        # f) Insert review_drafts row with status 'pending_approval'
        insert_result = (
            supabase.table("review_drafts")
            .insert(
                {
                    "business_id": business["id"],
                    "google_review_id": google_review_id,
                    "reviewer_name": reviewer_name,
                    "review_text": review_text,
                    "star_rating": star_rating,
                    "ai_draft_reply": draft_text,
                    "status": "pending_approval",
                    "sent_to_owner": True,
                }
            )
            .execute()
        )
        draft_id = insert_result.data[0]["id"] if insert_result.data else "unknown"

        # g) Send WhatsApp message with Approve / Edit buttons
        owner_phone = business["phone_number"].lstrip("+")
        body = (
            f"⭐ New {star_rating}-star review from {reviewer_name}:\n\n"
            f'"{review_text[:500]}"\n\n'
            f"Suggested reply:\n"
            f'"{draft_text}"'
        )
        buttons = [
            {"id": f"approve_{draft_id}", "title": "Approve"},
            {"id": f"edit_{draft_id}", "title": "Edit"},
        ]
        await send_interactive_buttons(http_client, owner_phone, body, buttons)

        log_message(
            business_id=business["id"],
            to_phone=business["phone_number"],
            message_body=f"New {star_rating}-star review from {reviewer_name} — draft reply sent for approval",
            message_type="draft_notification",
        )

        new_count += 1
        logger.info(
            "Draft %s created for review %s (business %s)",
            draft_id,
            google_review_id,
            business["id"],
        )

    return new_count


# ──────────────────────────────────────────────
# Follow-up reminders
# ──────────────────────────────────────────────
@router.post("/send-follow-ups")
async def send_follow_ups(request: Request) -> dict[str, Any]:
    """Send follow-up review requests to customers who haven't responded.

    Checks each business's follow_up_enabled / follow_up_days / max_follow_ups
    settings.  Intended to be called once daily by an external cron scheduler.
    """
    db = get_supabase()
    http_client = request.app.state.http_client

    # Get businesses that have follow-ups enabled
    biz_result = (
        db.table("businesses")
        .select("*")
        .eq("subscription_status", "active")
        .eq("follow_up_enabled", 1)
        .execute()
    )
    businesses = biz_result.data or []

    sent = 0
    errors = 0

    for biz in businesses:
        follow_up_days = biz.get("follow_up_days", 3)
        max_follow_ups = biz.get("max_follow_ups", 2)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=follow_up_days)).isoformat()

        # Find customers who:
        #   - belong to this business
        #   - status is request_sent or follow_up_sent (haven't responded)
        #   - were requested (or last followed up) more than N days ago
        #   - haven't exceeded max follow-ups
        for status in ("request_sent", "follow_up_sent"):
            customers = (
                db.table("customers")
                .select("*")
                .eq("business_id", biz["id"])
                .eq("status", status)
                .execute()
            ).data or []

            for cust in customers:
                follow_up_count = cust.get("follow_up_count", 0)
                if follow_up_count >= max_follow_ups:
                    continue

                # Check if enough time has passed
                last_contact = cust.get("last_follow_up_at") or cust.get("review_requested_at")
                if not last_contact or last_contact > cutoff:
                    continue

                try:
                    first_name = cust["name"].split()[0]
                    # Pick the right message for this follow-up number
                    if follow_up_count == 0:
                        template = biz.get("follow_up_message") or (
                            "Hi {first_name}, just a friendly reminder from "
                            "{business_name} — we'd really appreciate "
                            "your feedback! It only takes 30 seconds. 😊"
                        )
                    elif follow_up_count == 1:
                        template = biz.get("follow_up_message_2") or (
                            "Hi {first_name}, we don't want to be a pest! "
                            "But if you have a spare moment, {business_name} "
                            "would love to hear how we did. Thank you! 🙏"
                        )
                    else:
                        template = biz.get("follow_up_message_3") or (
                            "Hi {first_name}, last reminder from {business_name} "
                            "— your feedback really helps us improve. "
                            "We'd be grateful if you could share your experience. Thanks! ⭐"
                        )
                    follow_up_body = template.format(
                        first_name=first_name,
                        business_name=biz["business_name"],
                    )
                    await send_text_message(
                        http_client,
                        cust["phone_number"].lstrip("+"),
                        follow_up_body,
                    )

                    log_message(
                        business_id=biz["id"],
                        to_phone=cust["phone_number"],
                        message_body=follow_up_body,
                        message_type="follow_up",
                    )

                    db.table("customers").update({
                        "follow_up_count": follow_up_count + 1,
                        "last_follow_up_at": datetime.now(timezone.utc).isoformat(),
                        "status": "follow_up_sent",
                    }).eq("id", cust["id"]).execute()

                    sent += 1
                except Exception:
                    errors += 1
                    logger.exception(
                        "Failed to send follow-up to customer %s", cust["id"]
                    )

    logger.info("Follow-ups complete — %d sent, %d errors", sent, errors)
    return {"follow_ups_sent": sent, "errors": errors}
