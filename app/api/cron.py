"""Cron-triggered endpoints for periodic background jobs."""

import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request, Header, HTTPException, Depends

from app.core.config import get_settings
from app.core.security import decrypt
from app.db.supabase import get_supabase
from app.services.google import (
    get_reviews,
    get_reviews_by_place_id,
    post_review_reply,
    refresh_access_token,
)
from app.services.message_log import log_message
from app.services.openai_service import generate_reply
from app.services.whatsapp import send_interactive_buttons, send_text_message

logger = logging.getLogger(__name__)


def _match_review_to_customer(
    supabase: Any,
    business_id: str,
    reviewer_name: str,
) -> None:
    """Try to match a Google review back to a customer we sent a request to.

    If matched, mark the customer as 'review_posted' so follow-ups stop.
    Matching is name-based (case-insensitive substring).
    """
    if not reviewer_name or reviewer_name == "A customer":
        return

    name_lower = reviewer_name.strip().lower()

    candidates = (
        supabase.table("customers")
        .select("id, name, status")
        .eq("business_id", business_id)
        .neq("status", "review_posted")
        .execute()
    )
    for cust in candidates.data or []:
        cust_name = (cust.get("name") or "").strip().lower()
        if not cust_name:
            continue
        # Match if either name contains the other (handles "John" vs "John Smith")
        if cust_name in name_lower or name_lower in cust_name:
            supabase.table("customers").update(
                {"status": "review_posted"}
            ).eq("id", cust["id"]).execute()
            logger.info(
                "Matched review from '%s' to customer %s (business %s) — marked review_posted",
                reviewer_name, cust["id"], business_id,
            )
            return


async def _require_cron_secret(x_admin_key: str = Header(default="")) -> None:
    """Protect cron endpoints with the same admin secret."""
    secret = get_settings().admin_secret
    if not secret or not hmac.compare_digest(x_admin_key, secret):
        raise HTTPException(status_code=403, detail="Forbidden")


router = APIRouter(
    prefix="/cron", tags=["cron"],
    dependencies=[Depends(_require_cron_secret)],
)

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

    Supports two modes:
    - Full OAuth (google_refresh_token set): can auto-reply
    - API-key only (google_place_id set, no OAuth): notification only
    """
    supabase = get_supabase()
    http_client = request.app.state.http_client

    # All active businesses that have some form of Google connection
    result = (
        supabase.table("businesses")
        .select("*")
        .eq("subscription_status", "active")
        .execute()
    )
    businesses = [
        b for b in (result.data or [])
        if b.get("google_refresh_token") or b.get("google_place_id")
    ]

    total_new = 0
    errors = 0

    for business in businesses:
        try:
            has_oauth = bool(
                business.get("google_refresh_token")
                and business.get("google_account_id")
                and business.get("google_location_id")
            )
            if has_oauth:
                new = await _process_business(business, http_client)
            else:
                new = await _process_business_places(business, http_client)
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

            # Match review back to a customer record
            _match_review_to_customer(supabase, business["id"], reviewer_name)

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

        # Match review back to a customer record
        _match_review_to_customer(supabase, business["id"], reviewer_name)

        new_count += 1
        logger.info(
            "Draft %s created for review %s (business %s)",
            draft_id,
            google_review_id,
            business["id"],
        )

    return new_count


async def _process_business_places(
    business: dict[str, Any],
    http_client: Any,
) -> int:
    """Fetch reviews via Places API (API-key only) and notify the owner.

    No auto-reply — owner must reply manually on Google.
    We still generate AI draft suggestions they can copy.
    """
    supabase = get_supabase()
    place_id = business.get("google_place_id", "")
    if not place_id:
        return 0

    reviews = await get_reviews_by_place_id(place_id)
    review_link = business.get("google_review_link", "")
    new_count = 0

    for review in reviews:
        # Build a stable ID from author + rating + first 100 chars of text
        author = review.get("author_name", "A customer")
        text = review.get("text", "")
        rating = review.get("rating", 5)
        review_key = f"places_{place_id}_{author}_{rating}_{text[:100]}"

        # Skip already-processed
        existing = (
            supabase.table("review_drafts")
            .select("id")
            .eq("google_review_id", review_key)
            .execute()
        )
        if existing.data:
            continue

        # Generate AI draft reply (they can copy-paste it)
        draft_text = await generate_reply(
            business_name=business["business_name"],
            review_text=text,
            star_rating=rating,
        )

        # Save draft
        insert_result = (
            supabase.table("review_drafts")
            .insert({
                "business_id": business["id"],
                "google_review_id": review_key,
                "reviewer_name": author,
                "review_text": text,
                "star_rating": rating,
                "ai_draft_reply": draft_text,
                "status": "pending_approval",
                "sent_to_owner": True,
            })
            .execute()
        )
        draft_id = insert_result.data[0]["id"] if insert_result.data else "unknown"

        # Notify owner via WhatsApp
        owner_phone = business["phone_number"].lstrip("+")
        body = (
            f"⭐ New {rating}-star review from {author}:\n\n"
            f'"{text[:500]}"\n\n'
            f"💡 Suggested reply:\n"
            f'"{draft_text}"\n\n'
        )
        if review_link:
            body += f"👉 Reply on Google: {review_link}"

        await send_text_message(http_client, owner_phone, body)

        log_message(
            business_id=business["id"],
            to_phone=business["phone_number"],
            message_body=f"New {rating}-star review from {author} — suggested reply sent",
            message_type="draft_notification",
        )

        # Match review to customer record
        _match_review_to_customer(supabase, business["id"], author)

        new_count += 1
        logger.info(
            "Places API draft %s for review by '%s' (business %s)",
            draft_id, author, business["id"],
        )

    return new_count


# ── Follow-up reminders ───────────────────────────────────────────
@router.post("/send-follow-ups")
async def send_follow_ups(request: Request) -> dict[str, Any]:
    """Send follow-up reminders to customers who haven't posted a review yet.

    Intended to be called on the same cron schedule as poll-reviews.
    """
    supabase = get_supabase()
    http_client = request.app.state.http_client
    now = datetime.now(timezone.utc)

    businesses = (
        supabase.table("businesses")
        .select("*")
        .eq("subscription_status", "active")
        .eq("followup_enabled", 1)
        .execute()
    )

    total_sent = 0
    errors = 0

    for biz in businesses.data or []:
        interval_days = biz.get("followup_interval_days", 3)
        max_count = biz.get("followup_max_count", 2)
        message_tpl = biz.get("followup_message") or (
            "Hi {first_name}, just a quick reminder — we'd really appreciate "
            "your feedback! It only takes a minute. Thank you 😊"
        )
        review_link = biz.get("google_review_link", "")

        # Find customers eligible for a follow-up
        candidates = (
            supabase.table("customers")
            .select("id, name, phone_number, review_requested_at, followup_count, last_followup_at, whatsapp_opted_in")
            .eq("business_id", biz["id"])
            .neq("status", "review_posted")
            .execute()
        )

        for cust in candidates.data or []:
            followup_count = cust.get("followup_count", 0)
            if followup_count >= max_count:
                continue

            # Determine the reference date for when the next follow-up is due
            last_contact = cust.get("last_followup_at") or cust.get("review_requested_at")
            if not last_contact:
                continue

            try:
                last_dt = datetime.fromisoformat(last_contact.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            if (now - last_dt) < timedelta(days=interval_days):
                continue

            # Build the follow-up message
            first_name = (cust.get("name") or "there").split()[0]
            body = message_tpl.format(first_name=first_name)
            if review_link:
                body += f"\n\n{review_link}"

            # Content moderation check
            from app.services.moderation import moderate_outbound
            mod_warning = await moderate_outbound(body)
            if mod_warning:
                logger.warning("Follow-up blocked for biz %s: moderation flagged", biz["id"])
                continue

            phone = cust["phone_number"].lstrip("+")
            twilio_num = biz.get("twilio_number", "")
            opted_in = cust.get("whatsapp_opted_in", 0)
            try:
                if opted_in:
                    # Customer opted in — send via WhatsApp
                    await send_text_message(http_client, phone, body)
                elif twilio_num:
                    from app.services.sms_service import send_sms
                    await send_sms(http_client, cust["phone_number"], body, from_number=twilio_num)
                else:
                    await send_text_message(http_client, phone, body)
                supabase.table("customers").update({
                    "followup_count": followup_count + 1,
                    "last_followup_at": now.isoformat(),
                }).eq("id", cust["id"]).execute()

                log_message(
                    business_id=biz["id"],
                    to_phone=cust["phone_number"],
                    message_body=f"Follow-up #{followup_count + 1} sent",
                    message_type="review_followup",
                )
                total_sent += 1
            except Exception:
                errors += 1
                logger.exception(
                    "Failed to send follow-up to customer %s", cust["id"]
                )

    logger.info("Follow-ups complete — %d sent, %d errors", total_sent, errors)
    return {"followups_sent": total_sent, "errors": errors}
