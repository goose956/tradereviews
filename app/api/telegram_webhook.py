"""Telegram Bot webhook — handles inbound messages and callback queries.

This module mirrors the WhatsApp webhooks.py logic but operates over the
Telegram Bot API.  Business owners identified by their Telegram chat_id
(stored in businesses.telegram_chat_id) instead of phone number.

Onboarding:
  - A new user sends /start to the bot
  - The bot asks for their phone number (used to link to existing business)
    or walks them through signup if they're new
"""

import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response

from app.core.config import get_settings
from app.db.supabase import get_supabase
from app.services.telegram import (
    send_text,
    send_buttons,
    send_list,
    send_document,
    answer_callback_query,
    get_file,
    download_file,
)
from app.services.google import post_review_reply, refresh_access_token
from app.services.message_log import log_message
from app.services.parser import parse_review_command
from app.services.openai_service import extract_receipt_data, parse_booking_details
from app.services.email_service import (
    send_email,
    build_invoice_email,
    build_quote_email,
    build_review_email,
)
from app.services.sms_service import (
    send_sms,
    build_invoice_sms,
    build_quote_sms,
    build_review_sms,
)
from app.services.moderation import moderate_outbound
from app.core.security import decrypt

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook/telegram", tags=["telegram"])

# ──────────────────────────────────────────────
# In-memory state (mirrors whatsapp webhooks.py)
# ──────────────────────────────────────────────

_processed_update_ids: dict[int, float] = {}
_DEDUP_TTL = 300

_wizard_sessions: dict[str, dict[str, Any]] = {}  # chat_id str → session
_wizard_timeouts: dict[str, asyncio.Task] = {}
_SESSION_TIMEOUT = 600

# Pending link: user sent /link command — awaiting their phone number
_pending_link: dict[str, bool] = {}  # chat_id → True

_CHANNEL_LABELS = {
    "whatsapp": "📱 WhatsApp",
    "email": "📧 Email",
    "sms": "💬 SMS",
}


# ──────────────────────────────────────────────
# Deduplication
# ──────────────────────────────────────────────

def _is_duplicate(update_id: int) -> bool:
    now = datetime.now(timezone.utc).timestamp()
    stale = [k for k, v in _processed_update_ids.items() if now - v > _DEDUP_TTL]
    for k in stale:
        _processed_update_ids.pop(k, None)
    if update_id in _processed_update_ids:
        return True
    _processed_update_ids[update_id] = now
    return False


# ──────────────────────────────────────────────
# Wizard session management
# ──────────────────────────────────────────────

def _start_timeout(chat_id: str, client: httpx.AsyncClient) -> None:
    old = _wizard_timeouts.pop(chat_id, None)
    if old:
        old.cancel()

    async def _fire():
        await asyncio.sleep(_SESSION_TIMEOUT)
        session = _wizard_sessions.pop(chat_id, None)
        _wizard_timeouts.pop(chat_id, None)
        if session:
            try:
                await send_text(client, chat_id, "⏰ *Session ended* — no activity for 10 minutes.\n\nType /start to begin a new session.")
            except Exception:
                pass

    _wizard_timeouts[chat_id] = asyncio.create_task(_fire())


def _wizard_end_session(chat_id: str) -> None:
    _wizard_sessions.pop(chat_id, None)
    task = _wizard_timeouts.pop(chat_id, None)
    if task:
        task.cancel()


# ──────────────────────────────────────────────
# Business lookup (by telegram_chat_id)
# ──────────────────────────────────────────────

def _get_business_by_chat_id(chat_id: str) -> dict | None:
    supabase = get_supabase()
    result = (
        supabase.table("businesses")
        .select("*")
        .eq("telegram_chat_id", chat_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _get_business_by_phone(phone_e164: str) -> dict | None:
    supabase = get_supabase()
    result = (
        supabase.table("businesses")
        .select("*")
        .eq("phone_number", phone_e164)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


# ──────────────────────────────────────────────
# Phone normalisation
# ──────────────────────────────────────────────

def _normalise_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0"):
        digits = "44" + digits[1:]
    return f"+{digits}"


def _format_phone_display(e164: str) -> str:
    if e164.startswith("+44") and len(e164) == 13:
        local = "0" + e164[3:]
        return f"{local[:5]} {local[5:]}"
    return e164


def _signup_url(chat_id: str = "") -> str:
    settings = get_settings()
    base = (settings.telegram_signup_base_url or settings.base_url).rstrip("/")
    url = f"{base}/login.html?mode=signup"
    if chat_id:
        url += f"&tg={chat_id}"
    return url


# ──────────────────────────────────────────────
# Channel resolver (for outbound to customers)
# ──────────────────────────────────────────────

def _resolve_channel(channel: str, customer_phone: str, business_id: str) -> str:
    if channel == "email":
        return channel
    supabase = get_supabase()
    cust = (
        supabase.table("customers")
        .select("whatsapp_opted_in")
        .eq("business_id", business_id)
        .eq("phone_number", customer_phone)
        .limit(1)
        .execute()
    )
    if cust.data and cust.data[0].get("whatsapp_opted_in"):
        return "whatsapp"
    return "sms"


# ──────────────────────────────────────────────
# Action menu rows
# ──────────────────────────────────────────────

def _action_menu_rows(channel: str = "sms", existing_customer_only: bool = False) -> list[dict]:
    ch_label = _CHANNEL_LABELS.get(channel, "💬 SMS")
    if existing_customer_only:
        return [
            {"id": "wiz_review", "title": "⭐ Review Request"},
            {"id": "wiz_invoice", "title": "💷 Send Invoice"},
            {"id": "wiz_quote", "title": "📋 Send Quote"},
            {"id": "wiz_dashboard", "title": "💻 Open Dashboard"},
            {"id": "wiz_channel", "title": f"📲 Change Channel ({ch_label})"},
        ]
    return [
        {"id": "wiz_review", "title": "⭐ Review Request"},
        {"id": "wiz_invoice", "title": "💷 Send Invoice"},
        {"id": "wiz_quote", "title": "📋 Send Quote"},
        {"id": "wiz_expense", "title": "🧾 Record Expense"},
        {"id": "wiz_view_expenses", "title": "📊 View Expenses"},
        {"id": "wiz_booking", "title": "📅 New Booking"},
        {"id": "wiz_view_bookings", "title": "🗓 View Calendar"},
        {"id": "wiz_balance", "title": "💰 Account Balance"},
        {"id": "wiz_dashboard", "title": "💻 Open Dashboard"},
        {"id": "wiz_channel", "title": f"📲 Change Channel ({ch_label})"},
    ]


async def _show_action_menu(
    chat_id: str, session: dict, client: httpx.AsyncClient,
    prefix: str = "What would you like to do?",
) -> None:
    rows = _action_menu_rows(
        session.get("channel", "sms"),
        existing_customer_only=session.get("customer_source") == "existing",
    )
    await send_list(
        client, chat_id,
        prefix,
        "Choose an option",
        [{"title": "Actions", "rows": rows}],
    )


# ──────────────────────────────────────────────
# POST /webhook/telegram
# ──────────────────────────────────────────────

@router.get("/ping")
async def telegram_ping() -> dict:
    """Diagnostic endpoint — confirms the Telegram webhook handler is alive."""
    import os
    # Bypass lru_cache to read current env directly
    raw_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    raw_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    # Also clear the cache so the app picks up new values
    get_settings.cache_clear()
    settings = get_settings()
    return {
        "status": "ok",
        "bot_token_set": bool(settings.telegram_bot_token),
        "bot_token_env_set": bool(raw_token),
        "webhook_secret_set": bool(settings.telegram_webhook_secret),
        "active_sessions": list(_wizard_sessions.keys()),
        "pending_links": list(_pending_link.keys()),
    }


@router.post("", status_code=200)
async def receive_telegram_update(request: Request) -> dict[str, str]:
    """Receive a Telegram update (message or callback_query)."""
    # Verify secret token if configured
    settings = get_settings()
    if settings.telegram_webhook_secret:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != settings.telegram_webhook_secret:
            logger.warning("Telegram webhook secret mismatch — dropping update")
            return {"status": "ok"}  # return 200 to avoid Telegram retries

    body = await request.json()
    logger.info("Telegram update received: %s", json.dumps(body)[:500])
    client: httpx.AsyncClient = request.app.state.http_client

    update_id: int = body.get("update_id", 0)
    if update_id and _is_duplicate(update_id):
        logger.info("Duplicate update_id %s — skipping", update_id)
        return {"status": "ok"}

    try:
        if "callback_query" in body:
            await _handle_callback_query(body["callback_query"], client)
        elif "message" in body:
            await _handle_message(body["message"], client)
    except Exception:
        logger.exception("Error handling Telegram update %s", update_id)

    return {"status": "ok"}


# ──────────────────────────────────────────────
# Message dispatcher
# ──────────────────────────────────────────────

async def _handle_message(message: dict[str, Any], client: httpx.AsyncClient) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id:
        return

    text = message.get("text", "").strip()
    photo = message.get("photo")
    document = message.get("document")

    if photo:
        await _handle_photo(chat_id, photo, message.get("caption", ""), client)
        return

    if document:
        await send_text(client, chat_id, "📎 Document received. To record an expense, please send a *photo* of the receipt.")
        return

    if text:
        await _handle_text(chat_id, text, client)


async def _handle_text(chat_id: str, text: str, client: httpx.AsyncClient) -> None:
    lower = text.strip().lower()

    # ── /start always wins — escape any stuck state ──
    if lower.startswith("/start") or lower == "start":
        _pending_link.pop(chat_id, None)  # clear any stuck link flow
        await _handle_start(chat_id, text, client)
        return

    # ── Pending phone link ──
    if chat_id in _pending_link:
        await _handle_link_phone(chat_id, text.strip(), client)
        return

    if lower in ("/link", "link"):
        _pending_link[chat_id] = True
        await send_text(
            client, chat_id,
            "📱 Please type your *phone number* to link your GafferApp account.\n\n"
            "Use the same number you signed up with.\n"
            "Example: *07845 774563*",
        )
        return

    # ── Look up business ──
    business = _get_business_by_chat_id(chat_id)

    if not business:
        await send_text(
            client, chat_id,
            "👋 Welcome to *GafferApp*!\n\n"
            "Your Telegram isn't linked to an account yet.\n\n"
            "Type /link to connect your existing account, or visit "
            f"{_signup_url(chat_id)} to sign up.",
        )
        return

    status = business.get("subscription_status", "")

    if status == "inactive":
        settings = get_settings()
        checkout_url = f"{settings.base_url}/checkout.html?business_id={business['id']}"
        await send_text(
            client, chat_id,
            f"👋 Hey {business.get('business_name', 'there')}! "
            f"Your account isn't active yet.\n\n"
            f"Complete your setup here:\n👉 {checkout_url}\n\n"
            f"🔒 Cancel anytime · 💰 14-day money-back guarantee",
        )
        return

    await _handle_tradesperson_text(chat_id, text.strip(), business, client)


# ──────────────────────────────────────────────
# /start command
# ──────────────────────────────────────────────

async def _handle_start(chat_id: str, text: str, client: httpx.AsyncClient) -> None:
    business = _get_business_by_chat_id(chat_id)
    if business:
        await _wizard_start(chat_id, business, client)
        return

    settings = get_settings()
    await send_buttons(
        client, chat_id,
        "👋 Welcome to *GafferApp*!\n\n"
        "The easiest way for tradespeople to get more Google reviews, "
        "send professional invoices, and run your business — all from Telegram.\n\n"
        "Already have an account? Link it below. Or sign up to get started.",
        [
            {"id": "onboard_link", "title": "🔗 Link My Account"},
            {"id": "onboard_signup", "title": "🚀 Sign Up"},
        ],
    )


# ──────────────────────────────────────────────
# Account linking (phone number → telegram_chat_id)
# ──────────────────────────────────────────────

async def _handle_link_phone(chat_id: str, phone_raw: str, client: httpx.AsyncClient) -> None:
    """Link a Telegram chat_id to an existing business by phone number."""
    _pending_link.pop(chat_id, None)
    try:
        phone_e164 = _normalise_phone(phone_raw)
    except Exception:
        await send_text(client, chat_id, "⚠️ Couldn't read that number. Please try again with your phone number.")
        _pending_link[chat_id] = True
        return

    business = _get_business_by_phone(phone_e164)
    if not business:
        await send_text(
            client, chat_id,
            f"⚠️ No GafferApp account found for *{phone_e164}*.\n\n"
            f"Please check the number or visit {_signup_url(chat_id)} to sign up.",
        )
        return

    # Link the telegram_chat_id
    supabase = get_supabase()
    supabase.table("businesses").update(
        {"telegram_chat_id": chat_id}
    ).eq("id", business["id"]).execute()

    biz_name = business.get("business_name", "your business")
    await send_text(
        client, chat_id,
        f"✅ *Linked!* You're now connected to *{biz_name}*.\n\n"
        f"Type /start to begin.",
    )


# ──────────────────────────────────────────────
# Tradesperson text handler
# ──────────────────────────────────────────────

async def _handle_tradesperson_text(
    chat_id: str, text: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    upper = text.upper()
    session = _wizard_sessions.get(chat_id)

    if upper.startswith("/LOGIN") or upper.startswith("/DASHBOARD"):
        await _cmd_login(chat_id, business, client)
        return

    if upper.startswith("/HELP"):
        await send_text(
            client, chat_id,
            "📋 *How to use GafferApp*\n\n"
            "Type /start to begin a new session.\n"
            "The bot will walk you through sending reviews, invoices, or quotes step by step.\n\n"
            "Type /login to access your dashboard.",
        )
        return

    if upper.startswith("/START") or upper == "START":
        await _wizard_start(chat_id, business, client)
        return

    if session:
        await _wizard_handle_text(chat_id, text, session, business, client)
        return

    # Handle awaiting_edit drafts
    if not upper.startswith("/"):
        supabase = get_supabase()
        draft_result = (
            supabase.table("review_drafts")
            .select("id, google_review_id")
            .eq("business_id", business["id"])
            .eq("status", "awaiting_edit")
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        if draft_result.data:
            await _post_edited_reply(chat_id, text, business, draft_result.data[0], client)
            return

    await send_text(
        client, chat_id,
        "👋 Type /start to begin.\n\nI'll walk you through sending reviews, invoices, or quotes to your customers.",
    )


# ──────────────────────────────────────────────
# Callback query handler (button taps)
# ──────────────────────────────────────────────

async def _handle_callback_query(callback: dict[str, Any], client: httpx.AsyncClient) -> None:
    callback_id = callback.get("id", "")
    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
    payload = callback.get("data", "")

    # Acknowledge the button press
    await answer_callback_query(client, callback_id)

    if not chat_id or not payload:
        return

    # Onboarding buttons (before account link)
    if payload == "onboard_link":
        _pending_link[chat_id] = True
        await send_text(
            client, chat_id,
            "📱 Please type your *phone number* to link your GafferApp account.\n\n"
            "Example: *07845 774563*",
        )
        return

    if payload == "onboard_signup":
        await send_text(
            client, chat_id,
            f"🚀 Sign up here:\n👉 {_signup_url(chat_id)}\n\n"
            "Fill in the form and your Telegram will be linked automatically — no extra steps!",
        )
        return

    business = _get_business_by_chat_id(chat_id)
    if not business:
        await send_text(client, chat_id, "Please type /start to link your account first.")
        return

    await _handle_button(chat_id, payload, business, client)


# ──────────────────────────────────────────────
# Button router
# ──────────────────────────────────────────────

async def _handle_button(
    chat_id: str, payload: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    supabase = get_supabase()

    # Wizard buttons
    if payload.startswith("wiz_"):
        if await _wizard_handle_button(chat_id, payload, business, client):
            return

    # Confirm/cancel invoice
    if payload.startswith("sendinv_"):
        await _confirm_send_invoice(chat_id, payload.removeprefix("sendinv_"), business, client)
        return
    if payload.startswith("cancelinv_"):
        await _cancel_invoice(chat_id, payload.removeprefix("cancelinv_"), client)
        return

    # Confirm/cancel quote
    if payload.startswith("sendquo_"):
        await _confirm_send_quote(chat_id, payload.removeprefix("sendquo_"), business, client)
        return
    if payload.startswith("cancelquo_"):
        await _cancel_quote(chat_id, payload.removeprefix("cancelquo_"), client)
        return

    # Confirm/cancel booking
    if payload == "confirmbk":
        await _confirm_booking(chat_id, client)
        return
    if payload == "cancelbk":
        await _cancel_booking(chat_id, client)
        return

    # Review draft: approve
    if payload.startswith("approve_"):
        await _approve_draft(chat_id, payload.removeprefix("approve_"), business, client)
        return

    # Review draft: request edit
    if payload.startswith("edit_"):
        draft_id = payload.removeprefix("edit_")
        supabase.table("review_drafts").update({"status": "awaiting_edit"}).eq("id", draft_id).execute()
        await send_text(client, chat_id, "No problem. Just type your reply and I'll post it for you.")
        return

    # Review draft: reject
    if payload.startswith("reject_"):
        draft_id = payload.removeprefix("reject_")
        supabase.table("review_drafts").update({"status": "rejected"}).eq("id", draft_id).execute()
        await send_text(client, chat_id, "Draft rejected. No reply will be posted.")
        return

    logger.debug("Unknown button payload: %s from chat_id %s", payload, chat_id)


# ──────────────────────────────────────────────
# Wizard start
# ──────────────────────────────────────────────

async def _wizard_start(
    chat_id: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    _wizard_end_session(chat_id)
    _wizard_sessions[chat_id] = {
        "state": "choose_customer_type",
        "business_id": business["id"],
    }
    _start_timeout(chat_id, client)

    await send_buttons(
        client, chat_id,
        "👋 *Let's get started!*\n\nIs this for a new customer or an existing one?",
        [
            {"id": "wiz_new", "title": "➕ New Customer"},
            {"id": "wiz_existing", "title": "📋 Existing Customer"},
            {"id": "wiz_menu", "title": "📚 MENU"},
            {"id": "wiz_dashboard", "title": "💻 Open Dashboard"},
        ],
    )


# ──────────────────────────────────────────────
# Wizard text handler
# ──────────────────────────────────────────────

async def _wizard_handle_text(
    chat_id: str, text: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    state = session["state"]
    _start_timeout(chat_id, client)

    if text.upper() in ("CANCEL", "/CANCEL"):
        _wizard_end_session(chat_id)
        await send_text(client, chat_id, "❌ Session cancelled.\n\nType /start to begin again.")
        return

    if state == "awaiting_new_customer":
        await _wizard_new_customer_input(chat_id, text, session, business, client)
    elif state == "awaiting_customer_email":
        await _wizard_email_input(chat_id, text, session, business, client)
    elif state == "awaiting_invoice_details":
        await _wizard_invoice_input(chat_id, text, session, business, client)
    elif state == "awaiting_quote_details":
        await _wizard_quote_input(chat_id, text, session, business, client)
    elif state == "awaiting_booking_details":
        await _wizard_booking_input(chat_id, text, session, business, client)
    elif state == "awaiting_booking_name":
        await _wizard_booking_name_input(chat_id, text, session, business, client)
    elif state == "awaiting_receipt_photo":
        await send_text(
            client, chat_id,
            "📸 Please send a *photo* of the receipt — not text.\n\n"
            "Tap the 📎 icon to attach an image. Or type *CANCEL* to go back.",
        )
    else:
        await send_text(client, chat_id, "☝️ Please tap one of the buttons above to continue.\n\nOr type *CANCEL* to cancel.")


# ──────────────────────────────────────────────
# Wizard button handler
# ──────────────────────────────────────────────

async def _wizard_handle_button(
    chat_id: str, payload: str, business: dict[str, Any], client: httpx.AsyncClient
) -> bool:
    session = _wizard_sessions.get(chat_id)
    if not session:
        # Start a fresh session for this business
        session = {"state": "choose_customer_type", "business_id": business["id"]}
        _wizard_sessions[chat_id] = session

    _start_timeout(chat_id, client)
    supabase = get_supabase()

    def _has_customer_selected() -> bool:
        return bool(session.get("customer_phone"))

    def _requires_customer(action_payload: str) -> bool:
        return action_payload in {"wiz_review", "wiz_invoice", "wiz_quote", "wiz_channel"}

    if _requires_customer(payload) and not _has_customer_selected():
        session["state"] = "choose_customer_type"
        await send_buttons(
            client,
            chat_id,
            "👤 This option needs a customer first.\n\nChoose one to continue:",
            [
                {"id": "wiz_new", "title": "➕ New Customer"},
                {"id": "wiz_existing", "title": "📋 Existing Customer"},
                {"id": "wiz_dashboard", "title": "💻 Open Dashboard"},
            ],
        )
        return True

    if payload == "wiz_new":
        session["state"] = "awaiting_new_customer"
        await send_text(client, chat_id, "📝 Please type the customer's *name* and *phone number*.\n\nExample: *John Smith 07845774563*")
        return True

    if payload == "wiz_existing":
        await _wizard_show_existing_customers(chat_id, session, business, client)
        return True

    if payload == "wiz_menu":
        session["state"] = "choose_action"
        session.setdefault("channel", "sms")
        await _show_action_menu(
            chat_id,
            session,
            client,
            "📚 *Menu*\n\nTap an option below to continue.",
        )
        return True

    if payload.startswith("wiz_cust_"):
        customer_id = payload.removeprefix("wiz_cust_")
        await _wizard_customer_selected(chat_id, customer_id, business, client)
        return True

    if payload == "wiz_review":
        await _wizard_review_check_invoices(chat_id, session, business, client)
        return True

    if payload.startswith("wiz_rev_inv_"):
        invoice_id = payload.removeprefix("wiz_rev_inv_")
        items = supabase.table("line_items").select("description").eq("parent_id", invoice_id).eq("parent_type", "invoice").execute()
        job_desc = ", ".join(it["description"] for it in (items.data or []) if it.get("description"))
        session["review_job_description"] = job_desc or ""
        await _wizard_action_review(chat_id, business, client)
        return True

    if payload == "wiz_rev_skip":
        session["review_job_description"] = ""
        await _wizard_action_review(chat_id, business, client)
        return True

    if payload == "wiz_invoice":
        await _wizard_action_invoice(chat_id, session, client)
        return True

    if payload == "wiz_quote":
        await _wizard_action_quote(chat_id, session, client)
        return True

    if payload == "wiz_expense":
        session["state"] = "awaiting_receipt_photo"
        await send_text(client, chat_id, "📸 *Record an Expense*\n\nSend a photo of your receipt and I'll extract the details automatically.\n\nType *CANCEL* to go back.")
        return True

    if payload == "wiz_booking":
        session["state"] = "awaiting_booking_details"
        await send_text(
            client, chat_id,
            "📅 *New Booking*\n\n"
            "Type the job details — include a *date*, *time*, and *description*.\n\n"
            "Examples:\n"
            "• _Boiler service Tuesday 2pm_\n"
            "• _Kitchen fitting 15 April 9am 3 hours_\n\n"
            "Type *CANCEL* to go back.",
        )
        return True

    if payload == "wiz_balance":
        await _wizard_action_balance(chat_id, session, client)
        return True

    if payload == "wiz_view_expenses":
        await _wizard_view_expenses(chat_id, session, client)
        return True

    if payload == "wiz_view_bookings":
        await _wizard_view_bookings(chat_id, session, client)
        return True

    if payload == "wiz_dashboard":
        _wizard_end_session(chat_id)
        await _cmd_login(chat_id, business, client)
        return True

    if payload == "wiz_channel":
        session["state"] = "choose_channel"
        await send_list(
            client, chat_id,
            "📲 *Choose delivery channel for customers*",
            "Select Channel",
            [{"title": "Channels", "rows": [
                {"id": "wiz_ch_sms", "title": "💬 SMS", "description": "Text message (default)"},
                {"id": "wiz_ch_whatsapp", "title": "📱 WhatsApp", "description": "If customer opted in"},
                {"id": "wiz_ch_email", "title": "📧 Email", "description": "Send via email"},
            ]}],
        )
        return True

    if payload == "wiz_ch_sms":
        session["channel"] = "sms"
        session["state"] = "choose_action"
        await _show_action_menu(chat_id, session, client, "✅ Channel set to *💬 SMS*\n\nWhat would you like to do?")
        return True

    if payload == "wiz_ch_whatsapp":
        session["channel"] = "whatsapp"
        session["state"] = "choose_action"
        await _show_action_menu(chat_id, session, client, "✅ Channel set to *📱 WhatsApp*\n\nWhat would you like to do?")
        return True

    if payload == "wiz_ch_email":
        cust_phone = session.get("customer_phone", "")
        existing_email = ""
        if cust_phone:
            cust_result = (
                supabase.table("customers")
                .select("email")
                .eq("business_id", session["business_id"])
                .eq("phone_number", cust_phone)
                .limit(1)
                .execute()
            )
            if cust_result.data:
                existing_email = cust_result.data[0].get("email", "")
        if existing_email:
            session["channel"] = "email"
            session["customer_email"] = existing_email
            session["state"] = "choose_action"
            await _show_action_menu(chat_id, session, client, f"✅ Channel set to *📧 Email* ({existing_email})\n\nWhat would you like to do?")
        else:
            session["state"] = "awaiting_customer_email"
            await send_text(client, chat_id, "📧 No email on file. Please type the customer's *email address*:")
        return True

    return False


# ──────────────────────────────────────────────
# Customer input handlers
# ──────────────────────────────────────────────

async def _wizard_new_customer_input(
    chat_id: str, text: str, session: dict, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    parsed = parse_review_command(text)
    if not parsed:
        await send_text(client, chat_id, "⚠️ I couldn't read that.\n\nPlease type the customer's *name* and *phone number*.\nExample: *John Smith 07845774563*")
        return

    supabase = get_supabase()
    supabase.table("customers").upsert(
        {"business_id": business["id"], "phone_number": parsed.phone, "name": parsed.name, "status": "active"},
        on_conflict="business_id,phone_number",
    ).execute()
    supabase.table("businesses").update({"active_customer_phone": parsed.phone}).eq("id", business["id"]).execute()

    session["customer_phone"] = parsed.phone
    session["customer_name"] = parsed.name
    session["customer_source"] = "new"
    session["state"] = "choose_action"
    session.setdefault("channel", "sms")

    await _show_action_menu(chat_id, session, client, f"✅ *{parsed.name}* ({parsed.phone}) added!\n\nWhat would you like to do?")


async def _wizard_email_input(
    chat_id: str, text: str, session: dict, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    email = text.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        await send_text(client, chat_id, "⚠️ That doesn't look valid. Please type the customer's *email address*:")
        return
    supabase = get_supabase()
    cust_phone = session.get("customer_phone", "")
    supabase.table("customers").update({"email": email}).eq("business_id", business["id"]).eq("phone_number", cust_phone).execute()
    session["channel"] = "email"
    session["customer_email"] = email
    session["state"] = "choose_action"
    await _show_action_menu(chat_id, session, client, f"✅ Email saved! Channel set to *📧 Email* ({email})\n\nWhat would you like to do?")


async def _wizard_show_existing_customers(
    chat_id: str, session: dict, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    supabase = get_supabase()
    custs = (
        supabase.table("customers")
        .select("id, name, phone_number")
        .eq("business_id", business["id"])
        .order("created_at", desc=True)
        .execute()
    )
    if not custs.data:
        session["state"] = "awaiting_new_customer"
        await send_text(client, chat_id, "You don't have any customers yet.\n\nPlease type the customer's *name* and *phone number*.\nExample: *John Smith 07845774563*")
        return

    session["state"] = "awaiting_customer_pick"
    rows = [{"id": f"wiz_cust_{c['id']}", "title": c["name"][:24], "description": c["phone_number"]} for c in custs.data[:10]]

    if len(rows) <= 3:
        await send_buttons(client, chat_id, "Which customer?", [{"id": r["id"], "title": r["title"]} for r in rows])
    else:
        await send_list(client, chat_id, "Which customer is this for?", "Select Customer", [{"title": "Customers", "rows": rows}])


async def _wizard_customer_selected(
    chat_id: str, customer_id: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    session = _wizard_sessions.get(chat_id)
    if not session:
        return

    supabase = get_supabase()
    cust_result = supabase.table("customers").select("id, name, phone_number").eq("id", customer_id).limit(1).execute()
    if not cust_result.data:
        await send_text(client, chat_id, "⚠️ Customer not found.")
        return

    cust = cust_result.data[0]
    supabase.table("businesses").update({"active_customer_phone": cust["phone_number"]}).eq("id", business["id"]).execute()

    session["customer_phone"] = cust["phone_number"]
    session["customer_name"] = cust["name"]
    session["customer_source"] = "existing"
    session["state"] = "choose_action"
    session.setdefault("channel", "sms")

    await _show_action_menu(chat_id, session, client, f"✅ Selected *{cust['name']}*\n\nWhat would you like to do?")


# ──────────────────────────────────────────────
# Review flow
# ──────────────────────────────────────────────

async def _wizard_review_check_invoices(
    chat_id: str, session: dict, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    supabase = get_supabase()
    customer_phone = session.get("customer_phone", "")
    customer_name = session.get("customer_name", "Customer")

    cust = supabase.table("customers").select("id").eq("business_id", business["id"]).eq("phone_number", customer_phone).limit(1).execute()
    cust_id = cust.data[0]["id"] if cust.data else None

    if cust_id:
        invoices = supabase.table("invoices").select("id, invoice_number, total, created_at").eq("business_id", business["id"]).eq("customer_id", cust_id).execute()
    else:
        invoices = type("R", (), {"data": []})()

    if invoices.data:
        rows = []
        for inv in invoices.data[:9]:
            items = supabase.table("line_items").select("description").eq("parent_id", inv["id"]).eq("parent_type", "invoice").execute()
            desc = ", ".join(it["description"] for it in (items.data or []) if it.get("description"))[:70] or "Invoice"
            date_str = inv["created_at"][:10] if inv.get("created_at") else ""
            total = inv.get("total", 0) or 0
            rows.append({"id": f"wiz_rev_inv_{inv['id']}", "title": f"#{inv['invoice_number']} — £{total:,.2f}", "description": f"{desc} ({date_str})"})
        rows.append({"id": "wiz_rev_skip", "title": "Skip — send generic"})
        await send_list(
            client, chat_id,
            f"📋 *{customer_name}* has {len(invoices.data)} invoice(s).\n\nPick one to personalise the review request, or skip.",
            "Select Invoice",
            [{"title": "Invoices", "rows": rows}],
        )
    else:
        session["review_job_description"] = ""
        await _wizard_action_review(chat_id, business, client)


async def _wizard_action_review(
    chat_id: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    session = _wizard_sessions.get(chat_id)
    if not session:
        return

    customer_phone = session.get("customer_phone", "")
    customer_name = session.get("customer_name", "Customer")
    channel = session.get("channel", "sms")
    customer_email = session.get("customer_email", "")
    job_desc = session.get("review_job_description", "")

    supabase = get_supabase()
    supabase.table("customers").upsert(
        {"business_id": business["id"], "phone_number": customer_phone, "name": customer_name, "status": "request_sent", "review_requested_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="business_id,phone_number",
    ).execute()

    first_name = customer_name.split()[0]
    biz_name = business["business_name"]
    review_link = business.get("google_review_link", "")
    job_snippet = f" for the {job_desc}" if job_desc else ""
    job_thanks = f"We hope you're happy with the {job_desc} we completed for you. " if job_desc else ""

    channel = _resolve_channel(channel, customer_phone, business["id"])

    mod_warning = await moderate_outbound(f"{biz_name} {job_desc}")
    if mod_warning:
        await send_text(client, chat_id, mod_warning)
        return

    sent_via = ""
    try:
        if channel == "email" and customer_email:
            subject, html_body, plain_body = build_review_email(
                customer_name=customer_name, first_name=first_name, biz_name=biz_name,
                review_link=review_link or "https://g.page/review", job_description=job_desc,
            )
            await send_email(client, customer_email, subject, html_body, plain_body)
            sent_via = f"via email to {customer_email}"
        elif channel == "sms":
            sms_body = build_review_sms(
                first_name=first_name, biz_name=biz_name,
                review_link=review_link or "https://g.page/review", job_description=job_desc,
            )
            await send_sms(client, customer_phone, sms_body)
            sent_via = f"via SMS to {customer_phone}"
        else:
            # WhatsApp to customer
            from app.services.whatsapp import send_interactive_buttons as wa_buttons, send_template_message as wa_template
            customer_raw = customer_phone.lstrip("+")
            try:
                await wa_template(client, to_phone=customer_raw, customer_name=first_name, business_name=biz_name)
            except Exception:
                await wa_buttons(
                    client, customer_raw,
                    f"Hi {first_name}, thanks for choosing {biz_name}{job_snippet}! {job_thanks}How was your experience?",
                    [{"id": "review_great", "title": "Great! ⭐"}, {"id": "could_be_better", "title": "Could be better"}],
                )
            sent_via = f"via WhatsApp to {customer_phone}"
    except Exception:
        logger.exception("Failed to send review request to %s", customer_phone)
        await send_text(client, chat_id, f"⚠️ Failed to send review request to {customer_name}.\n\nType /start to try again.")
        _wizard_end_session(chat_id)
        return

    log_message(business_id=business["id"], to_phone=customer_phone, message_body=f"Review request sent to {customer_name} {sent_via}", message_type="review_request")
    _wizard_end_session(chat_id)
    await send_text(client, chat_id, f"✅ Review request sent to *{customer_name}* ({sent_via})!\n\nSession ended. Type /start for a new session.")


# ──────────────────────────────────────────────
# Invoice flow
# ──────────────────────────────────────────────

def _parse_invoice_args(args: str) -> tuple[float | None, str]:
    args = args.strip()
    if not args:
        return None, ""
    m = re.search(r"[£$]?\s*([\d,]+(?:\.\d{1,2})?)\b", args)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            return None, args
        desc = (args[:m.start()] + " " + args[m.end():]).strip()
        desc = re.sub(r"\s{2,}", " ", desc)
        desc = re.sub(r"^[£$]\s*", "", desc).strip()
        return amount, desc
    return None, args


async def _wizard_action_invoice(chat_id: str, session: dict, client: httpx.AsyncClient) -> None:
    session["state"] = "awaiting_invoice_details"
    _start_timeout(chat_id, client)
    await send_text(client, chat_id, "📝 *Invoice*\n\nPlease type the *job description* and *total cost*.\n\nExample: *Boiler repair 250*")


async def _wizard_action_quote(chat_id: str, session: dict, client: httpx.AsyncClient) -> None:
    session["state"] = "awaiting_quote_details"
    _start_timeout(chat_id, client)
    await send_text(client, chat_id, "📝 *Quote*\n\nPlease type the *job description* and *estimated cost*.\n\nExample: *Full bathroom refit 2500*")


async def _wizard_invoice_input(
    chat_id: str, text: str, session: dict, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    amount, description = _parse_invoice_args(text)
    if amount is None and session.get("pending_amount") and text.strip():
        description = text.strip()
        amount = session.pop("pending_amount")

    if amount is None:
        await send_text(client, chat_id, "⚠️ I couldn't find an amount.\n\nPlease include the *cost* and *description*.\nExample: *Boiler repair 250*\n\nType *CANCEL* to cancel.")
        return

    if not description:
        session["pending_amount"] = amount
        await send_text(client, chat_id, f"✅ Amount: £{amount:.2f}\n\nNow type a short *description* of the job.\nExample: *Boiler repair*")
        return

    business["confirm_before_send"] = True
    session["state"] = "confirm_invoice"
    await _finalise_invoice(chat_id, amount, description, business, client)


async def _wizard_quote_input(
    chat_id: str, text: str, session: dict, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    amount, description = _parse_invoice_args(text)
    if amount is None and session.get("pending_amount") and text.strip():
        description = text.strip()
        amount = session.pop("pending_amount")

    if amount is None:
        await send_text(client, chat_id, "⚠️ I couldn't find an amount.\n\nPlease include the *estimated cost* and *description*.\nExample: *Full bathroom refit 2500*\n\nType *CANCEL* to cancel.")
        return

    if not description:
        session["pending_amount"] = amount
        await send_text(client, chat_id, f"✅ Amount: £{amount:.2f}\n\nNow type a short *description* of the work.\nExample: *Full bathroom refit*")
        return

    business["confirm_before_send"] = True
    session["state"] = "confirm_quote"
    await _finalise_quote(chat_id, amount, description, business, client)


async def _finalise_invoice(
    chat_id: str, amount: float, description: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    from app.api.member import _next_number, _get_business_tax_rate

    supabase = get_supabase()
    customer_phone = business.get("active_customer_phone", "")
    cust_result = supabase.table("customers").select("id, name, phone_number").eq("business_id", business["id"]).eq("phone_number", customer_phone).limit(1).execute()
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_id = customer["id"] if customer else ""
    first_name = customer_name.split()[0]

    tax_rate = _get_business_tax_rate(supabase, business["id"])
    inv_number = _next_number(supabase, business["id"], "invoices", "INV")
    subtotal = round(amount, 2)
    tax_amount = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)
    currency = business.get("currency", "GBP")
    sym = "£" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "

    inv_result = supabase.table("invoices").insert({
        "business_id": business["id"], "customer_id": customer_id,
        "invoice_number": inv_number, "status": "draft",
        "subtotal": subtotal, "tax_rate": tax_rate, "tax_amount": tax_amount, "total": total,
        "currency": currency, "payment_terms": business.get("default_payment_terms", "Payment due within 14 days"), "notes": description,
    }).execute()
    inv = inv_result.data[0]

    supabase.table("line_items").insert({
        "parent_id": inv["id"], "parent_type": "invoice",
        "description": description, "quantity": 1, "unit_price": subtotal, "total": subtotal, "sort_order": 0,
    }).execute()

    settings = get_settings()
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/invoices/{inv['id']}/pdf"
    session = _wizard_sessions.get(chat_id, {})
    ch_label = _CHANNEL_LABELS.get(session.get("channel", "sms"), "💬 SMS")

    preview_msg = (
        f"🔍 *Invoice Preview — {inv_number}*\n"
        f"To: {customer_name} ({customer_phone})\n"
        f"📲 Sending via: {ch_label}\n\n"
        f"• {description}\n"
        f"• Subtotal: {sym}{subtotal:.2f}\n"
        f"• VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
        f"• *Total: {sym}{total:.2f}*\n\n"
        f"📎 PDF: {pdf_url}\n\n"
        f"Tap *Send* to deliver it to {first_name}."
    )
    await send_buttons(client, chat_id, preview_msg, [
        {"id": f"sendinv_{inv['id']}", "title": "✅ Send"},
        {"id": f"cancelinv_{inv['id']}", "title": "❌ Cancel"},
    ])


async def _finalise_quote(
    chat_id: str, amount: float, description: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    from app.api.member import _next_number, _get_business_tax_rate

    supabase = get_supabase()
    customer_phone = business.get("active_customer_phone", "")
    cust_result = supabase.table("customers").select("id, name, phone_number").eq("business_id", business["id"]).eq("phone_number", customer_phone).limit(1).execute()
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_id = customer["id"] if customer else ""
    first_name = customer_name.split()[0]

    tax_rate = _get_business_tax_rate(supabase, business["id"])
    quo_number = _next_number(supabase, business["id"], "quotes", "QUO")
    subtotal = round(amount, 2)
    tax_amount = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)
    currency = business.get("currency", "GBP")
    sym = "£" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "
    valid_until = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")

    quo_result = supabase.table("quotes").insert({
        "business_id": business["id"], "customer_id": customer_id,
        "quote_number": quo_number, "status": "draft",
        "subtotal": subtotal, "tax_rate": tax_rate, "tax_amount": tax_amount, "total": total,
        "currency": currency, "valid_until": valid_until, "notes": description,
    }).execute()
    quo = quo_result.data[0]

    supabase.table("line_items").insert({
        "parent_id": quo["id"], "parent_type": "quote",
        "description": description, "quantity": 1, "unit_price": subtotal, "total": subtotal, "sort_order": 0,
    }).execute()

    settings = get_settings()
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/quotes/{quo['id']}/pdf"
    session = _wizard_sessions.get(chat_id, {})
    ch_label = _CHANNEL_LABELS.get(session.get("channel", "sms"), "💬 SMS")

    preview_msg = (
        f"🔍 *Quote Preview — {quo_number}*\n"
        f"To: {customer_name} ({customer_phone})\n"
        f"📲 Sending via: {ch_label}\n\n"
        f"• {description}\n"
        f"• Subtotal: {sym}{subtotal:.2f}\n"
        f"• VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
        f"• *Total: {sym}{total:.2f}*\n"
        f"• Valid until: {valid_until}\n\n"
        f"📎 PDF: {pdf_url}\n\n"
        f"Tap *Send* to deliver it to {first_name}."
    )
    await send_buttons(client, chat_id, preview_msg, [
        {"id": f"sendquo_{quo['id']}", "title": "✅ Send"},
        {"id": f"cancelquo_{quo['id']}", "title": "❌ Cancel"},
    ])


async def _confirm_send_invoice(
    chat_id: str, inv_id: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    supabase = get_supabase()
    session = _wizard_sessions.get(chat_id, {})
    channel = session.get("channel", "sms")
    customer_email = session.get("customer_email", "")

    inv_result = supabase.table("invoices").select("*").eq("id", inv_id).execute()
    if not inv_result.data:
        await send_text(client, chat_id, "Invoice not found.")
        return
    inv = inv_result.data[0]

    cust_result = supabase.table("customers").select("id, name, phone_number, email").eq("id", inv["customer_id"]).execute()
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_phone = customer["phone_number"] if customer else ""
    first_name = customer_name.split()[0]
    if not customer_email and customer:
        customer_email = customer.get("email", "")

    currency = inv.get("currency", "GBP")
    sym = "£" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "

    await _send_invoice_to_customer(
        chat_id, inv, invoice_number=inv["invoice_number"], description=inv.get("notes", ""),
        subtotal=inv["subtotal"], tax_rate=inv["tax_rate"], tax_amount=inv["tax_amount"], total=inv["total"],
        sym=sym, customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=business.get("business_name", ""),
        business=business, client=client, channel=channel, customer_email=customer_email,
    )


async def _send_invoice_to_customer(
    chat_id: str, inv: dict, *, invoice_number: str, description: str,
    subtotal: float, tax_rate: float, tax_amount: float, total: float, sym: str,
    customer_name: str, customer_phone: str, first_name: str, biz_name: str,
    business: dict[str, Any], client: httpx.AsyncClient,
    channel: str = "sms", customer_email: str = "",
) -> None:
    supabase = get_supabase()
    settings = get_settings()
    personal_phone = _format_phone_display(business.get("phone_number", ""))
    customer_raw = customer_phone.lstrip("+")
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/invoices/{inv['id']}/pdf"
    ch_label = _CHANNEL_LABELS.get(channel, "WhatsApp")
    channel = _resolve_channel(channel, customer_phone, business["id"])

    mod_warning = await moderate_outbound(f"{description} {biz_name}")
    if mod_warning:
        await send_text(client, chat_id, mod_warning)
        return

    sent_via = ""
    try:
        if channel == "email" and customer_email:
            subject, html_body, plain_body = build_invoice_email(
                customer_name=customer_name, first_name=first_name, biz_name=biz_name,
                invoice_number=invoice_number, description=description, subtotal=subtotal,
                tax_rate=tax_rate, tax_amount=tax_amount, total=total, sym=sym,
                pdf_url=pdf_url, personal_phone=personal_phone,
            )
            await send_email(client, customer_email, subject, html_body, plain_body)
            sent_via = f"via email to {customer_email}"
        elif channel == "sms":
            sms_body = build_invoice_sms(
                first_name=first_name, biz_name=biz_name, invoice_number=invoice_number,
                description=description, total=total, sym=sym, pdf_url=pdf_url, personal_phone=personal_phone,
            )
            await send_sms(client, customer_phone, sms_body)
            sent_via = f"via SMS to {customer_phone}"
        else:
            from app.services.whatsapp import send_text_message as wa_text
            invoice_msg = (
                f"Hi {first_name}, here is your invoice from {biz_name}:\n\n"
                f"📄 *Invoice {invoice_number}*\n• {description}\n"
                f"• Subtotal: {sym}{subtotal:.2f}\n• VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
                f"• *Total: {sym}{total:.2f}*\n\n📎 Download PDF: {pdf_url}\n\n"
                f"To discuss, contact {biz_name} on *{personal_phone}*"
            )
            await wa_text(client, customer_raw, invoice_msg)
            sent_via = f"via WhatsApp to {customer_phone}"
    except Exception:
        logger.exception("Failed to send invoice to %s", customer_phone)
        await send_text(client, chat_id, f"✅ Invoice {invoice_number} created but failed to send to {customer_name}. You can send the PDF from your dashboard.")
        return

    supabase.table("invoices").update({"status": "sent", "sent_at": datetime.now(timezone.utc).isoformat()}).eq("id", inv["id"]).execute()
    log_message(business_id=business["id"], to_phone=customer_phone, message_body=f"Invoice {invoice_number} sent {sent_via}", message_type="invoice")

    _wizard_end_session(chat_id)
    await send_text(client, chat_id, f"✅ Invoice {invoice_number} sent to *{customer_name}* ({sent_via})!\n\n• {description}: {sym}{total:.2f} (inc. VAT)\n\nSession ended. Type /start for a new session.")


async def _cancel_invoice(chat_id: str, inv_id: str, client: httpx.AsyncClient) -> None:
    supabase = get_supabase()
    inv_result = supabase.table("invoices").select("invoice_number").eq("id", inv_id).execute()
    inv_number = inv_result.data[0]["invoice_number"] if inv_result.data else "?"
    supabase.table("line_items").delete().eq("parent_id", inv_id).eq("parent_type", "invoice").execute()
    supabase.table("invoices").delete().eq("id", inv_id).execute()
    _wizard_end_session(chat_id)
    await send_text(client, chat_id, f"❌ Invoice {inv_number} cancelled.\n\nType /start for a new session.")


async def _confirm_send_quote(
    chat_id: str, quo_id: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    supabase = get_supabase()
    session = _wizard_sessions.get(chat_id, {})
    channel = session.get("channel", "sms")
    customer_email = session.get("customer_email", "")

    quo_result = supabase.table("quotes").select("*").eq("id", quo_id).execute()
    if not quo_result.data:
        await send_text(client, chat_id, "Quote not found.")
        return
    quo = quo_result.data[0]

    cust_result = supabase.table("customers").select("id, name, phone_number, email").eq("id", quo["customer_id"]).execute()
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_phone = customer["phone_number"] if customer else ""
    first_name = customer_name.split()[0]
    if not customer_email and customer:
        customer_email = customer.get("email", "")

    currency = quo.get("currency", "GBP")
    sym = "£" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "

    await _send_quote_to_customer(
        chat_id, quo, quote_number=quo["quote_number"], description=quo.get("notes", ""),
        subtotal=quo["subtotal"], tax_rate=quo["tax_rate"], tax_amount=quo["tax_amount"], total=quo["total"],
        sym=sym, valid_until=quo.get("valid_until", ""),
        customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=business.get("business_name", ""),
        business=business, client=client, channel=channel, customer_email=customer_email,
    )


async def _send_quote_to_customer(
    chat_id: str, quo: dict, *, quote_number: str, description: str,
    subtotal: float, tax_rate: float, tax_amount: float, total: float, sym: str,
    valid_until: str, customer_name: str, customer_phone: str, first_name: str, biz_name: str,
    business: dict[str, Any], client: httpx.AsyncClient,
    channel: str = "sms", customer_email: str = "",
) -> None:
    supabase = get_supabase()
    settings = get_settings()
    personal_phone = _format_phone_display(business.get("phone_number", ""))
    customer_raw = customer_phone.lstrip("+")
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/quotes/{quo['id']}/pdf"
    channel = _resolve_channel(channel, customer_phone, business["id"])

    mod_warning = await moderate_outbound(f"{description} {biz_name}")
    if mod_warning:
        await send_text(client, chat_id, mod_warning)
        return

    sent_via = ""
    try:
        if channel == "email" and customer_email:
            subject, html_body, plain_body = build_quote_email(
                customer_name=customer_name, first_name=first_name, biz_name=biz_name,
                quote_number=quote_number, description=description, subtotal=subtotal,
                tax_rate=tax_rate, tax_amount=tax_amount, total=total, sym=sym,
                valid_until=valid_until, pdf_url=pdf_url, personal_phone=personal_phone,
            )
            await send_email(client, customer_email, subject, html_body, plain_body)
            sent_via = f"via email to {customer_email}"
        elif channel == "sms":
            sms_body = build_quote_sms(
                first_name=first_name, biz_name=biz_name, quote_number=quote_number,
                description=description, total=total, sym=sym, valid_until=valid_until,
                pdf_url=pdf_url, personal_phone=personal_phone,
            )
            await send_sms(client, customer_phone, sms_body)
            sent_via = f"via SMS to {customer_phone}"
        else:
            from app.services.whatsapp import send_text_message as wa_text
            quote_msg = (
                f"Hi {first_name}, here is a quote from {biz_name}:\n\n"
                f"📄 *Quote {quote_number}*\n• {description}\n"
                f"• Subtotal: {sym}{subtotal:.2f}\n• VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
                f"• *Total: {sym}{total:.2f}*\n• Valid until: {valid_until}\n\n"
                f"📎 Download PDF: {pdf_url}\n\n"
                f"To discuss, contact {biz_name} on *{personal_phone}*"
            )
            await wa_text(client, customer_raw, quote_msg)
            sent_via = f"via WhatsApp to {customer_phone}"
    except Exception:
        logger.exception("Failed to send quote to %s", customer_phone)
        await send_text(client, chat_id, f"✅ Quote {quote_number} created but failed to send to {customer_name}. You can send the PDF from your dashboard.")
        return

    supabase.table("quotes").update({"status": "sent", "sent_at": datetime.now(timezone.utc).isoformat()}).eq("id", quo["id"]).execute()
    log_message(business_id=business["id"], to_phone=customer_phone, message_body=f"Quote {quote_number} sent {sent_via}", message_type="quote")

    _wizard_end_session(chat_id)
    await send_text(client, chat_id, f"✅ Quote {quote_number} sent to *{customer_name}* ({sent_via})!\n\n• {description}: {sym}{total:.2f} (inc. VAT)\n• Valid until {valid_until}\n\nSession ended. Type /start for a new session.")


async def _cancel_quote(chat_id: str, quo_id: str, client: httpx.AsyncClient) -> None:
    supabase = get_supabase()
    quo_result = supabase.table("quotes").select("quote_number").eq("id", quo_id).execute()
    quo_number = quo_result.data[0]["quote_number"] if quo_result.data else "?"
    supabase.table("line_items").delete().eq("parent_id", quo_id).eq("parent_type", "quote").execute()
    supabase.table("quotes").delete().eq("id", quo_id).execute()
    _wizard_end_session(chat_id)
    await send_text(client, chat_id, f"❌ Quote {quo_number} cancelled.\n\nType /start for a new session.")


# ──────────────────────────────────────────────
# Balance / Expenses / Bookings views
# ──────────────────────────────────────────────

async def _wizard_action_balance(chat_id: str, session: dict, client: httpx.AsyncClient) -> None:
    supabase = get_supabase()
    biz_id = session["business_id"]
    invoices = supabase.table("invoices").select("total, status, paid_at").eq("business_id", biz_id).execute()

    total_invoiced = total_paid = outstanding = 0.0
    num_invoices = num_paid = num_outstanding = 0
    for inv in (invoices.data or []):
        amount = inv.get("total", 0) or 0
        total_invoiced += amount
        num_invoices += 1
        if inv.get("paid_at") or inv.get("status") == "paid":
            total_paid += amount
            num_paid += 1
        else:
            outstanding += amount
            num_outstanding += 1

    quotes = supabase.table("quotes").select("total").eq("business_id", biz_id).execute()
    num_quotes = len(quotes.data or [])
    quotes_total = sum((q.get("total", 0) or 0) for q in (quotes.data or []))

    msg = (
        f"💰 *Account Balance*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📋 *Invoices:* {num_invoices}\n"
        f"💷 *Total Invoiced:* £{total_invoiced:,.2f}\n"
        f"✅ *Paid:* £{total_paid:,.2f} ({num_paid})\n"
        f"⏳ *Outstanding:* £{outstanding:,.2f} ({num_outstanding})\n\n"
        f"📝 *Quotes Sent:* {num_quotes} (£{quotes_total:,.2f})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"What would you like to do next?"
    )
    session["state"] = "choose_action"
    await _show_action_menu(chat_id, session, client, msg)


async def _wizard_view_expenses(chat_id: str, session: dict, client: httpx.AsyncClient) -> None:
    supabase = get_supabase()
    expenses = (supabase.table("expenses").select("*").eq("business_id", session["business_id"]).order("date", desc=True).execute()).data or []

    if not expenses:
        await send_text(client, chat_id, "📊 *Expenses*\n\nNo expenses recorded yet.\nSend a receipt photo after choosing 🧾 Record Expense.")
    else:
        total = sum(float(e.get("total", 0)) for e in expenses)
        tax = sum(float(e.get("tax_amount", 0)) for e in expenses)
        now = datetime.now(timezone.utc)
        month_key = now.strftime("%Y-%m")
        month_total = sum(float(e.get("total", 0)) for e in expenses if (e.get("date", "") or "")[:7] == month_key)
        lines = [f"  • {e.get('date', '?')} — {e.get('vendor', '?')} — £{float(e.get('total', 0)):.2f}" for e in expenses[:5]]
        await send_text(
            client, chat_id,
            f"📊 *Expense Summary*\n\n"
            f"💰 *Total Spent:* £{total:.2f}\n"
            f"🧾 *VAT (Reclaimable):* £{tax:.2f}\n"
            f"📅 *This Month:* £{month_total:.2f}\n"
            f"📋 *Receipts:* {len(expenses)}\n\n"
            f"📝 *Recent:*\n" + "\n".join(lines),
        )

    session["state"] = "choose_action"
    await _show_action_menu(chat_id, session, client, "What would you like to do next?")


async def _wizard_view_bookings(chat_id: str, session: dict, client: httpx.AsyncClient) -> None:
    supabase = get_supabase()
    bookings = (supabase.table("bookings").select("*").eq("business_id", session["business_id"]).order("date").execute()).data or []

    if not bookings:
        await send_text(client, chat_id, "🗓 *Your Calendar*\n\nNo bookings yet.\nTap 📅 New Booking to add your first job.")
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        upcoming = [b for b in bookings if (b.get("date", "") or "") >= today and b.get("status") != "cancelled"]
        today_bks = [b for b in upcoming if b.get("date", "") == today]
        lines = []
        for b in upcoming[:7]:
            try:
                from datetime import datetime as _dt
                d = _dt.strptime(b["date"], "%Y-%m-%d")
                day_str = d.strftime("%a %d %b")
            except Exception:
                day_str = b.get("date", "?")
            lines.append(f"  • {day_str} {b.get('time', '')} — {b.get('title', '?')}" + (f" ({b.get('customer_name', '')})" if b.get("customer_name") else ""))

        await send_text(
            client, chat_id,
            f"🗓 *Your Calendar*\n\n"
            f"📋 *Total Bookings:* {len(bookings)}\n"
            f"📅 *Today:* {len(today_bks)} job{'s' if len(today_bks) != 1 else ''}\n"
            f"⏳ *Upcoming:* {len(upcoming)}\n\n"
            f"📝 *Next Up:*\n" + ("\n".join(lines) if lines else "  None"),
        )

    session["state"] = "choose_action"
    await _show_action_menu(chat_id, session, client, "What would you like to do next?")


# ──────────────────────────────────────────────
# Booking wizard
# ──────────────────────────────────────────────

async def _wizard_booking_input(
    chat_id: str, text: str, session: dict, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    await send_text(client, chat_id, "📅 *Got it!* Parsing your booking...")
    try:
        details = await parse_booking_details(text)
    except Exception:
        logger.exception("Failed to parse booking from %s", chat_id)
        await send_text(client, chat_id, "⚠️ Sorry, I couldn't understand that. Please try again.\n\nExample: *Boiler service Tuesday 2pm*\n\nType *CANCEL* to go back.")
        return

    title = details.get("title", text[:60])
    date = details.get("date", "")
    time_ = details.get("time", "09:00")
    duration = int(details.get("duration_mins", 60))
    notes = details.get("notes", "")
    parsed_name = details.get("customer_name", "").strip()
    customer_name = session.get("customer_name", "") or parsed_name

    if not customer_name:
        session["pending_booking_partial"] = {"title": title, "date": date, "time": time_, "duration_mins": duration, "notes": notes}
        session["state"] = "awaiting_booking_name"
        await send_text(client, chat_id, "👤 *Who is this booking for?*\n\nPlease type the customer's name.\n\nType *SKIP* to save without a name.")
        return

    await _show_booking_preview(chat_id, session, title, date, time_, duration, notes, customer_name, client)


async def _wizard_booking_name_input(
    chat_id: str, text: str, session: dict, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    partial = session.get("pending_booking_partial")
    if not partial:
        session["state"] = "awaiting_booking_details"
        await send_text(client, chat_id, "⚠️ Something went wrong. Please enter the booking details again.")
        return

    customer_name = "" if text.strip().upper() == "SKIP" else text.strip()
    session.pop("pending_booking_partial", None)
    await _show_booking_preview(chat_id, session, partial["title"], partial["date"], partial["time"], partial["duration_mins"], partial["notes"], customer_name, client)


async def _show_booking_preview(
    chat_id: str, session: dict, title: str, date: str, time_: str,
    duration: int, notes: str, customer_name: str, client: httpx.AsyncClient,
) -> None:
    # Clash check
    clash_warning = ""
    if date and time_:
        try:
            from datetime import datetime as _dt, timedelta as _td
            supabase = get_supabase()
            new_start = _dt.strptime(f"{date} {time_}", "%Y-%m-%d %H:%M")
            new_end = new_start + _td(minutes=duration)
            existing = supabase.table("bookings").select("title, date, time, duration_mins").eq("business_id", session["business_id"]).eq("date", date).neq("status", "cancelled").execute()
            for bk in (existing.data or []):
                bk_start = _dt.strptime(f"{bk['date']} {bk['time']}", "%Y-%m-%d %H:%M")
                bk_end = bk_start + _td(minutes=int(bk.get("duration_mins", 60)))
                if new_start < bk_end and bk_start < new_end:
                    clash_warning = f"\n\n⚠️ *Clash!* Overlaps with:\n📋 {bk['title']} — {bk['time']} ({bk.get('duration_mins', 60)} mins)"
                    break
        except Exception:
            pass

    try:
        from datetime import datetime as _dt
        friendly_date = _dt.strptime(date, "%Y-%m-%d").strftime("%A %d %B %Y")
    except Exception:
        friendly_date = date

    session["pending_booking"] = {"title": title, "date": date, "time": time_, "duration_mins": duration, "notes": notes, "customer_id": session.get("customer_id"), "customer_name": customer_name, "customer_phone": session.get("customer_phone", "")}
    session["state"] = "confirm_booking"

    customer_line = f"\n👤 *Customer:* {customer_name}" if customer_name else ""
    notes_line = f"\n📝 *Notes:* {notes}" if notes else ""

    preview_msg = (
        f"🔍 *Booking Preview*\n\n"
        f"📋 *Job:* {title}\n"
        f"📅 *Date:* {friendly_date}\n"
        f"🕐 *Time:* {time_}\n"
        f"⏱ *Duration:* {duration} mins"
        f"{customer_line}{notes_line}"
        f"{clash_warning}\n\n"
        f"Is this correct?"
    )
    await send_buttons(client, chat_id, preview_msg, [
        {"id": "confirmbk", "title": "✅ Confirm"},
        {"id": "cancelbk", "title": "❌ Cancel"},
    ])


async def _confirm_booking(chat_id: str, client: httpx.AsyncClient) -> None:
    import uuid as _uuid
    session = _wizard_sessions.get(chat_id)
    if not session or session.get("state") != "confirm_booking":
        await send_text(client, chat_id, "⚠️ No pending booking to confirm.")
        return

    pb = session.get("pending_booking")
    if not pb:
        await send_text(client, chat_id, "⚠️ No pending booking found.")
        return

    try:
        supabase = get_supabase()
        now = datetime.now(timezone.utc).isoformat()
        supabase.table("bookings").insert({
            "id": str(_uuid.uuid4()), "business_id": session["business_id"],
            "customer_id": pb["customer_id"], "customer_name": pb["customer_name"],
            "customer_phone": pb["customer_phone"], "title": pb["title"],
            "date": pb["date"], "time": pb["time"], "duration_mins": pb["duration_mins"],
            "notes": pb["notes"], "status": "confirmed",
            "created_at": now, "updated_at": now,
        }).execute()
    except Exception:
        logger.exception("Failed to save booking for chat_id %s", chat_id)
        await send_text(client, chat_id, "⚠️ Booking failed to save. Please try again.")
        session["state"] = "awaiting_booking_details"
        return

    try:
        from datetime import datetime as _dt
        friendly_date = _dt.strptime(pb["date"], "%Y-%m-%d").strftime("%A %d %B %Y")
    except Exception:
        friendly_date = pb["date"]

    customer_line = f"\n👤 *Customer:* {pb['customer_name']}" if pb["customer_name"] else ""
    notes_line = f"\n📝 *Notes:* {pb['notes']}" if pb["notes"] else ""

    await send_text(
        client, chat_id,
        f"✅ *Booking confirmed!*\n\n"
        f"📋 *Job:* {pb['title']}\n"
        f"📅 *Date:* {friendly_date}\n"
        f"🕐 *Time:* {pb['time']}\n"
        f"⏱ *Duration:* {pb['duration_mins']} mins"
        f"{customer_line}{notes_line}",
    )

    session.pop("pending_booking", None)
    session["state"] = "choose_action"
    await _show_action_menu(chat_id, session, client, "What would you like to do next?")


async def _cancel_booking(chat_id: str, client: httpx.AsyncClient) -> None:
    session = _wizard_sessions.get(chat_id)
    if session:
        session.pop("pending_booking", None)
        session["state"] = "awaiting_booking_details"
    await send_text(client, chat_id, "❌ Booking cancelled. Please type the booking details again.\n\nType *CANCEL* to go back to the menu.")


# ──────────────────────────────────────────────
# Photo handler — receipt OCR
# ──────────────────────────────────────────────

async def _handle_photo(
    chat_id: str, photo: list[dict], caption: str, client: httpx.AsyncClient
) -> None:
    session = _wizard_sessions.get(chat_id)
    if not session or session.get("state") != "awaiting_receipt_photo":
        await send_text(client, chat_id, "📸 To record an expense, type /start and choose *🧾 Record Expense* first.")
        return

    _start_timeout(chat_id, client)
    business = _get_business_by_chat_id(chat_id)
    if not business:
        _wizard_end_session(chat_id)
        return

    # Get largest photo
    file_id = photo[-1]["file_id"]

    await send_text(client, chat_id, "🧾 *Receipt received!* Scanning now...\nThis usually takes a few seconds.")

    try:
        file_info = await get_file(client, file_id)
        file_path = file_info.get("result", {}).get("file_path", "")
        if not file_path:
            raise ValueError("No file_path from Telegram")

        image_bytes = await download_file(client, file_path)
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64}"

        receipt = await extract_receipt_data(data_url)
        vendor = receipt.get("vendor", "Unknown")
        description = receipt.get("description", caption or "Receipt")
        category = receipt.get("category", "general")
        date = receipt.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        subtotal = float(receipt.get("subtotal", 0))
        tax_amount = float(receipt.get("tax_amount", 0))
        total = float(receipt.get("total", 0))
        currency = receipt.get("currency", business.get("currency", "GBP"))
        if total == 0 and subtotal > 0:
            total = subtotal + tax_amount

        supabase = get_supabase()
        import json as _json
        supabase.table("expenses").insert({
            "business_id": business["id"], "vendor": vendor, "description": description,
            "category": category, "date": date, "subtotal": round(subtotal, 2),
            "tax_amount": round(tax_amount, 2), "total": round(total, 2),
            "currency": currency, "receipt_data": _json.dumps(receipt), "receipt_image": data_url,
        }).execute()

        sym = "£" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "
        await send_text(
            client, chat_id,
            f"✅ *Expense recorded!*\n\n"
            f"🏪 *Vendor:* {vendor}\n"
            f"📝 *Description:* {description}\n"
            f"📂 *Category:* {category}\n"
            f"📅 *Date:* {date}\n"
            f"💰 *Total:* {sym}{total:.2f}"
            + (f" (inc. {sym}{tax_amount:.2f} VAT)" if tax_amount > 0 else ""),
        )

        session["state"] = "choose_action"
        await _show_action_menu(chat_id, session, client, "What would you like to do next?")

    except Exception:
        logger.exception("Failed to process receipt for chat_id %s", chat_id)
        await send_text(client, chat_id, "⚠️ Sorry, I couldn't read that receipt. Please make sure the image is clear and try again.")


# ──────────────────────────────────────────────
# Review draft approval
# ──────────────────────────────────────────────

async def _approve_draft(
    chat_id: str, draft_id: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    supabase = get_supabase()
    draft_result = supabase.table("review_drafts").select("*, businesses(*)").eq("id", draft_id).execute()
    if not draft_result.data:
        await send_text(client, chat_id, "Draft not found.")
        return

    draft = draft_result.data[0]
    biz = draft.get("businesses") or business

    encrypted_refresh = biz.get("google_refresh_token", "")
    account_id = biz.get("google_account_id", "")
    location_id = biz.get("google_location_id", "")

    if not (encrypted_refresh and account_id and location_id):
        await send_text(client, chat_id, "Google is not connected. Please connect your Google account via the dashboard first.")
        return

    try:
        refresh_token = decrypt(encrypted_refresh)
        access_token = await refresh_access_token(refresh_token)
        review_name = f"accounts/{account_id}/locations/{location_id}/reviews/{draft['google_review_id']}"
        await post_review_reply(access_token, review_name, draft["ai_draft_reply"])
        supabase.table("review_drafts").update({"status": "posted"}).eq("id", draft_id).execute()
        await send_text(client, chat_id, "✅ Done! Your reply has been posted to Google.")
    except Exception:
        logger.exception("Failed to post approved draft %s", draft_id)
        await send_text(client, chat_id, "Something went wrong posting to Google. Please try again.")


async def _post_edited_reply(
    chat_id: str, edited_text: str, business: dict[str, Any], draft: dict[str, Any], client: httpx.AsyncClient
) -> None:
    supabase = get_supabase()
    encrypted_refresh = business.get("google_refresh_token", "")
    account_id = business.get("google_account_id", "")
    location_id = business.get("google_location_id", "")

    if not (encrypted_refresh and account_id and location_id):
        await send_text(client, chat_id, "Google is not connected. Please connect your Google account first.")
        return
    try:
        refresh_token = decrypt(encrypted_refresh)
        access_token = await refresh_access_token(refresh_token)
        review_name = f"accounts/{account_id}/locations/{location_id}/reviews/{draft['google_review_id']}"
        await post_review_reply(access_token, review_name, edited_text)
        supabase.table("review_drafts").update({"status": "posted", "ai_draft_reply": edited_text}).eq("id", draft["id"]).execute()
        await send_text(client, chat_id, "✅ Done! Your reply has been posted to Google.")
    except Exception:
        logger.exception("Failed to post edited reply for draft %s", draft["id"])
        await send_text(client, chat_id, "Something went wrong posting your reply. Please try again.")


# ──────────────────────────────────────────────
# Login / dashboard link
# ──────────────────────────────────────────────

async def _cmd_login(chat_id: str, business: dict[str, Any], client: httpx.AsyncClient) -> None:
    settings = get_settings()
    base = settings.base_url.rstrip("/")
    login_url = f"{base}/login.html"
    await send_text(
        client, chat_id,
        f"👉 Open your dashboard:\n{login_url}\n\n"
        f"Tap the link, enter your phone number, and we'll send you a login code.",
    )
