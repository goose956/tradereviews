"""WhatsApp Cloud API webhook — verification (GET) and inbound messages (POST)."""

import asyncio
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.core.config import get_settings
from app.core.security import decrypt
from app.db.supabase import get_supabase
from app.services.google import post_review_reply, refresh_access_token
from app.services.message_log import log_message
from app.services.parser import parse_review_command
from app.services.whatsapp import (
    send_interactive_buttons,
    send_interactive_list,
    send_template_message,
    send_text_message,
    download_media,
)
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

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook/whatsapp", tags=["webhook"])

# ──────────────────────────────────────────────
# Message deduplication (prevent duplicate webhook processing)
# ──────────────────────────────────────────────
_processed_msg_ids: dict[str, float] = {}  # msg_id → timestamp
_DEDUP_TTL = 300  # keep IDs for 5 minutes


def _is_duplicate(msg_id: str) -> bool:
    """Return True if we already processed this message ID."""
    now = datetime.now(timezone.utc).timestamp()
    # Prune old entries
    stale = [k for k, v in _processed_msg_ids.items() if now - v > _DEDUP_TTL]
    for k in stale:
        _processed_msg_ids.pop(k, None)
    if msg_id in _processed_msg_ids:
        return True
    _processed_msg_ids[msg_id] = now
    return False


# ──────────────────────────────────────────────
# Wizard session state + timeout (10 min)
# ──────────────────────────────────────────────
_wizard_sessions: dict[str, dict[str, Any]] = {}
_wizard_timeouts: dict[str, asyncio.Task] = {}
_SESSION_TIMEOUT = 600  # seconds


def _start_timeout(sender: str, client: httpx.AsyncClient) -> None:
    """Start or restart the 10-minute inactivity timer."""
    old = _wizard_timeouts.pop(sender, None)
    if old:
        old.cancel()

    async def _fire():
        await asyncio.sleep(_SESSION_TIMEOUT)
        session = _wizard_sessions.pop(sender, None)
        _wizard_timeouts.pop(sender, None)
        if session:
            try:
                await send_text_message(
                    client, sender,
                    "\u23f0 *Session ended* \u2014 no activity for 10 minutes.\n\n"
                    "Type /START to begin a new session.",
                )
            except Exception:
                pass

    _wizard_timeouts[sender] = asyncio.create_task(_fire())


def _wizard_end_session(sender: str) -> None:
    """Clean up wizard session and cancel timeout."""
    _wizard_sessions.pop(sender, None)
    task = _wizard_timeouts.pop(sender, None)
    if task:
        task.cancel()


# ──────────────────────────────────────────────
# Interactive Demo — sandbox with 30-message limit
# ──────────────────────────────────────────────
_demo_sessions: dict[str, dict] = {}  # sender raw phone → {msg_count, business_id}
_DEMO_MSG_LIMIT = 30

_DEMO_TRIGGERS = {
    "hi, show me how this works!",
    "hi, show me how this works",
    "hi show me how this works",
    "show me how this works",
    "demo",
    "/demo",
}


def _seed_demo_data(supabase: Any, biz_id: str, cust_id: str) -> None:
    """Insert permanent demo records for every feature (idempotent per category)."""
    import uuid as _uuid
    from datetime import timedelta

    now = datetime.now(timezone.utc)

    # ── Invoices (1 paid, 1 outstanding) ──
    has_inv = supabase.table("invoices").select("id").eq(
        "business_id", biz_id).eq("customer_id", cust_id).limit(1).execute()
    if not has_inv.data:
        inv1_id = str(_uuid.uuid4())
        supabase.table("invoices").insert({
            "id": inv1_id,
            "business_id": biz_id,
            "customer_id": cust_id,
            "invoice_number": "INV-001",
            "status": "paid",
            "subtotal": 350.00,
            "tax_rate": 20,
            "tax_amount": 70.00,
            "total": 420.00,
            "currency": "GBP",
            "payment_terms": "Payment due within 14 days",
            "notes": "Annual boiler service and safety check",
            "paid_at": (now - timedelta(days=3)).isoformat(),
        }).execute()
        supabase.table("line_items").insert({
            "parent_id": inv1_id, "parent_type": "invoice",
            "description": "Annual boiler service and safety check",
            "quantity": 1, "unit_price": 350.00, "total": 350.00, "sort_order": 0,
        }).execute()

        inv2_id = str(_uuid.uuid4())
        supabase.table("invoices").insert({
            "id": inv2_id,
            "business_id": biz_id,
            "customer_id": cust_id,
            "invoice_number": "INV-002",
            "status": "sent",
            "subtotal": 180.00,
            "tax_rate": 20,
            "tax_amount": 36.00,
            "total": 216.00,
            "currency": "GBP",
            "payment_terms": "Payment due within 14 days",
            "notes": "Radiator replacement — front bedroom",
            "due_date": (now + timedelta(days=11)).strftime("%Y-%m-%d"),
        }).execute()
        supabase.table("line_items").insert({
            "parent_id": inv2_id, "parent_type": "invoice",
            "description": "Radiator replacement — front bedroom",
            "quantity": 1, "unit_price": 180.00, "total": 180.00, "sort_order": 0,
        }).execute()

    # ── Quote ──
    has_quo = supabase.table("quotes").select("id").eq(
        "business_id", biz_id).eq("customer_id", cust_id).limit(1).execute()
    if not has_quo.data:
        quo_id = str(_uuid.uuid4())
        supabase.table("quotes").insert({
            "id": quo_id,
            "business_id": biz_id,
            "customer_id": cust_id,
            "quote_number": "QUO-001",
            "status": "sent",
            "subtotal": 1200.00,
            "tax_rate": 20,
            "tax_amount": 240.00,
            "total": 1440.00,
            "currency": "GBP",
            "valid_until": (now + timedelta(days=30)).strftime("%Y-%m-%d"),
            "notes": "Full bathroom refit — supply and install all pipework, basin, toilet and shower",
        }).execute()
        supabase.table("line_items").insert({
            "parent_id": quo_id, "parent_type": "quote",
            "description": "Full bathroom refit — supply and install all pipework, basin, toilet and shower",
            "quantity": 1, "unit_price": 1200.00, "total": 1200.00, "sort_order": 0,
        }).execute()

    # ── Expenses ──
    has_exp = supabase.table("expenses").select("id").eq(
        "business_id", biz_id).limit(1).execute()
    if not has_exp.data:
        for vendor, desc, cat, amount, days_ago in [
            ("Screwfix", "22mm copper pipe and compression fittings", "materials", 47.85, 5),
            ("Toolstation", "Thermostatic radiator valve x2", "materials", 28.50, 3),
            ("Shell Garage", "Diesel — van", "fuel", 65.00, 1),
        ]:
            exp_id = str(_uuid.uuid4())
            tax = round(amount * 0.2, 2)
            supabase.table("expenses").insert({
                "id": exp_id,
                "business_id": biz_id,
                "vendor": vendor,
                "description": desc,
                "category": cat,
                "date": (now - timedelta(days=days_ago)).strftime("%Y-%m-%d"),
                "subtotal": amount,
                "tax_amount": tax,
                "total": round(amount + tax, 2),
                "currency": "GBP",
            }).execute()

    # ── Bookings (1 past, 2 upcoming) ──
    has_bk = supabase.table("bookings").select("id").eq(
        "business_id", biz_id).limit(1).execute()
    if not has_bk.data:
        for title, cname, days_offset, time_str in [
            ("Boiler service — annual check", "John Smith", -2, "09:00"),
            ("Radiator install — front bedroom", "John Smith", 2, "10:00"),
            ("Emergency leak repair — kitchen", "John Smith", 5, "14:30"),
        ]:
            bk_id = str(_uuid.uuid4())
            supabase.table("bookings").insert({
                "id": bk_id,
                "business_id": biz_id,
                "customer_id": cust_id,
                "customer_name": cname,
                "customer_phone": "+447700900123",
                "title": title,
                "date": (now + timedelta(days=days_offset)).strftime("%Y-%m-%d"),
                "time": time_str,
                "duration_mins": 60,
                "notes": "",
                "status": "confirmed",
            }).execute()


def _demo_cleanup(sender: str) -> None:
    """Pop demo session and restore original business name if needed."""
    demo = _demo_sessions.pop(sender, None)
    if not demo:
        return
    orig_name = demo.get("original_biz_name")
    orig_trade = demo.get("original_trade")
    biz_id = demo.get("business_id")
    if orig_name and biz_id:
        updates: dict = {"business_name": orig_name}
        if orig_trade:
            updates["trade_type"] = orig_trade
        get_supabase().table("businesses").update(updates).eq("id", biz_id).execute()


def _demo_action_menu_rows() -> list[dict]:
    """Action menu for demo users — includes signup option."""
    return [
        {"id": "wiz_review", "title": "⭐ Review Request", "description": "See what your customers receive"},
        {"id": "wiz_invoice", "title": "💷 Create Invoice", "description": "Build a real invoice with PDF"},
        {"id": "wiz_quote", "title": "📋 Create Quote", "description": "Build a real quote with PDF"},
        {"id": "wiz_expense", "title": "🧾 Snap a Receipt", "description": "AI reads & logs it automatically"},
        {"id": "wiz_booking", "title": "📅 New Booking", "description": "Add a job to your calendar"},
        {"id": "wiz_view_bookings", "title": "🗓 View Calendar", "description": "See upcoming bookings"},
        {"id": "wiz_balance", "title": "💰 Account Balance", "description": "View income & outstanding"},
        {"id": "wiz_view_expenses", "title": "📊 View Expenses", "description": "See your expense summary"},
        {"id": "demo_start_trial", "title": "🚀 Get Started", "description": "Sign up — takes 30 seconds"},
    ]


async def _demo_show_menu(
    sender: str, demo: dict, client: httpx.AsyncClient,
    prefix: str = "",
) -> None:
    """Show the demo action menu, with periodic signup nudge."""
    header = prefix or "What would you like to try next?"
    await send_interactive_list(
        client, sender,
        f"✅ {header}",
        "Choose an option",
        [{"title": "Actions", "rows": _demo_action_menu_rows()}],
    )

    # Every 5 messages, send a signup nudge
    count = demo.get("msg_count", 0)
    if count > 0 and count % 5 == 0:
        await send_interactive_buttons(
            client, sender,
            "💡 Enjoying the demo? Get your own account — takes 30 seconds, cancel anytime.",
            [{"id": "demo_start_trial", "title": "🚀 Get Started"}],
        )


async def _demo_check_limit(
    sender: str, client: httpx.AsyncClient,
) -> bool:
    """Increment demo counter. If limit reached, show signup and return True."""
    demo = _demo_sessions.get(sender)
    if not demo:
        return False
    demo["msg_count"] = demo.get("msg_count", 0) + 1
    if demo["msg_count"] >= _DEMO_MSG_LIMIT:
        await _demo_show_signup(sender, demo, client)
        return True
    return False


async def _demo_show_signup(
    sender: str, demo: dict, client: httpx.AsyncClient,
) -> None:
    """Demo limit reached — prompt to sign up."""
    settings = get_settings()
    biz_id = demo.get("business_id", "")
    _wizard_end_session(sender)
    _demo_cleanup(sender)

    await send_text_message(
        client, sender,
        "🎉 *That's the end of your demo!*\n\n"
        "You've seen what GafferApp can do — now imagine "
        "it working for your real customers every day.\n\n"
        "Ready to get started?",
    )
    await send_interactive_buttons(
        client, sender,
        "Sign up takes 30 seconds:",
        [
            {"id": "demo_start_trial", "title": "🚀 Get Started"},
        ],
    )


async def _maybe_handle_demo(
    sender: str, text: str, client: httpx.AsyncClient
) -> bool:
    """Handle demo trigger. Returns True if handled."""
    demo = _demo_sessions.get(sender)
    trimmed = text.strip()
    lower = trimmed.lower().rstrip("!.")

    # ── Signup flow: awaiting business name ──
    if demo and demo.get("state") == "awaiting_business_name":
        biz_name = trimmed
        if len(biz_name) < 2:
            await send_text_message(client, sender, "Just type your business name — e.g. \"Smith's Plumbing\"")
            return True
        demo["business_name"] = biz_name
        demo["state"] = "awaiting_trade"
        await send_interactive_list(
            client, sender,
            f"Great — {biz_name}! What trade are you in?",
            "Pick your trade",
            [{"title": "Trade", "rows": [
                {"id": "demo_trade_plumber", "title": "Plumber"},
                {"id": "demo_trade_electrician", "title": "Electrician"},
                {"id": "demo_trade_builder", "title": "Builder"},
                {"id": "demo_trade_roofer", "title": "Roofer"},
                {"id": "demo_trade_landscaper", "title": "Landscaper"},
                {"id": "demo_trade_painter", "title": "Painter & Decorator"},
                {"id": "demo_trade_other", "title": "Other"},
            ]}],
        )
        return True

    # ── Existing demo user sends a demo trigger again — restart fresh ──
    if demo and lower in _DEMO_TRIGGERS:
        _wizard_end_session(sender)
        _demo_cleanup(sender)
        demo = None  # fall through to "New demo trigger" below

    # ── Existing demo user with active wizard — let wizard handle it ──
    if demo and sender in _wizard_sessions:
        return False

    # ── Existing demo user, no wizard — re-entry (typed /START etc) ──
    if demo:
        if await _demo_check_limit(sender, client):
            return True
        # Re-create wizard session with demo customer pre-selected
        _wizard_sessions[sender] = {
            "state": "choose_action",
            "business_id": demo["business_id"],
            "customer_phone": "+447700900123",
            "customer_name": "John Smith",
            "channel": "whatsapp",
        }
        _start_timeout(sender, client)
        await _demo_show_menu(sender, demo, client)
        return True

    # ── New demo trigger ──
    if lower not in _DEMO_TRIGGERS:
        return False

    # /demo always works; casual greetings skip registered businesses
    force = lower in ("/demo", "demo")
    if not force:
        sender_e164 = f"+{sender}" if not sender.startswith("+") else sender
        supabase = get_supabase()
        biz = supabase.table("businesses").select("id").eq("phone_number", sender_e164).execute()
        if biz.data:
            return False

    # ── Create demo business + customer ──
    import uuid
    sender_e164 = f"+{sender}" if not sender.startswith("+") else sender
    supabase = get_supabase()

    # Re-use existing demo business for this phone, or create new
    existing = supabase.table("businesses").select("id, subscription_status, business_name, trade_type").eq(
        "phone_number", sender_e164
    ).execute()
    original_biz_name = None
    original_trade = None
    if existing.data and existing.data[0].get("subscription_status") in ("demo", "trial"):
        biz_id = existing.data[0]["id"]
    elif existing.data:
        # Already a real business — create demo session pointing to it
        biz_id = existing.data[0]["id"]
        original_biz_name = existing.data[0].get("business_name")
        original_trade = existing.data[0].get("trade_type")
    else:
        biz_id = str(uuid.uuid4())
        supabase.table("businesses").insert({
            "id": biz_id,
            "owner_name": "Demo User",
            "business_name": "Plumbing Services 247",
            "phone_number": sender_e164,
            "trade_type": "plumber",
            "subscription_status": "demo",
        }).execute()

    # Always set demo business name so the demo looks consistent
    supabase.table("businesses").update({
        "business_name": "Plumbing Services 247",
        "trade_type": "plumber",
    }).eq("id", biz_id).execute()

    # Ensure demo customer exists
    demo_cust = supabase.table("customers").select("id").eq(
        "business_id", biz_id
    ).eq("phone_number", "+447700900123").execute()
    if not demo_cust.data:
        supabase.table("customers").upsert({
            "business_id": biz_id,
            "phone_number": "+447700900123",
            "name": "John Smith",
            "status": "active",
        }, on_conflict="business_id,phone_number").execute()

    supabase.table("businesses").update(
        {"active_customer_phone": "+447700900123"}
    ).eq("id", biz_id).execute()

    # ── Seed ALL demo data (idempotent — only inserts if missing) ──
    demo_cust_row = supabase.table("customers").select("id").eq(
        "business_id", biz_id
    ).eq("phone_number", "+447700900123").limit(1).execute()
    demo_cust_id = demo_cust_row.data[0]["id"] if demo_cust_row.data else ""

    if demo_cust_id:
        _seed_demo_data(supabase, biz_id, demo_cust_id)

    # Set up demo tracking (save originals so we can restore after demo)
    _demo_sessions[sender] = {
        "msg_count": 0,
        "business_id": biz_id,
        "original_biz_name": original_biz_name,
        "original_trade": original_trade,
    }

    # Set up wizard — NO pre-selected customer so user picks them
    _wizard_sessions[sender] = {
        "state": "awaiting_customer_pick",
        "business_id": biz_id,
        "channel": "whatsapp",
    }
    _start_timeout(sender, client)

    # Get demo customer id for button
    _demo_cust_id = demo_cust_row.data[0]["id"] if demo_cust_row.data else ""

    await send_text_message(
        client, sender,
        "Hey! 👋 Welcome to *GafferApp*.\n\n"
        "Let me show you how easy it is to get a 5-star review.\n\n"
        "First — pick the customer you just finished a job for:",
    )

    # Show customer picker (just John Smith)
    await send_interactive_buttons(
        client, sender,
        "Who would you like to send a review request to?",
        [{"id": f"wiz_cust_{_demo_cust_id}", "title": "John Smith"}],
    )
    return True


async def _handle_demo_button(
    sender: str, payload: str, client: httpx.AsyncClient
) -> bool:
    """Handle demo-specific button taps. Returns True if handled."""
    if not payload.startswith("demo_"):
        return False

    # ── Review demo: user confirmed delivery channel — now send ──
    if payload == "demo_review_send_confirm":
        session = _wizard_sessions.get(sender)
        if not session:
            return True
        biz_id = session.get("business_id", "")
        biz_row = get_supabase().table("businesses").select("business_name").eq("id", biz_id).execute().data
        biz_name = biz_row[0]["business_name"] if biz_row else "Plumbing Services 247"
        customer_name = session.get("customer_name", "John Smith")
        job_desc = session.get("review_job_description", "")
        job_snippet = f" for the {job_desc}" if job_desc else ""
        job_thanks = f"We hope you're happy with the {job_desc} we completed for you. " if job_desc else ""

        await send_text_message(
            client, sender,
            f"✅ *Review request sent to {customer_name}!*\n\n"
            "📱 Now switch hats — you're *John* for a moment.\n"
            "Here's what he'd see on his phone:",
        )
        await send_interactive_buttons(
            client, sender,
            f"Hi John, thanks for choosing {biz_name}{job_snippet}! "
            f"{job_thanks}"
            f"How was your experience?",
            [
                {"id": "demo_review_great", "title": "Great! ⭐"},
                {"id": "demo_review_bad", "title": "Could be better"},
            ],
        )
        session["state"] = "demo_awaiting_review_tap"
        return True

    # ── Review demo: user tapped "Great!" as the customer ──
    if payload == "demo_review_great":
        session = _wizard_sessions.get(sender)
        biz_name = "Plumbing Services 247"
        if session:
            biz = get_supabase().table("businesses").select("business_name").eq(
                "id", session["business_id"]
            ).execute().data
            if biz:
                biz_name = biz[0]["business_name"]

        await send_text_message(
            client, sender,
            "👷 *Back to YOUR view now…*\n\n"
            "✅ *John Smith left a 5-star review!*\n"
            "⭐⭐⭐⭐⭐\n\n"
            "_\"Great service from " + biz_name + ", very professional.\"_\n\n"
            "🤖 *AI has drafted a reply:*\n"
            "_\"Thank you so much John! We really appreciate your kind words. "
            "It was a pleasure working with you — don't hesitate to reach out "
            "if you need anything in future!\"_\n\n"
            "In the real app you'd tap to approve or edit this reply, "
            "and it gets posted to Google automatically.\n\n"
            "_Auto follow-ups are also sent if customers don't respond._",
        )
        if session:
            session["state"] = "choose_action"
        demo = _demo_sessions.get(sender)
        if demo:
            demo["review_done"] = True
        await send_interactive_list(
            client, sender,
            "That's the review flow! 🎉\n\n"
            "But that's just the start — here's everything else you can do:",
            "Choose an option",
            [{"title": "Actions", "rows": _demo_action_menu_rows()}],
        )
        await send_interactive_buttons(
            client, sender,
            "💡 Want this working for your real customers? Set up takes 30 seconds.",
            [{"id": "demo_start_trial", "title": "🚀 Get Started"}],
        )
        return True

    # ── Review demo: user tapped "Could be better" as the customer ──
    if payload == "demo_review_bad":
        session = _wizard_sessions.get(sender)
        customer_phone = "+447700900123"

        await send_text_message(
            client, sender,
            "👷 *Back to YOUR view now…*\n\n"
            "⚠️ *John Smith wasn't fully satisfied.*\n\n"
            "Their feedback comes to YOU *privately* — not posted publicly.\n"
            f"You'd get a prompt to call them on {customer_phone} "
            "to sort things out before they leave a bad review.\n\n"
            "This catches unhappy customers *before* they go to Google. 🛡️",
        )
        if session:
            session["state"] = "choose_action"
        demo = _demo_sessions.get(sender)
        if demo:
            demo["review_done"] = True
        await send_interactive_list(
            client, sender,
            "That's the review flow! 🎉\n\n"
            "But that's just the start — here's everything else you can do:",
            "Choose an option",
            [{"title": "Actions", "rows": _demo_action_menu_rows()}],
        )
        await send_interactive_buttons(
            client, sender,
            "💡 Want this working for your real customers? Set up takes 30 seconds.",
            [{"id": "demo_start_trial", "title": "🚀 Get Started"}],
        )
        return True

    # ── Start Trial — ask for business name ──
    if payload == "demo_start_trial":
        demo = _demo_sessions.get(sender)
        if demo:
            _demo_cleanup(sender)
            _wizard_end_session(sender)

        # Check if they already have a real business
        sender_e164 = f"+{sender}" if not sender.startswith("+") else sender
        supabase = get_supabase()
        existing = supabase.table("businesses").select("id, subscription_status").eq(
            "phone_number", sender_e164
        ).execute()

        if existing.data and existing.data[0].get("subscription_status") not in ("demo", "trial", None):
            biz_id = existing.data[0]["id"]
            settings = get_settings()
            checkout_url = f"{settings.base_url}/checkout.html?business_id={biz_id}"
            await send_text_message(
                client, sender,
                f"You already have an account! Complete your setup:\n"
                f"👉 {checkout_url}",
            )
            return True

        # Start signup: ask business name
        _demo_sessions[sender] = {"state": "awaiting_business_name"}
        await send_text_message(
            client, sender,
            "🚀 Let's get you set up! It takes 30 seconds.\n\n"
            "What's your *business name*?",
        )
        return True

    # ── Trade selection (during signup) ──
    if payload.startswith("demo_trade_"):
        session = _demo_sessions.get(sender)
        if not session:
            return False

        trade = payload.replace("demo_trade_", "")
        biz_name = session.get("business_name", "Plumbing Services 247")
        sender_e164 = f"+{sender}" if not sender.startswith("+") else sender
        settings = get_settings()

        # Create or update the business record (inactive until payment)
        supabase = get_supabase()
        existing = supabase.table("businesses").select("id").eq(
            "phone_number", sender_e164
        ).execute()
        if existing.data:
            biz_id = existing.data[0]["id"]
            supabase.table("businesses").update({
                "business_name": biz_name,
                "owner_name": biz_name,
                "trade_type": trade,
                "subscription_status": "inactive",
            }).eq("phone_number", sender_e164).execute()
        else:
            import uuid
            biz_id = str(uuid.uuid4())
            supabase.table("businesses").insert({
                "id": biz_id,
                "owner_name": biz_name,
                "business_name": biz_name,
                "phone_number": sender_e164,
                "trade_type": trade,
                "subscription_status": "inactive",
            }).execute()

        # Already set correct name above — just drop the demo session (no restore)
        demo = _demo_sessions.pop(sender, None)

        checkout_url = f"{settings.base_url}/checkout.html?business_id={biz_id}"
        await send_text_message(
            client, sender,
            f"🎉 *Almost there, {biz_name}!*\n\n"
            f"Here's your personalised setup page — see exactly what's "
            f"included, our money-back guarantee, and get started:\n\n"
            f"👉 {checkout_url}\n\n"
            f"🔒 Cancel anytime · 💰 14-day money-back guarantee",
        )
        return True

    return False


# ──────────────────────────────────────────────
# GET  /webhook  — Meta verification challenge
# ──────────────────────────────────────────────
@router.get("")
async def verify_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
) -> Response:
    settings = get_settings()

    if hub_mode == "subscribe" and hmac.compare_digest(
        hub_verify_token, settings.whatsapp_verify_token
    ):
        logger.info("Webhook verified successfully")
        return Response(content=hub_challenge, media_type="text/plain")

    logger.warning("Webhook verification failed — token mismatch")
    raise HTTPException(status_code=403, detail="Verification failed")


# ──────────────────────────────────────────────
# POST /webhook  — Inbound messages & statuses
# ──────────────────────────────────────────────
@router.post("", status_code=200)
async def receive_message(request: Request) -> dict[str, str]:
    raw_body = await request.body()

    # Verify webhook signature if app secret is configured
    settings = get_settings()
    app_secret = settings.whatsapp_app_secret
    if app_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            app_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("Invalid webhook signature")
            raise HTTPException(status_code=403, detail="Invalid signature")

    body: dict[str, Any] = json.loads(raw_body)

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                try:
                    await _handle_message(message, request)
                except Exception:
                    logger.exception("Error handling message: %s", message.get("id", "?"))

    return {"status": "ok"}


# ──────────────────────────────────────────────
# Message router
# ──────────────────────────────────────────────
async def _handle_message(message: dict[str, Any], request: Request) -> None:
    msg_id = message.get("id", "")
    if msg_id and _is_duplicate(msg_id):
        logger.debug("Skipping duplicate message %s", msg_id)
        return

    msg_type = message.get("type")
    sender = message.get("from", "")
    http_client = request.app.state.http_client

    if msg_type == "button":
        payload = message.get("button", {}).get("payload", "")
        await _handle_button(sender, payload, http_client)
        return

    if msg_type == "interactive":
        interactive = message.get("interactive", {})
        button_id = interactive.get("button_reply", {}).get("id", "")
        if not button_id:
            button_id = interactive.get("list_reply", {}).get("id", "")
        if button_id:
            await _handle_button(sender, button_id, http_client)
        return

    if msg_type == "text":
        text = message.get("text", {}).get("body", "")
        await _handle_text(sender, text, http_client)
        return

    if msg_type == "image":
        image_info = message.get("image", {})
        await _handle_image(sender, image_info, http_client)
        return

    logger.debug("Ignoring message type=%s from %s", msg_type, sender)


# ──────────────────────────────────────────────
# Text message router
# ──────────────────────────────────────────────
async def _handle_text(
    sender: str, text: str, client: httpx.AsyncClient
) -> None:
    supabase = get_supabase()
    sender_e164 = f"+{sender}" if not sender.startswith("+") else sender

    # ── Demo flow (non-registered leads from the website) ──
    if await _maybe_handle_demo(sender, text.strip(), client):
        return

    # ── Route: is this a tradesperson or a customer? ──
    biz_result = (
        supabase.table("businesses")
        .select("*")
        .eq("phone_number", sender_e164)
        .execute()
    )

    if biz_result.data:
        biz = biz_result.data[0]
        status = biz.get("subscription_status", "")

        # Demo user: check message limit
        if sender in _demo_sessions:
            if await _demo_check_limit(sender, client):
                return

        # Nudge inactive (unpaid) users
        if status == "inactive":
            settings = get_settings()
            checkout_url = f"{settings.base_url}/checkout.html?business_id={biz['id']}"
            await send_text_message(
                client, sender,
                f"👋 Hey {biz.get('business_name', 'there')}! "
                f"Your account isn't active yet.\n\n"
                f"Complete your setup here to get started:\n"
                f"👉 {checkout_url}\n\n"
                f"🔒 Cancel anytime · 💰 14-day money-back guarantee",
            )
            return
        await _handle_tradesperson_text(sender, text, biz, client)
    else:
        await _handle_customer_text(sender, sender_e164, text, client)


# ──────────────────────────────────────────────
# Tradesperson — /START wizard flow
# ──────────────────────────────────────────────
async def _handle_tradesperson_text(
    sender: str, text: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Process text from a registered business owner — wizard-based flow."""
    supabase = get_supabase()
    trimmed = text.strip()
    upper = trimmed.upper()
    session = _wizard_sessions.get(sender)

    # ── Always-available commands ──
    if upper.startswith("/LOGIN"):
        await _cmd_login(sender, business, client)
        return
    if upper.startswith("/HELP"):
        await send_text_message(
            client, sender,
            "\U0001f4cb *How to use GafferApp*\n\n"
            "Type /START to begin a new session.\n"
            "The bot will walk you through sending reviews,\n"
            "invoices, or quotes step by step.\n\n"
            "Type /LOGIN to access your dashboard.",
        )
        return

    # ── /START — begin a new wizard session ──
    if upper in ("/START", "START"):
        await _wizard_start(sender, business, client)
        return

    # ── In a session — wizard takes priority over everything else ──
    if session:
        await _wizard_handle_text(sender, trimmed, session, business, client)
        return

    # ── Handle awaiting_edit drafts (Google review replies) ──
    if not upper.startswith("/"):
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
            await _post_edited_reply(
                sender, trimmed, business, draft_result.data[0], client
            )
            return

    # ── No session, not a recognised command ──
    await send_text_message(
        client, sender,
        "\U0001f44b Type /START to begin.\n\n"
        "I'll walk you through sending reviews, invoices,\n"
        "or quotes to your customers.",
    )


# ──────────────────────────────────────────────
# Wizard step handlers
# ──────────────────────────────────────────────

_CHANNEL_LABELS = {
    "whatsapp": "📱 WhatsApp",
    "email": "📧 Email",
    "sms": "💬 SMS",
}


def _action_menu_rows(channel: str = "whatsapp", sender: str = "") -> list[dict]:
    """Return the interactive-list rows for the action picker."""
    # Demo users get a menu with signup option instead of dashboard/channel
    if sender and sender in _demo_sessions:
        return _demo_action_menu_rows()
    ch_label = _CHANNEL_LABELS.get(channel, "📱 WhatsApp")
    return [
        {"id": "wiz_review", "title": "⭐ Review Request", "description": "Ask for a Google review"},
        {"id": "wiz_invoice", "title": "💷 Send Invoice", "description": "Create & send an invoice"},
        {"id": "wiz_quote", "title": "📋 Send Quote", "description": "Create & send a quote"},
        {"id": "wiz_expense", "title": "🧾 Record Expense", "description": "Snap a receipt to log it"},
        {"id": "wiz_view_expenses", "title": "📊 View Expenses", "description": "See your expense summary"},
        {"id": "wiz_booking", "title": "📅 New Booking", "description": "Add a job to your calendar"},
        {"id": "wiz_view_bookings", "title": "🗓 View Calendar", "description": "See upcoming bookings"},
        {"id": "wiz_balance", "title": "💰 Account Balance", "description": "View income & outstanding"},
        {"id": "wiz_dashboard", "title": "💻 Open Dashboard", "description": "Manage your account"},
        {"id": "wiz_channel", "title": "📲 Change Channel", "description": f"Currently: {ch_label}"},
    ]

async def _wizard_start(
    sender: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Start a new wizard session — ask new or existing customer."""
    _wizard_end_session(sender)
    _wizard_sessions[sender] = {
        "state": "choose_customer_type",
        "business_id": business["id"],
    }
    _start_timeout(sender, client)
    await send_interactive_buttons(
        client, sender,
        "\U0001f44b *Let's get started!*\n\n"
        "Is this for a new customer or an existing one?",
        [
            {"id": "wiz_new", "title": "\u2795 New Customer"},
            {"id": "wiz_existing", "title": "\U0001f4cb Existing Customer"},
        ],
    )


async def _wizard_handle_text(
    sender: str, text: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Handle free-text input during a wizard session."""
    state = session["state"]
    _start_timeout(sender, client)

    if text.upper() in ("CANCEL", "/CANCEL"):
        _wizard_end_session(sender)
        await send_text_message(
            client, sender,
            "\u274c Session cancelled.\n\nType /START to begin again.",
        )
        return

    if state == "awaiting_new_customer":
        await _wizard_new_customer_input(sender, text, session, business, client)
    elif state == "awaiting_customer_email":
        await _wizard_email_input(sender, text, session, business, client)
    elif state == "awaiting_invoice_details":
        await _wizard_invoice_input(sender, text, session, business, client)
    elif state == "awaiting_quote_details":
        await _wizard_quote_input(sender, text, session, business, client)
    elif state == "awaiting_booking_details":
        await _wizard_booking_input(sender, text, session, business, client)
    elif state == "awaiting_booking_name":
        await _wizard_booking_name_input(sender, text, session, business, client)
    elif state == "awaiting_receipt_photo":
        await send_text_message(
            client, sender,
            "📸 Please send a *photo* of the receipt — not text.\n\n"
            "Tap the 📎 or 📷 icon to attach an image.\n"
            "Or type *CANCEL* to go back.",
        )
        return
    else:
        await send_text_message(
            client, sender,
            "\u261d\ufe0f Please tap one of the buttons above to continue.\n\n"
            "Or type *CANCEL* to cancel.",
        )


async def _wizard_handle_button(
    sender: str, payload: str, client: httpx.AsyncClient
) -> bool:
    """Handle wizard-prefixed button/list taps. Returns True if handled."""
    session = _wizard_sessions.get(sender)
    if not session:
        return False

    _start_timeout(sender, client)
    supabase = get_supabase()

    if payload == "wiz_new":
        session["state"] = "awaiting_new_customer"
        await send_text_message(
            client, sender,
            "\U0001f4dd Please type the customer's *name* and *phone number*.\n\n"
            "Example: *John Smith 07845774563*",
        )
        return True

    if payload == "wiz_existing":
        biz = supabase.table("businesses").select("*").eq(
            "id", session["business_id"]
        ).execute()
        business = biz.data[0] if biz.data else {}
        await _wizard_show_existing_customers(sender, session, business, client)
        return True

    if payload.startswith("wiz_cust_"):
        customer_id = payload.removeprefix("wiz_cust_")
        biz = supabase.table("businesses").select("*").eq(
            "id", session["business_id"]
        ).execute()
        business = biz.data[0] if biz.data else {}
        await _wizard_customer_selected(sender, customer_id, business, client)
        return True

    if payload == "wiz_review":
        biz = supabase.table("businesses").select("*").eq(
            "id", session["business_id"]
        ).execute()
        business = biz.data[0] if biz.data else {}
        await _wizard_review_check_invoices(sender, session, business, client)
        return True

    if payload.startswith("wiz_rev_inv_"):
        invoice_id = payload.removeprefix("wiz_rev_inv_")
        biz = supabase.table("businesses").select("*").eq(
            "id", session["business_id"]
        ).execute()
        business = biz.data[0] if biz.data else {}
        # Fetch invoice line items for personalisation
        items = supabase.table("line_items").select("description").eq(
            "parent_id", invoice_id
        ).eq("parent_type", "invoice").execute()
        job_desc = ", ".join(
            it["description"] for it in (items.data or []) if it.get("description")
        )
        session["review_job_description"] = job_desc or ""
        await _wizard_action_review(sender, business, client)
        return True

    if payload == "wiz_rev_skip":
        biz = supabase.table("businesses").select("*").eq(
            "id", session["business_id"]
        ).execute()
        business = biz.data[0] if biz.data else {}
        session["review_job_description"] = ""
        await _wizard_action_review(sender, business, client)
        return True

    if payload == "wiz_invoice":
        await _wizard_action_invoice(sender, session, client)
        return True

    if payload == "wiz_quote":
        await _wizard_action_quote(sender, session, client)
        return True

    if payload == "wiz_expense":
        session["state"] = "awaiting_receipt_photo"
        await send_text_message(
            client, sender,
            "📸 *Record an Expense*\n\n"
            "Take a photo of your receipt or forward one from your gallery.\n\n"
            "Type *CANCEL* to go back.",
        )
        return True

    if payload == "wiz_booking":
        session["state"] = "awaiting_booking_details"
        await send_text_message(
            client, sender,
            "📅 *New Booking*\n\n"
            "Type the job details — include a *date*, *time*, and *description*.\n\n"
            "Examples:\n"
            "• _Boiler service Tuesday 2pm_\n"
            "• _Kitchen fitting 15 April 9am 3 hours_\n"
            "• _Emergency callout tomorrow 8:30am_\n\n"
            "Type *CANCEL* to go back.",
        )
        return True

    if payload == "wiz_balance":
        await _wizard_action_balance(sender, session, client)
        return True

    if payload == "wiz_view_expenses":
        await _wizard_view_expenses(sender, session, client)
        return True

    if payload == "wiz_view_bookings":
        await _wizard_view_bookings(sender, session, client)
        return True

    if payload == "wiz_dashboard":
        biz = supabase.table("businesses").select("*").eq(
            "id", session["business_id"]
        ).execute()
        business = biz.data[0] if biz.data else {}
        _wizard_end_session(sender)
        await _cmd_login(sender, business, client)
        return True

    if payload == "wiz_channel":
        session["state"] = "choose_channel"
        await send_interactive_list(
            client, sender,
            "📲 *Choose delivery channel*\n\n"
            "How should we send to this customer?",
            "Select Channel",
            [{"title": "Channels", "rows": [
                {"id": "wiz_ch_whatsapp", "title": "📱 WhatsApp", "description": "Send via WhatsApp (default)"},
                {"id": "wiz_ch_email", "title": "📧 Email", "description": "Send via email (SendGrid)"},
                {"id": "wiz_ch_sms", "title": "💬 SMS", "description": "Send via text message"},
            ]}],
        )
        return True

    if payload == "wiz_ch_whatsapp":
        session["channel"] = "whatsapp"
        session["state"] = "choose_action"
        ch_label = _CHANNEL_LABELS["whatsapp"]
        await send_interactive_list(
            client, sender,
            f"✅ Channel set to *{ch_label}*\n\nWhat would you like to do?",
            "Choose an option",
            [{"title": "Actions", "rows": _action_menu_rows("whatsapp", sender)}],
        )
        return True

    if payload == "wiz_ch_email":
        # Check if customer has an email on file
        cust_phone = session.get("customer_phone", "")
        cust_result = (
            supabase.table("customers")
            .select("email")
            .eq("business_id", session["business_id"])
            .eq("phone_number", cust_phone)
            .limit(1)
            .execute()
        )
        existing_email = ""
        if cust_result.data:
            existing_email = cust_result.data[0].get("email", "")

        if existing_email:
            session["channel"] = "email"
            session["customer_email"] = existing_email
            session["state"] = "choose_action"
            ch_label = _CHANNEL_LABELS["email"]
            await send_interactive_list(
                client, sender,
                f"✅ Channel set to *{ch_label}*\n"
                f"Email: {existing_email}\n\nWhat would you like to do?",
                "Choose an option",
                [{"title": "Actions", "rows": _action_menu_rows("email", sender)}],
            )
        else:
            session["state"] = "awaiting_customer_email"
            await send_text_message(
                client, sender,
                "📧 No email on file for this customer.\n\n"
                "Please type the customer's *email address*:",
            )
        return True

    if payload == "wiz_ch_sms":
        session["channel"] = "sms"
        session["state"] = "choose_action"
        ch_label = _CHANNEL_LABELS["sms"]
        await send_interactive_list(
            client, sender,
            f"✅ Channel set to *{ch_label}*\n\nWhat would you like to do?",
            "Choose an option",
            [{"title": "Actions", "rows": _action_menu_rows("sms", sender)}],
        )
        return True

    return False


async def _wizard_new_customer_input(
    sender: str, text: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Parse name + phone from user input and save customer."""
    parsed = parse_review_command(text)
    if not parsed:
        await send_text_message(
            client, sender,
            "\u26a0\ufe0f I couldn't read that.\n\n"
            "Please type the customer's *name* and *phone number*.\n"
            "Example: *John Smith 07845774563*",
        )
        return

    supabase = get_supabase()
    supabase.table("customers").upsert(
        {
            "business_id": business["id"],
            "phone_number": parsed.phone,
            "name": parsed.name,
            "status": "active",
        },
        on_conflict="business_id,phone_number",
    ).execute()

    # Set as active customer
    supabase.table("businesses").update(
        {"active_customer_phone": parsed.phone}
    ).eq("id", business["id"]).execute()

    session["customer_phone"] = parsed.phone
    session["customer_name"] = parsed.name
    session["state"] = "choose_action"
    session.setdefault("channel", "whatsapp")

    await send_interactive_list(
        client, sender,
        f"\u2705 *{parsed.name}* ({parsed.phone}) added!\n\n"
        f"What would you like to do?",
        "Choose an option",
        [{"title": "Actions", "rows": _action_menu_rows(session["channel"], sender)}],
    )


async def _wizard_email_input(
    sender: str, text: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Save customer email and switch channel to email."""
    email = text.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        await send_text_message(
            client, sender,
            "⚠️ That doesn't look like a valid email address.\n\n"
            "Please type the customer's *email address*:",
        )
        return

    supabase = get_supabase()
    cust_phone = session.get("customer_phone", "")
    supabase.table("customers").update({"email": email}).eq(
        "business_id", business["id"]
    ).eq("phone_number", cust_phone).execute()

    session["channel"] = "email"
    session["customer_email"] = email
    session["state"] = "choose_action"
    ch_label = _CHANNEL_LABELS["email"]

    await send_interactive_list(
        client, sender,
        f"✅ Email saved! Channel set to *{ch_label}*\n"
        f"Email: {email}\n\nWhat would you like to do?",
        "Choose an option",
        [{"title": "Actions", "rows": _action_menu_rows("email", sender)}],
    )


async def _wizard_show_existing_customers(
    sender: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Show customer list for selection (buttons if <=3, list if more)."""
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
        await send_text_message(
            client, sender,
            "You don't have any customers yet.\n\n"
            "Please type the customer's *name* and *phone number*.\n"
            "Example: *John Smith 07845774563*",
        )
        return

    session["state"] = "awaiting_customer_pick"

    if len(custs.data) <= 3:
        buttons = [
            {"id": f"wiz_cust_{c['id']}", "title": c["name"][:20]}
            for c in custs.data
        ]
        await send_interactive_buttons(
            client, sender, "Which customer?", buttons,
        )
    else:
        rows = [
            {
                "id": f"wiz_cust_{c['id']}",
                "title": c["name"][:24],
                "description": c["phone_number"],
            }
            for c in custs.data[:10]
        ]
        await send_interactive_list(
            client, sender,
            "Which customer is this for?",
            "Select Customer",
            [{"title": "Customers", "rows": rows}],
        )


async def _wizard_customer_selected(
    sender: str, customer_id: str, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """A customer was picked from the button/list — show action menu."""
    session = _wizard_sessions.get(sender)
    if not session:
        return

    supabase = get_supabase()
    cust_result = (
        supabase.table("customers")
        .select("id, name, phone_number")
        .eq("id", customer_id)
        .limit(1)
        .execute()
    )
    if not cust_result.data:
        await send_text_message(client, sender, "\u26a0\ufe0f Customer not found.")
        return

    cust = cust_result.data[0]
    supabase.table("businesses").update(
        {"active_customer_phone": cust["phone_number"]}
    ).eq("id", business["id"]).execute()

    session["customer_phone"] = cust["phone_number"]
    session["customer_name"] = cust["name"]
    session["state"] = "choose_action"
    session.setdefault("channel", "whatsapp")

    # Demo first-run: skip action menu, go straight to review flow
    demo = _demo_sessions.get(sender)
    if demo and not demo.get("review_done"):
        await send_text_message(
            client, sender,
            f"✅ Selected *{cust['name']}*\n\n"
            "Now let's send them a review request.\n"
            "If there's an invoice on file, you can personalise it with the job details:",
        )
        await _wizard_review_check_invoices(sender, session, business, client)
        return

    await send_interactive_list(
        client, sender,
        f"\u2705 Selected *{cust['name']}*\n\nWhat would you like to do?",
        "Choose an option",
        [{"title": "Actions", "rows": _action_menu_rows(session["channel"], sender)}],
    )


async def _wizard_action_balance(
    sender: str, session: dict, client: httpx.AsyncClient
) -> None:
    """Show account balance: total invoiced, paid, and outstanding."""
    supabase = get_supabase()
    biz_id = session["business_id"]

    invoices = (
        supabase.table("invoices")
        .select("total, status, paid_at")
        .eq("business_id", biz_id)
        .execute()
    )

    total_invoiced = 0.0
    total_paid = 0.0
    outstanding = 0.0
    num_invoices = 0
    num_paid = 0
    num_outstanding = 0

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

    quotes = (
        supabase.table("quotes")
        .select("total, status")
        .eq("business_id", biz_id)
        .execute()
    )
    num_quotes = len(quotes.data or [])
    quotes_total = sum((q.get("total", 0) or 0) for q in (quotes.data or []))

    msg = (
        f"\U0001f4b0 *Account Balance*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"\U0001f4cb *Invoices:* {num_invoices}\n"
        f"\U0001f4b7 *Total Invoiced:* £{total_invoiced:,.2f}\n"
        f"\u2705 *Paid:* £{total_paid:,.2f} ({num_paid})\n"
        f"\u23f3 *Outstanding:* £{outstanding:,.2f} ({num_outstanding})\n\n"
        f"\U0001f4dd *Quotes Sent:* {num_quotes} (£{quotes_total:,.2f})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"What would you like to do next?"
    )

    await send_interactive_list(
        client, sender, msg,
        "Choose an option",
        [{"title": "Actions", "rows": _action_menu_rows(session.get("channel", "whatsapp"), sender)}],
    )


async def _wizard_review_check_invoices(
    sender: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Check if the customer has invoices — if so, offer to personalise the review request."""
    supabase = get_supabase()
    customer_phone = session.get("customer_phone", "")
    customer_name = session.get("customer_name", "Customer")

    # Look up customer ID
    cust = (
        supabase.table("customers")
        .select("id")
        .eq("business_id", business["id"])
        .eq("phone_number", customer_phone)
        .limit(1)
        .execute()
    )
    cust_id = cust.data[0]["id"] if cust.data else None

    if cust_id:
        invoices = (
            supabase.table("invoices")
            .select("id, invoice_number, total, created_at")
            .eq("business_id", business["id"])
            .eq("customer_id", cust_id)
            .execute()
        )
    else:
        invoices = type("R", (), {"data": []})()

    if invoices.data:
        # Fetch line items for each invoice to show job descriptions
        rows = []
        for inv in invoices.data[:9]:  # Max 9 + skip = 10 rows
            items = (
                supabase.table("line_items")
                .select("description")
                .eq("parent_id", inv["id"])
                .eq("parent_type", "invoice")
                .execute()
            )
            desc = ", ".join(
                it["description"] for it in (items.data or []) if it.get("description")
            )[:70] or "Invoice"
            date_str = inv["created_at"][:10] if inv.get("created_at") else ""
            total = inv.get("total", 0) or 0
            rows.append({
                "id": f"wiz_rev_inv_{inv['id']}",
                "title": f"#{inv['invoice_number']} — £{total:,.2f}",
                "description": f"{desc} ({date_str})" if date_str else desc,
            })

        rows.append({
            "id": "wiz_rev_skip",
            "title": "Skip — send generic",
            "description": "Send without job details",
        })

        await send_interactive_list(
            client, sender,
            f"📋 *{customer_name}* has {len(invoices.data)} invoice(s).\n\n"
            f"Pick one to personalise the review request with the job details, "
            f"or skip for a generic message.",
            "Select Invoice",
            [{"title": "Invoices", "rows": rows}],
        )
    else:
        # No invoices — go straight to generic review request
        session["review_job_description"] = ""
        await _wizard_action_review(sender, business, client)


async def _wizard_action_review(
    sender: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Send a review request to the selected customer."""
    session = _wizard_sessions.get(sender)
    if not session:
        return

    customer_phone = session.get("customer_phone", "")
    customer_name = session.get("customer_name", "Customer")
    channel = session.get("channel", "whatsapp")
    customer_email = session.get("customer_email", "")

    supabase = get_supabase()
    supabase.table("customers").upsert(
        {
            "business_id": business["id"],
            "phone_number": customer_phone,
            "name": customer_name,
            "status": "request_sent",
            "review_requested_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="business_id,phone_number",
    ).execute()

    first_name = customer_name.split()[0]
    biz_name = business["business_name"]
    review_link = business.get("google_review_link", "")
    job_desc = session.get("review_job_description", "")
    ch_label = _CHANNEL_LABELS.get(channel, "WhatsApp")
    sent_via = ""

    # Build personalised snippet if job details are available
    job_snippet = f" for the {job_desc}" if job_desc else ""
    job_thanks = (
        f"We hope you're happy with the {job_desc} we completed for you. "
        if job_desc
        else ""
    )

    # ── Demo mode: show delivery channel confirmation before sending ──
    if sender in _demo_sessions:
        session["state"] = "demo_confirm_review_send"
        await send_text_message(
            client, sender,
            f"📤 *Delivery method for {customer_name}:*\n\n"
            f"✅ 📱 *WhatsApp* — selected\n"
            f"☐ 📧 Email\n"
            f"☐ 💬 SMS\n\n"
            f"_You can change your default channel anytime from the main menu._",
        )
        await send_interactive_buttons(
            client, sender,
            f"Send review request to {customer_name} via WhatsApp?",
            [
                {"id": "demo_review_send_confirm", "title": "✅ Send Now"},
            ],
        )
        return

    # ── Content moderation check ──
    mod_warning = await moderate_outbound(f"{biz_name} {job_desc}")
    if mod_warning:
        await send_text_message(client, sender, mod_warning)
        return

    try:
        if channel == "email" and customer_email:
            subject, html_body, plain_body = build_review_email(
                customer_name=customer_name, first_name=first_name,
                biz_name=biz_name,
                review_link=review_link or "https://g.page/review",
                job_description=job_desc,
            )
            await send_email(client, customer_email, subject, html_body, plain_body)
            sent_via = f"via email to {customer_email}"
        elif channel == "sms":
            sms_body = build_review_sms(
                first_name=first_name, biz_name=biz_name,
                review_link=review_link or "https://g.page/review",
                job_description=job_desc,
            )
            await send_sms(client, customer_phone, sms_body)
            sent_via = f"via SMS to {customer_phone}"
        else:
            customer_raw = customer_phone.lstrip("+")
            try:
                await send_template_message(
                    client,
                    to_phone=customer_raw,
                    customer_name=first_name,
                    business_name=biz_name,
                )
            except Exception:
                logger.info("Template failed, falling back to interactive buttons for %s", customer_phone)
                await send_interactive_buttons(
                    client, customer_raw,
                    f"Hi {first_name}, thanks for choosing {biz_name}{job_snippet}! "
                    f"{job_thanks}"
                    f"How was your experience?",
                    [
                        {"id": "review_great", "title": "Great! ⭐"},
                        {"id": "could_be_better", "title": "Could be better"},
                    ],
                )
            sent_via = f"via WhatsApp to {customer_phone}"
    except Exception:
        logger.exception("Failed to send review request (%s) to %s", channel, customer_phone)
        await send_text_message(
            client, sender,
            f"⚠️ Failed to send review request to {customer_name} ({ch_label}).\n\n"
            f"Type /START to try again.",
        )
        _wizard_end_session(sender)
        return

    log_message(
        business_id=business["id"],
        to_phone=customer_phone,
        message_body=f"Review request sent to {customer_name} {sent_via}",
        message_type="review_request",
    )

    _wizard_end_session(sender)
    await send_text_message(
        client, sender,
        f"\u2705 Review request sent to *{customer_name}* ({sent_via})!\n\n"
        f"Session ended. Type /START for a new session.",
    )


async def _wizard_action_invoice(
    sender: str, session: dict, client: httpx.AsyncClient
) -> None:
    """Transition to invoice details input."""
    session["state"] = "awaiting_invoice_details"
    _start_timeout(sender, client)
    await send_text_message(
        client, sender,
        "\U0001f4dd *Invoice*\n\n"
        "Please type the *job description* and *total cost*.\n\n"
        "Example: *Boiler repair 250*\n"
        "Example: *Emergency callout + new tap \u00a3180*",
    )


async def _wizard_action_quote(
    sender: str, session: dict, client: httpx.AsyncClient
) -> None:
    """Transition to quote details input."""
    session["state"] = "awaiting_quote_details"
    _start_timeout(sender, client)
    await send_text_message(
        client, sender,
        "\U0001f4dd *Quote*\n\n"
        "Please type the *job description* and *estimated cost*.\n\n"
        "Example: *Full bathroom refit 2500*\n"
        "Example: *New boiler installation \u00a31800*",
    )


async def _wizard_invoice_input(
    sender: str, text: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Parse invoice details from free text."""
    amount, description = _parse_invoice_args(text)

    # If they previously gave just an amount, treat this text as description
    if amount is None and session.get("pending_amount") and text.strip():
        description = text.strip()
        amount = session.pop("pending_amount")

    if amount is None:
        await send_text_message(
            client, sender,
            "\u26a0\ufe0f I couldn't find an amount.\n\n"
            "Please include the *cost* and *description*.\n"
            "Example: *Boiler repair 250*\n\n"
            "Type *CANCEL* to cancel.",
        )
        return

    if not description:
        session["pending_amount"] = amount
        await send_text_message(
            client, sender,
            f"\u2705 Amount: \u00a3{amount:.2f}\n\n"
            f"Now type a short *description* of the job.\n"
            f"Example: *Boiler repair*",
        )
        return

    # Force confirm-before-send so they can review before sending
    business["confirm_before_send"] = True
    session["state"] = "confirm_invoice"
    await _finalise_invoice(sender, amount, description, business, client)


async def _wizard_quote_input(
    sender: str, text: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Parse quote details from free text."""
    amount, description = _parse_invoice_args(text)

    if amount is None and session.get("pending_amount") and text.strip():
        description = text.strip()
        amount = session.pop("pending_amount")

    if amount is None:
        await send_text_message(
            client, sender,
            "\u26a0\ufe0f I couldn't find an amount.\n\n"
            "Please include the *estimated cost* and *description*.\n"
            "Example: *Full bathroom refit 2500*\n\n"
            "Type *CANCEL* to cancel.",
        )
        return

    if not description:
        session["pending_amount"] = amount
        await send_text_message(
            client, sender,
            f"\u2705 Amount: \u00a3{amount:.2f}\n\n"
            f"Now type a short *description* of the work.\n"
            f"Example: *Full bathroom refit*",
        )
        return

    business["confirm_before_send"] = True
    session["state"] = "confirm_quote"
    await _finalise_quote(sender, amount, description, business, client)


# ──────────────────────────────────────────────
# /LOGIN
# ──────────────────────────────────────────────
async def _cmd_login(sender: str, business: dict[str, Any], client: httpx.AsyncClient) -> None:
    """Send a direct login link to the dashboard."""
    settings = get_settings()

    base = settings.base_url.rstrip("/")
    login_url = f"{base}/login.html"

    msg = (
        f"\U0001f449 Open your dashboard:\n{login_url}\n\n"
        f"Tap the link, enter your phone number, and we'll send you a login code."
    )
    await send_text_message(client, sender, msg)


# ──────────────────────────────────────────────
# Invoice / Quote creation helpers
# ──────────────────────────────────────────────

def _parse_invoice_args(args: str) -> tuple[float | None, str]:
    """Parse amount and description from command arguments.

    Accepts flexible formats like:
      250 Plumbing work
      \u00a3250 Plumbing
      Plumbing work 250
    Returns (amount_or_None, description_string).
    """
    args = args.strip()
    if not args:
        return None, ""

    # Find a monetary amount anywhere in the string
    m = re.search(r"[\u00a3$]?\s*([\d,]+(?:\.\d{1,2})?)\b", args)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            return None, args
        desc = (args[:m.start()] + " " + args[m.end():]).strip()
        desc = re.sub(r"\s{2,}", " ", desc)
        desc = re.sub(r"^[\u00a3$]\s*", "", desc).strip()
        return amount, desc

    return None, args


async def _finalise_invoice(
    sender: str, amount: float, description: str,
    business: dict[str, Any], client: httpx.AsyncClient,
) -> None:
    """Create a real invoice in the DB and notify both parties."""
    from app.api.member import _next_number, _get_business_tax_rate

    supabase = get_supabase()
    customer_phone = business.get("active_customer_phone", "")

    cust_result = (
        supabase.table("customers")
        .select("id, name, phone_number")
        .eq("business_id", business["id"])
        .eq("phone_number", customer_phone)
        .limit(1)
        .execute()
    )
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_id = customer["id"] if customer else ""
    first_name = customer_name.split()[0]
    biz_name = business["business_name"]

    # ── Create invoice in DB ──
    tax_rate = _get_business_tax_rate(supabase, business["id"])
    inv_number = _next_number(supabase, business["id"], "invoices", "INV")
    subtotal = round(amount, 2)
    tax_amount = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)
    currency = business.get("currency", "GBP")

    inv_result = supabase.table("invoices").insert({
        "business_id": business["id"],
        "customer_id": customer_id,
        "invoice_number": inv_number,
        "status": "draft",
        "subtotal": subtotal,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "total": total,
        "currency": currency,
        "payment_terms": business.get("default_payment_terms", "Payment due within 14 days"),
        "notes": description,
    }).execute()
    inv = inv_result.data[0]

    # Add single line item
    supabase.table("line_items").insert({
        "parent_id": inv["id"],
        "parent_type": "invoice",
        "description": description,
        "quantity": 1,
        "unit_price": subtotal,
        "total": subtotal,
        "sort_order": 0,
    }).execute()

    sym = "\u00a3" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "

    settings = get_settings()
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/invoices/{inv['id']}/pdf"

    # Get channel from wizard session
    session = _wizard_sessions.get(sender, {})
    channel = session.get("channel", "whatsapp")
    customer_email = session.get("customer_email", "")
    ch_label = _CHANNEL_LABELS.get(channel, "📱 WhatsApp")

    # ── Always show preview with confirm/cancel ──
    if business.get("confirm_before_send"):
        preview_msg = (
            f"\U0001f50d *Invoice Preview \u2014 {inv_number}*\n"
            f"To: {customer_name} ({customer_phone})\n"
            f"📲 Sending via: {ch_label}\n\n"
            f"\u2022 {description}\n"
            f"\u2022 Subtotal: {sym}{subtotal:.2f}\n"
            f"\u2022 VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
            f"\u2022 *Total: {sym}{total:.2f}*\n\n"
            f"\U0001f4ce PDF: {pdf_url}\n\n"
            f"Happy with this? Tap *Send* to deliver it to {first_name}, "
            f"or *Cancel* to discard."
        )
        try:
            resp = await send_interactive_buttons(client, sender, preview_msg, [
                {"id": f"sendinv_{inv['id']}", "title": "\u2705 Send"},
                {"id": f"cancelinv_{inv['id']}", "title": "\u274c Cancel"},
            ])
            logger.info(
                "INVOICE PREVIEW sent: biz=%s inv=%s total=%s resp=%s",
                business["id"], inv_number, total, resp,
            )
        except Exception:
            logger.exception("INVOICE PREVIEW SEND FAILED for inv=%s", inv_number)
        return

    # ── No confirmation — send straight away ──
    await _send_invoice_to_customer(
        sender, inv, invoice_number=inv_number, description=description,
        subtotal=subtotal, tax_rate=tax_rate, tax_amount=tax_amount,
        total=total, sym=sym,
        customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=biz_name,
        business=business, client=client,
        channel=channel, customer_email=customer_email,
    )


async def _send_invoice_to_customer(
    sender: str, inv: dict, *, invoice_number: str, description: str,
    subtotal: float, tax_rate: float, tax_amount: float, total: float,
    sym: str, customer_name: str, customer_phone: str,
    first_name: str, biz_name: str,
    business: dict[str, Any], client: httpx.AsyncClient,
    channel: str = "whatsapp", customer_email: str = "",
) -> None:
    """Deliver the invoice to the customer and confirm to the tradesperson."""
    supabase = get_supabase()
    settings = get_settings()
    personal_phone = _format_phone_display(business.get("phone_number", ""))
    customer_raw = customer_phone.lstrip("+")
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/invoices/{inv['id']}/pdf"

    ch_label = _CHANNEL_LABELS.get(channel, "WhatsApp")
    sent_via = ""

    # ── Demo mode: show POV instead of actually sending ──
    if sender in _demo_sessions:
        session = _wizard_sessions.get(sender)
        await send_text_message(
            client, sender,
            f"📱 *YOUR CUSTOMER ({customer_name}) would receive:*\n\n"
            f"\"Hi {first_name}, here is your invoice from {biz_name}:\n\n"
            f"📄 *Invoice {invoice_number}*\n"
            f"• {description}\n"
            f"• Subtotal: {sym}{subtotal:.2f}\n"
            f"• VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
            f"• *Total: {sym}{total:.2f}*\n\n"
            f"📎 Download PDF: {pdf_url}\"\n\n"
            f"_The PDF is real — tap the link to see it!_",
        )
        await send_text_message(
            client, sender,
            f"🔔 *YOU would see:*\n\n"
            f"\"✅ Invoice {invoice_number} sent to {customer_name} "
            f"for {sym}{total:.2f}\"\n\n"
            f"_It appears in your dashboard under Outstanding invoices._",
        )
        supabase.table("invoices").update({
            "status": "sent",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", inv["id"]).execute()
        if session:
            session["state"] = "choose_action"
            await send_interactive_list(
                client, sender,
                "What would you like to try next?",
                "Choose an option",
                [{"title": "Actions", "rows": _demo_action_menu_rows()}],
            )
        else:
            _wizard_end_session(sender)
        return

    # ── Content moderation check ──
    mod_warning = await moderate_outbound(f"{description} {biz_name}")
    if mod_warning:
        await send_text_message(client, sender, mod_warning)
        return

    try:
        if channel == "email" and customer_email:
            subject, html_body, plain_body = build_invoice_email(
                customer_name=customer_name, first_name=first_name,
                biz_name=biz_name, invoice_number=invoice_number,
                description=description, subtotal=subtotal,
                tax_rate=tax_rate, tax_amount=tax_amount,
                total=total, sym=sym, pdf_url=pdf_url,
                personal_phone=personal_phone,
            )
            await send_email(client, customer_email, subject, html_body, plain_body)
            sent_via = f"via email to {customer_email}"
        elif channel == "sms":
            sms_body = build_invoice_sms(
                first_name=first_name, biz_name=biz_name,
                invoice_number=invoice_number, description=description,
                total=total, sym=sym, pdf_url=pdf_url,
                personal_phone=personal_phone,
            )
            await send_sms(client, customer_phone, sms_body)
            sent_via = f"via SMS to {customer_phone}"
        else:
            invoice_msg = (
                f"Hi {first_name}, here is your invoice from {biz_name}:\n\n"
                f"\U0001f4c4 *Invoice {invoice_number}*\n"
                f"\u2022 {description}\n"
                f"\u2022 Subtotal: {sym}{subtotal:.2f}\n"
                f"\u2022 VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
                f"\u2022 *Total: {sym}{total:.2f}*\n\n"
                f"\U0001f4ce Download PDF: {pdf_url}\n\n"
                f"\U0001f4b3 Payment details will follow shortly.\n\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"\u26a0\ufe0f This is an automated message. Please do not reply here.\n"
                f"To discuss this job, contact {biz_name} on *{personal_phone}*"
            )
            await send_text_message(client, customer_raw, invoice_msg)
            sent_via = f"via WhatsApp to {customer_phone}"
    except Exception:
        logger.exception("Failed to send invoice (%s) to %s", channel, customer_phone)
        await send_text_message(
            client, sender,
            f"\u2705 Invoice {invoice_number} created for {sym}{total:.2f}, "
            f"but failed to send to {customer_name} ({ch_label}). "
            f"You can send the PDF from your dashboard.",
        )
        return

    supabase.table("invoices").update({
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", inv["id"]).execute()

    log_message(
        business_id=business["id"],
        to_phone=customer_phone,
        message_body=f"Invoice {invoice_number} sent {sent_via}",
        message_type="invoice",
    )

    await send_text_message(
        client, sender,
        f"\u2705 Invoice {invoice_number} sent to {customer_name} "
        f"({sent_via}).\n\n"
        f"\u2022 {description}: {sym}{total:.2f} (inc. VAT)\n\n"
        f"Session ended. Type /START for a new session.",
    )
    _wizard_end_session(sender)
    logger.info(
        "INVOICE: biz=%s customer=%s inv=%s total=%s channel=%s",
        business["id"], customer_phone, invoice_number, total, channel,
    )


async def _finalise_quote(
    sender: str, amount: float, description: str,
    business: dict[str, Any], client: httpx.AsyncClient,
) -> None:
    """Create a real quote in the DB and notify both parties."""
    from app.api.member import _next_number, _get_business_tax_rate

    supabase = get_supabase()
    customer_phone = business.get("active_customer_phone", "")

    cust_result = (
        supabase.table("customers")
        .select("id, name, phone_number")
        .eq("business_id", business["id"])
        .eq("phone_number", customer_phone)
        .limit(1)
        .execute()
    )
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_id = customer["id"] if customer else ""
    first_name = customer_name.split()[0]
    biz_name = business["business_name"]

    tax_rate = _get_business_tax_rate(supabase, business["id"])
    quo_number = _next_number(supabase, business["id"], "quotes", "QUO")
    subtotal = round(amount, 2)
    tax_amount = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)
    currency = business.get("currency", "GBP")

    valid_until = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")

    quo_result = supabase.table("quotes").insert({
        "business_id": business["id"],
        "customer_id": customer_id,
        "quote_number": quo_number,
        "status": "draft",
        "subtotal": subtotal,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "total": total,
        "currency": currency,
        "valid_until": valid_until,
        "notes": description,
    }).execute()
    quo = quo_result.data[0]

    supabase.table("line_items").insert({
        "parent_id": quo["id"],
        "parent_type": "quote",
        "description": description,
        "quantity": 1,
        "unit_price": subtotal,
        "total": subtotal,
        "sort_order": 0,
    }).execute()

    sym = "\u00a3" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "

    settings = get_settings()
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/quotes/{quo['id']}/pdf"

    # Get channel from wizard session
    session = _wizard_sessions.get(sender, {})
    channel = session.get("channel", "whatsapp")
    customer_email = session.get("customer_email", "")
    ch_label = _CHANNEL_LABELS.get(channel, "📱 WhatsApp")

    # ── Always show preview with confirm/cancel ──
    if business.get("confirm_before_send"):
        preview_msg = (
            f"\U0001f50d *Quote Preview \u2014 {quo_number}*\n"
            f"To: {customer_name} ({customer_phone})\n"
            f"📲 Sending via: {ch_label}\n\n"
            f"\u2022 {description}\n"
            f"\u2022 Subtotal: {sym}{subtotal:.2f}\n"
            f"\u2022 VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
            f"\u2022 *Total: {sym}{total:.2f}*\n"
            f"\u2022 Valid until: {valid_until}\n\n"
            f"\U0001f4ce PDF: {pdf_url}\n\n"
            f"Happy with this? Tap *Send* to deliver it to {first_name}, "
            f"or *Cancel* to discard."
        )
        await send_interactive_buttons(client, sender, preview_msg, [
            {"id": f"sendquo_{quo['id']}", "title": "\u2705 Send"},
            {"id": f"cancelquo_{quo['id']}", "title": "\u274c Cancel"},
        ])
        logger.info(
            "QUOTE PREVIEW: biz=%s quo=%s total=%s (awaiting confirm)",
            business["id"], quo_number, total,
        )
        return

    # ── No confirmation — send straight away ──
    await _send_quote_to_customer(
        sender, quo, quote_number=quo_number, description=description,
        subtotal=subtotal, tax_rate=tax_rate, tax_amount=tax_amount,
        total=total, sym=sym, valid_until=valid_until,
        customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=biz_name,
        business=business, client=client,
        channel=channel, customer_email=customer_email,
    )


async def _send_quote_to_customer(
    sender: str, quo: dict, *, quote_number: str, description: str,
    subtotal: float, tax_rate: float, tax_amount: float, total: float,
    sym: str, valid_until: str,
    customer_name: str, customer_phone: str,
    first_name: str, biz_name: str,
    business: dict[str, Any], client: httpx.AsyncClient,
    channel: str = "whatsapp", customer_email: str = "",
) -> None:
    """Deliver the quote to the customer and confirm to the tradesperson."""
    supabase = get_supabase()
    settings = get_settings()
    personal_phone = _format_phone_display(business.get("phone_number", ""))
    customer_raw = customer_phone.lstrip("+")
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/quotes/{quo['id']}/pdf"

    ch_label = _CHANNEL_LABELS.get(channel, "WhatsApp")
    sent_via = ""

    # ── Demo mode: show POV instead of actually sending ──
    if sender in _demo_sessions:
        session = _wizard_sessions.get(sender)
        await send_text_message(
            client, sender,
            f"📱 *YOUR CUSTOMER ({customer_name}) would receive:*\n\n"
            f"\"Hi {first_name}, here is a quote from {biz_name}:\n\n"
            f"📄 *Quote {quote_number}*\n"
            f"• {description}\n"
            f"• Subtotal: {sym}{subtotal:.2f}\n"
            f"• VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
            f"• *Total: {sym}{total:.2f}*\n"
            f"• Valid until: {valid_until}\n\n"
            f"📎 Download PDF: {pdf_url}\"\n\n"
            f"_The PDF is real — tap the link to see it!_",
        )
        await send_text_message(
            client, sender,
            f"🔔 *YOU would see:*\n\n"
            f"\"✅ Quote {quote_number} sent to {customer_name} "
            f"for {sym}{total:.2f}\"\n\n"
            f"_It appears in your dashboard under Quotes._",
        )
        supabase.table("quotes").update({
            "status": "sent",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", quo["id"]).execute()
        if session:
            session["state"] = "choose_action"
            await send_interactive_list(
                client, sender,
                "What would you like to try next?",
                "Choose an option",
                [{"title": "Actions", "rows": _demo_action_menu_rows()}],
            )
        else:
            _wizard_end_session(sender)
        return

    # ── Content moderation check ──
    mod_warning = await moderate_outbound(f"{description} {biz_name}")
    if mod_warning:
        await send_text_message(client, sender, mod_warning)
        return

    try:
        if channel == "email" and customer_email:
            subject, html_body, plain_body = build_quote_email(
                customer_name=customer_name, first_name=first_name,
                biz_name=biz_name, quote_number=quote_number,
                description=description, subtotal=subtotal,
                tax_rate=tax_rate, tax_amount=tax_amount,
                total=total, sym=sym, valid_until=valid_until,
                pdf_url=pdf_url, personal_phone=personal_phone,
            )
            await send_email(client, customer_email, subject, html_body, plain_body)
            sent_via = f"via email to {customer_email}"
        elif channel == "sms":
            sms_body = build_quote_sms(
                first_name=first_name, biz_name=biz_name,
                quote_number=quote_number, description=description,
                total=total, sym=sym, valid_until=valid_until,
                pdf_url=pdf_url, personal_phone=personal_phone,
            )
            await send_sms(client, customer_phone, sms_body)
            sent_via = f"via SMS to {customer_phone}"
        else:
            quote_msg = (
                f"Hi {first_name}, here is a quote from {biz_name}:\n\n"
                f"\U0001f4c4 *Quote {quote_number}*\n"
                f"\u2022 {description}\n"
                f"\u2022 Subtotal: {sym}{subtotal:.2f}\n"
                f"\u2022 VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
                f"\u2022 *Total: {sym}{total:.2f}*\n"
                f"\u2022 Valid until: {valid_until}\n\n"
                f"\U0001f4ce Download PDF: {pdf_url}\n\n"
                f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                f"\u26a0\ufe0f This is an automated message. Please do not reply here.\n"
                f"To discuss this job, contact {biz_name} on *{personal_phone}*"
            )
            await send_text_message(client, customer_raw, quote_msg)
            sent_via = f"via WhatsApp to {customer_phone}"
    except Exception:
        logger.exception("Failed to send quote (%s) to %s", channel, customer_phone)
        await send_text_message(
            client, sender,
            f"\u2705 Quote {quote_number} created for {sym}{total:.2f}, "
            f"but failed to send to {customer_name} ({ch_label}). "
            f"You can send the PDF from your dashboard.",
        )
        return

    supabase.table("quotes").update({
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", quo["id"]).execute()

    log_message(
        business_id=business["id"],
        to_phone=customer_phone,
        message_body=f"Quote {quote_number} sent {sent_via}",
        message_type="quote",
    )

    await send_text_message(
        client, sender,
        f"\u2705 Quote {quote_number} sent to {customer_name} "
        f"({sent_via}).\n\n"
        f"\u2022 {description}: {sym}{total:.2f} (inc. VAT)\n"
        f"\u2022 Valid until {valid_until}\n\n"
        f"Session ended. Type /START for a new session.",
    )
    _wizard_end_session(sender)
    logger.info(
        "QUOTE: biz=%s customer=%s quo=%s total=%s channel=%s",
        business["id"], customer_phone, quote_number, total, channel,
    )


# ──────────────────────────────────────────────
# View Expenses (WhatsApp summary)
# ──────────────────────────────────────────────
async def _wizard_view_expenses(
    sender: str, session: dict, client: httpx.AsyncClient
) -> None:
    """Show a WhatsApp-friendly expense summary."""
    supabase = get_supabase()
    expenses = (
        supabase.table("expenses")
        .select("*")
        .eq("business_id", session["business_id"])
        .order("date", desc=True)
        .execute()
    ).data or []

    if not expenses:
        await send_text_message(
            client, sender,
            "📊 *Expenses*\n\nNo expenses recorded yet.\n"
            "Send a receipt photo after choosing 🧾 Record Expense.",
        )
    else:
        total = sum(float(e.get("total", 0)) for e in expenses)
        tax = sum(float(e.get("tax_amount", 0)) for e in expenses)

        # This month
        now = datetime.now(timezone.utc)
        month_key = now.strftime("%Y-%m")
        month_total = sum(
            float(e.get("total", 0)) for e in expenses
            if (e.get("date", "") or "")[:7] == month_key
        )

        # Recent 5
        recent = expenses[:5]
        lines = []
        for e in recent:
            sym = "£" if e.get("currency", "GBP") == "GBP" else "$"
            lines.append(
                f"  • {e.get('date', '?')} — {e.get('vendor', '?')} — "
                f"{sym}{float(e.get('total', 0)):.2f}"
            )
        recent_text = "\n".join(lines)

        sym = "£"
        await send_text_message(
            client, sender,
            f"📊 *Expense Summary*\n\n"
            f"💰 *Total Spent:* {sym}{total:.2f}\n"
            f"🧾 *VAT (Reclaimable):* {sym}{tax:.2f}\n"
            f"📅 *This Month:* {sym}{month_total:.2f}\n"
            f"📋 *Receipts:* {len(expenses)}\n\n"
            f"📝 *Recent Expenses:*\n{recent_text}\n\n"
            f"View full details in your dashboard.",
        )

    # Return to action menu
    session["state"] = "choose_action"
    await send_interactive_list(
        client, sender,
        "What would you like to do next?",
        "Choose an option",
        [{"title": "Actions", "rows": _action_menu_rows(session.get("channel", "whatsapp"), sender)}],
    )


# ──────────────────────────────────────────────
# View Calendar (WhatsApp summary)
# ──────────────────────────────────────────────
async def _wizard_view_bookings(
    sender: str, session: dict, client: httpx.AsyncClient
) -> None:
    """Show a WhatsApp-friendly booking calendar summary."""
    supabase = get_supabase()
    bookings = (
        supabase.table("bookings")
        .select("*")
        .eq("business_id", session["business_id"])
        .order("date")
        .execute()
    ).data or []

    if not bookings:
        await send_text_message(
            client, sender,
            "🗓 *Your Calendar*\n\nNo bookings yet.\n"
            "Tap 📅 New Booking to add your first job.",
        )
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        upcoming = [
            b for b in bookings
            if (b.get("date", "") or "") >= today and b.get("status") != "cancelled"
        ]
        today_bks = [b for b in upcoming if b.get("date", "") == today]

        # Next 7 upcoming
        lines = []
        for b in upcoming[:7]:
            try:
                from datetime import datetime as _dt
                d = _dt.strptime(b["date"], "%Y-%m-%d")
                day_str = d.strftime("%a %d %b")
            except Exception:
                day_str = b.get("date", "?")
            time_str = b.get("time", "")
            lines.append(
                f"  • {day_str} {time_str} — {b.get('title', '?')}"
                + (f" ({b.get('customer_name', '')})" if b.get("customer_name") else "")
            )
        upcoming_text = "\n".join(lines) if lines else "  None"

        await send_text_message(
            client, sender,
            f"🗓 *Your Calendar*\n\n"
            f"📋 *Total Bookings:* {len(bookings)}\n"
            f"📅 *Today:* {len(today_bks)} job{'s' if len(today_bks) != 1 else ''}\n"
            f"⏳ *Upcoming:* {len(upcoming)}\n\n"
            f"📝 *Next Up:*\n{upcoming_text}\n\n"
            f"View full calendar in your dashboard.",
        )

    # Return to action menu
    session["state"] = "choose_action"
    await send_interactive_list(
        client, sender,
        "What would you like to do next?",
        "Choose an option",
        [{"title": "Actions", "rows": _action_menu_rows(session.get("channel", "whatsapp"), sender)}],
    )


# ──────────────────────────────────────────────
# Booking wizard handler
# ──────────────────────────────────────────────
async def _wizard_booking_name_input(
    sender: str, text: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Handle the customer name input for a booking when none was provided."""
    partial = session.get("pending_booking_partial")
    if not partial:
        session["state"] = "awaiting_booking_details"
        await send_text_message(client, sender, "⚠️ Something went wrong. Please enter the booking details again.")
        return

    if text.strip().upper() == "SKIP":
        customer_name = ""
    else:
        customer_name = text.strip()

    session["customer_name"] = customer_name
    session.pop("pending_booking_partial", None)

    # Now continue the booking flow with the name resolved — rebuild as if we just parsed
    session["pending_booking"] = {
        "title": partial["title"],
        "date": partial["date"],
        "time": partial["time"],
        "duration_mins": partial["duration_mins"],
        "notes": partial["notes"],
        "customer_id": session.get("customer_id") or None,
        "customer_name": customer_name,
        "customer_phone": session.get("customer_phone", ""),
    }
    session["state"] = "confirm_booking"

    # ── Check for clashing bookings ──
    clash_warning = ""
    date = partial["date"]
    time_ = partial["time"]
    duration = partial["duration_mins"]
    if date and time_:
        try:
            from datetime import datetime as _dt, timedelta as _td
            new_start = _dt.strptime(f"{date} {time_}", "%Y-%m-%d %H:%M")
            new_end = new_start + _td(minutes=duration)
            supabase = get_supabase()
            existing = (
                supabase.table("bookings")
                .select("title, date, time, duration_mins")
                .eq("business_id", session["business_id"])
                .eq("date", date)
                .neq("status", "cancelled")
                .execute()
            )
            for bk in (existing.data or []):
                bk_start = _dt.strptime(f"{bk['date']} {bk['time']}", "%Y-%m-%d %H:%M")
                bk_end = bk_start + _td(minutes=int(bk.get("duration_mins", 60)))
                if new_start < bk_end and bk_start < new_end:
                    clash_warning = (
                        f"\n\n⚠️ *Clash detected!* This overlaps with:\n"
                        f"📋 {bk['title']} — {bk['time']} ({bk.get('duration_mins', 60)} mins)"
                    )
                    break
        except Exception:
            logger.exception("Availability check failed for %s", sender)

    # Format friendly date for preview
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(date, "%Y-%m-%d")
        friendly_date = d.strftime("%A %d %B %Y")
    except Exception:
        friendly_date = date

    customer_line = f"\n👤 *Customer:* {customer_name}" if customer_name else ""
    notes_line = f"\n📝 *Notes:* {partial['notes']}" if partial["notes"] else ""

    preview_msg = (
        f"🔍 *Booking Preview*\n\n"
        f"📋 *Job:* {partial['title']}\n"
        f"📅 *Date:* {friendly_date}\n"
        f"🕐 *Time:* {time_}\n"
        f"⏱ *Duration:* {duration} mins"
        f"{customer_line}{notes_line}"
        f"{clash_warning}\n\n"
        f"Is this correct? Tap *Confirm* to save or *Cancel* to re-enter."
    )

    await send_interactive_buttons(
        client, sender, preview_msg,
        [
            {"type": "reply", "reply": {"id": "confirm_booking", "title": "✅ Confirm"}},
            {"type": "reply", "reply": {"id": "cancel_booking", "title": "❌ Cancel"}},
        ],
    )


async def _wizard_booking_input(
    sender: str, text: str, session: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Parse natural-language booking details via GPT and show preview for confirmation."""

    await send_text_message(
        client, sender,
        "📅 *Got it!* Parsing your booking...",
    )

    try:
        details = await parse_booking_details(text)
    except Exception:
        logger.exception("Failed to parse booking from %s", sender)
        await send_text_message(
            client, sender,
            "⚠️ Sorry, I couldn't understand that. Please try again.\n\n"
            "Example: *Boiler service Tuesday 2pm*\n\n"
            "Type *CANCEL* to go back.",
        )
        return

    title = details.get("title", text[:60])
    date = details.get("date", "")
    time_ = details.get("time", "09:00")
    duration = int(details.get("duration_mins", 60))
    notes = details.get("notes", "")
    parsed_name = details.get("customer_name", "").strip()

    # Use customer name from session (if set by earlier flow) or from GPT parse
    customer_name = session.get("customer_name", "") or parsed_name

    # If no customer name yet, ask for one
    if not customer_name:
        session["pending_booking_partial"] = {
            "title": title, "date": date, "time": time_,
            "duration_mins": duration, "notes": notes,
        }
        session["state"] = "awaiting_booking_name"
        await send_text_message(
            client, sender,
            "👤 *Who is this booking for?*\n\n"
            "Please type the customer's name.\n\n"
            "Type *SKIP* to save without a name, or *CANCEL* to go back.",
        )
        return

    # ── Check for clashing bookings ──
    clash_warning = ""
    if date and time_:
        try:
            from datetime import datetime as _dt, timedelta as _td
            new_start = _dt.strptime(f"{date} {time_}", "%Y-%m-%d %H:%M")
            new_end = new_start + _td(minutes=duration)

            supabase = get_supabase()
            existing = (
                supabase.table("bookings")
                .select("title, date, time, duration_mins")
                .eq("business_id", session["business_id"])
                .eq("date", date)
                .neq("status", "cancelled")
                .execute()
            )
            for bk in (existing.data or []):
                bk_start = _dt.strptime(f"{bk['date']} {bk['time']}", "%Y-%m-%d %H:%M")
                bk_end = bk_start + _td(minutes=int(bk.get("duration_mins", 60)))
                if new_start < bk_end and bk_start < new_end:
                    clash_warning = (
                        f"\n\n⚠️ *Clash detected!* This overlaps with:\n"
                        f"📋 {bk['title']} — {bk['time']} ({bk.get('duration_mins', 60)} mins)"
                    )
                    break
        except Exception:
            logger.exception("Availability check failed for %s", sender)

    # Store parsed booking in session for confirmation
    session["pending_booking"] = {
        "title": title,
        "date": date,
        "time": time_,
        "duration_mins": duration,
        "notes": notes,
        "customer_id": session.get("customer_id") or None,
        "customer_name": customer_name,
        "customer_phone": session.get("customer_phone", ""),
    }
    session["state"] = "confirm_booking"

    # Format friendly date for preview
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(date, "%Y-%m-%d")
        friendly_date = d.strftime("%A %d %B %Y")
    except Exception:
        friendly_date = date

    customer_line = f"\n👤 *Customer:* {session.get('customer_name', '')}" if session.get("customer_name") else ""
    notes_line = f"\n📝 *Notes:* {notes}" if notes else ""

    preview_msg = (
        f"🔍 *Booking Preview*\n\n"
        f"📋 *Job:* {title}\n"
        f"📅 *Date:* {friendly_date}\n"
        f"🕐 *Time:* {time_}\n"
        f"⏱ *Duration:* {duration} mins"
        f"{customer_line}{notes_line}"
        f"{clash_warning}\n\n"
        f"Is this correct? Tap *Confirm* to save or *Cancel* to re-enter."
    )

    await send_interactive_buttons(client, sender, preview_msg, [
        {"id": "confirmbk", "title": "✅ Confirm"},
        {"id": "cancelbk", "title": "❌ Cancel"},
    ])

    logger.info(
        "BOOKING PREVIEW: biz=%s title=%s date=%s time=%s",
        session["business_id"], title, date, time_,
    )


# ──────────────────────────────────────────────
# Booking confirm / cancel handlers
# ──────────────────────────────────────────────
async def _confirm_booking(sender: str, client: httpx.AsyncClient) -> None:
    """Save the pending booking from the session to the database."""
    import uuid as _uuid

    session = _wizard_sessions.get(sender)
    if not session or session.get("state") != "confirm_booking":
        await send_text_message(client, sender, "⚠️ No pending booking to confirm.")
        return

    pb = session.get("pending_booking")
    if not pb:
        await send_text_message(client, sender, "⚠️ No pending booking found.")
        return

    try:
        supabase = get_supabase()
        now = datetime.now(timezone.utc).isoformat()
        booking_id = str(_uuid.uuid4())

        supabase.table("bookings").insert({
            "id": booking_id,
            "business_id": session["business_id"],
            "customer_id": pb["customer_id"],
            "customer_name": pb["customer_name"],
            "customer_phone": pb["customer_phone"],
            "title": pb["title"],
            "date": pb["date"],
            "time": pb["time"],
            "duration_mins": pb["duration_mins"],
            "notes": pb["notes"],
            "status": "confirmed",
            "created_at": now,
            "updated_at": now,
        }).execute()
    except Exception:
        logger.exception("Failed to save booking from %s", sender)
        await send_text_message(
            client, sender,
            "⚠️ Booking failed to save. Please try again.\n\n"
            "Type *CANCEL* to go back.",
        )
        session["state"] = "awaiting_booking_details"
        return

    # Format friendly date
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(pb["date"], "%Y-%m-%d")
        friendly_date = d.strftime("%A %d %B %Y")
    except Exception:
        friendly_date = pb["date"]

    customer_line = f"\n👤 *Customer:* {pb['customer_name']}" if pb["customer_name"] else ""
    notes_line = f"\n📝 *Notes:* {pb['notes']}" if pb["notes"] else ""

    await send_text_message(
        client, sender,
        f"✅ *Booking confirmed!*\n\n"
        f"📋 *Job:* {pb['title']}\n"
        f"📅 *Date:* {friendly_date}\n"
        f"🕐 *Time:* {pb['time']}\n"
        f"⏱ *Duration:* {pb['duration_mins']} mins"
        f"{customer_line}{notes_line}\n\n"
        f"View all bookings in your dashboard.",
    )

    # Clean up and return to action menu
    session.pop("pending_booking", None)
    session["state"] = "choose_action"
    await send_interactive_list(
        client, sender,
        "What would you like to do next?",
        "Choose an option",
        [{"title": "Actions", "rows": _action_menu_rows(session.get("channel", "whatsapp"), sender)}],
    )

    sender_e164 = f"+{sender}" if not sender.startswith("+") else sender
    log_message(
        business_id=session["business_id"],
        to_phone=sender_e164,
        message_body=f"Booking created: {pb['title']} — {friendly_date} {pb['time']}",
        message_type="booking",
    )

    logger.info(
        "BOOKING CONFIRMED: biz=%s title=%s date=%s time=%s",
        session["business_id"], pb["title"], pb["date"], pb["time"],
    )


async def _cancel_booking(sender: str, client: httpx.AsyncClient) -> None:
    """Cancel the pending booking and return to booking input."""
    session = _wizard_sessions.get(sender)
    if session:
        session.pop("pending_booking", None)
        session["state"] = "awaiting_booking_details"

    await send_text_message(
        client, sender,
        "❌ Booking cancelled. Please type the booking details again.\n\n"
        "Example: *Boiler service 15th July 2pm*\n\n"
        "Type *CANCEL* to go back to the menu.",
    )


# ──────────────────────────────────────────────
# Image message handler — expense receipt scanning
# ──────────────────────────────────────────────
async def _handle_image(
    sender: str, image_info: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Handle an incoming image — process as receipt if in expense wizard."""
    import base64

    # Check if user is in the expense wizard flow
    session = _wizard_sessions.get(sender)
    if not session or session.get("state") != "awaiting_receipt_photo":
        await send_text_message(
            client, sender,
            "📸 To record an expense, type /START and choose *🧾 Record Expense* first.",
        )
        return

    _start_timeout(sender, client)
    supabase = get_supabase()

    biz_result = (
        supabase.table("businesses")
        .select("*")
        .eq("id", session["business_id"])
        .execute()
    )
    if not biz_result.data:
        _wizard_end_session(sender)
        await send_text_message(
            client, sender,
            "⚠️ Business not found. Type /START to begin again.",
        )
        return

    business = biz_result.data[0]
    sender_e164 = f"+{sender}" if not sender.startswith("+") else sender
    media_id = image_info.get("id", "")
    caption = image_info.get("caption", "").strip()

    if not media_id:
        await send_text_message(client, sender, "⚠️ Couldn't read that image. Please try again.")
        return

    await send_text_message(
        client, sender,
        "🧾 *Receipt received!* Scanning now...\n"
        "This usually takes a few seconds.",
    )

    try:
        # Download the image from WhatsApp
        image_bytes, mime_type = await download_media(client, media_id)

        # Convert to base64 data URL for OpenAI vision
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        # Extract receipt data via GPT-4o vision
        receipt = await extract_receipt_data(data_url)

        vendor = receipt.get("vendor", "Unknown")
        description = receipt.get("description", caption or "Receipt")
        category = receipt.get("category", "general")
        date = receipt.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        subtotal = float(receipt.get("subtotal", 0))
        tax_amount = float(receipt.get("tax_amount", 0))
        total = float(receipt.get("total", 0))
        currency = receipt.get("currency", business.get("currency", "GBP"))

        # If total is 0 but subtotal isn't, use subtotal
        if total == 0 and subtotal > 0:
            total = subtotal + tax_amount

        # Store in DB (including the receipt image for later viewing)
        import json as _json
        supabase.table("expenses").insert({
            "business_id": business["id"],
            "vendor": vendor,
            "description": description,
            "category": category,
            "date": date,
            "subtotal": round(subtotal, 2),
            "tax_amount": round(tax_amount, 2),
            "total": round(total, 2),
            "currency": currency,
            "receipt_data": _json.dumps(receipt),
            "receipt_image": data_url,
        }).execute()

        sym = "£" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "
        line_items = receipt.get("line_items", [])
        items_text = ""
        if line_items:
            items_text = "\n".join(
                f"  • {li.get('description', '?')} — {sym}{li.get('amount', 0):.2f}"
                for li in line_items[:8]
            )
            items_text = f"\n\n📋 *Items:*\n{items_text}"

        await send_text_message(
            client, sender,
            f"✅ *Expense recorded!*\n\n"
            f"🏪 *Vendor:* {vendor}\n"
            f"📝 *Description:* {description}\n"
            f"📂 *Category:* {category}\n"
            f"📅 *Date:* {date}\n"
            f"💰 *Total:* {sym}{total:.2f}"
            f"{f' (inc. {sym}{tax_amount:.2f} VAT)' if tax_amount > 0 else ''}"
            f"{items_text}\n\n"
            f"View all expenses in your dashboard.",
        )

        # Return to action menu
        session["state"] = "choose_action"
        await send_interactive_list(
            client, sender,
            "What would you like to do next?",
            "Choose an option",
            [{"title": "Actions", "rows": _action_menu_rows(session.get("channel", "whatsapp"), sender)}],
        )

        log_message(
            business_id=business["id"],
            to_phone=sender_e164,
            message_body=f"Expense recorded: {vendor} — {sym}{total:.2f}",
            message_type="expense",
        )

        logger.info(
            "EXPENSE: biz=%s vendor=%s total=%s category=%s",
            business["id"], vendor, total, category,
        )

    except Exception:
        logger.exception("Failed to process receipt image from %s", sender)
        await send_text_message(
            client, sender,
            "⚠️ Sorry, I couldn't read that receipt. "
            "Please make sure the image is clear and try again.",
        )
        # Stay in awaiting_receipt_photo state so they can retry


# ──────────────────────────────────────────────
# Customer text handler (no chat relay — automated service only)
# ──────────────────────────────────────────────
async def _handle_customer_text(
    sender: str, sender_e164: str, text: str, client: httpx.AsyncClient
) -> None:
    """Customer texted the bot — tell them to contact the tradesperson directly."""
    supabase = get_supabase()

    cust_result = (
        supabase.table("customers")
        .select("id, business_id, name")
        .eq("phone_number", sender_e164)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not cust_result.data:
        await send_text_message(
            client, sender,
            "Sorry, we don't recognise your number. "
            "Please ask your tradesperson to set you up.",
        )
        return

    customer = cust_result.data[0]
    biz_result = (
        supabase.table("businesses")
        .select("phone_number, business_name")
        .eq("id", customer["business_id"])
        .single()
        .execute()
    )

    if not biz_result.data:
        return

    biz = biz_result.data
    biz_name = biz.get("business_name", "your tradesperson")
    personal_phone = _format_phone_display(biz.get("phone_number", ""))

    await send_text_message(
        client, sender,
        f"\U0001f44b This is an automated service for {biz_name}.\n\n"
        f"To chat about your job, please contact them directly on:\n"
        f"\U0001f4f1 *{personal_phone}*",
    )


# ──────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────

def _normalise_phone(raw: str) -> str:
    """Normalise a raw phone input to E.164."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0"):
        digits = "44" + digits[1:]
    return f"+{digits}"


def _format_phone_display(e164: str) -> str:
    """Format E.164 phone for display (UK-focused)."""
    if e164.startswith("+44") and len(e164) == 13:
        local = "0" + e164[3:]
        return f"{local[:5]} {local[5:]}"
    return e164


async def _post_edited_reply(
    sender: str,
    edited_text: str,
    business: dict[str, Any],
    draft: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Post the owner's hand-written reply to Google and mark the draft."""
    supabase = get_supabase()

    encrypted_refresh = business.get("google_refresh_token", "")
    account_id = business.get("google_account_id", "")
    location_id = business.get("google_location_id", "")

    if not (encrypted_refresh and account_id and location_id):
        await send_text_message(
            client, sender, "Google is not connected for your account. "
            "Please connect Google first.",
        )
        return

    try:
        refresh_token = decrypt(encrypted_refresh)
        access_token = await refresh_access_token(refresh_token)
        review_name = (
            f"accounts/{account_id}/locations/{location_id}"
            f"/reviews/{draft['google_review_id']}"
        )
        await post_review_reply(access_token, review_name, edited_text)

        supabase.table("review_drafts").update(
            {"status": "posted", "ai_draft_reply": edited_text}
        ).eq("id", draft["id"]).execute()

        await send_text_message(
            client, sender, "Done! Your reply has been posted to Google."
        )
        logger.info("Edited reply posted for draft %s", draft["id"])
    except Exception:
        logger.exception("Failed to post edited reply for draft %s", draft["id"])
        await send_text_message(
            client, sender,
            "Something went wrong posting your reply. Please try again.",
        )


# ──────────────────────────────────────────────
# Invoice / Quote confirm-before-send handlers
# ──────────────────────────────────────────────
async def _confirm_send_invoice(
    sender: str, inv_id: str, client: httpx.AsyncClient
) -> None:
    """User tapped Send on an invoice preview — deliver it now."""
    supabase = get_supabase()
    session = _wizard_sessions.get(sender, {})
    channel = session.get("channel", "whatsapp")
    customer_email = session.get("customer_email", "")

    inv_result = supabase.table("invoices").select("*").eq("id", inv_id).execute()
    if not inv_result.data:
        await send_text_message(client, sender, "Invoice not found.")
        return
    inv = inv_result.data[0]

    biz_result = supabase.table("businesses").select("*").eq("id", inv["business_id"]).execute()
    business = biz_result.data[0] if biz_result.data else {}

    cust_result = (
        supabase.table("customers").select("id, name, phone_number, email")
        .eq("id", inv["customer_id"]).execute()
    )
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_phone = customer["phone_number"] if customer else ""
    first_name = customer_name.split()[0]
    if not customer_email and customer:
        customer_email = customer.get("email", "")

    currency = inv.get("currency", "GBP")
    sym = "\u00a3" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "

    await _send_invoice_to_customer(
        sender, inv, invoice_number=inv["invoice_number"],
        description=inv.get("notes", ""),
        subtotal=inv["subtotal"], tax_rate=inv["tax_rate"],
        tax_amount=inv["tax_amount"], total=inv["total"],
        sym=sym,
        customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=business.get("business_name", ""),
        business=business, client=client,
        channel=channel, customer_email=customer_email,
    )


async def _cancel_invoice(
    sender: str, inv_id: str, client: httpx.AsyncClient
) -> None:
    """User tapped Cancel on an invoice preview — delete it."""
    supabase = get_supabase()
    inv_result = supabase.table("invoices").select("invoice_number").eq("id", inv_id).execute()
    inv_number = inv_result.data[0]["invoice_number"] if inv_result.data else "?"
    supabase.table("line_items").delete().eq("parent_id", inv_id).eq("parent_type", "invoice").execute()
    supabase.table("invoices").delete().eq("id", inv_id).execute()
    _wizard_end_session(sender)
    await send_text_message(
        client, sender,
        f"\u274c Invoice {inv_number} cancelled.\n\nType /START for a new session.",
    )


async def _confirm_send_quote(
    sender: str, quo_id: str, client: httpx.AsyncClient
) -> None:
    """User tapped Send on a quote preview — deliver it now."""
    supabase = get_supabase()
    session = _wizard_sessions.get(sender, {})
    channel = session.get("channel", "whatsapp")
    customer_email = session.get("customer_email", "")

    quo_result = supabase.table("quotes").select("*").eq("id", quo_id).execute()
    if not quo_result.data:
        await send_text_message(client, sender, "Quote not found.")
        return
    quo = quo_result.data[0]

    biz_result = supabase.table("businesses").select("*").eq("id", quo["business_id"]).execute()
    business = biz_result.data[0] if biz_result.data else {}

    cust_result = (
        supabase.table("customers").select("id, name, phone_number, email")
        .eq("id", quo["customer_id"]).execute()
    )
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_phone = customer["phone_number"] if customer else ""
    first_name = customer_name.split()[0]
    if not customer_email and customer:
        customer_email = customer.get("email", "")

    currency = quo.get("currency", "GBP")
    sym = "\u00a3" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "

    await _send_quote_to_customer(
        sender, quo, quote_number=quo["quote_number"],
        description=quo.get("notes", ""),
        subtotal=quo["subtotal"], tax_rate=quo["tax_rate"],
        tax_amount=quo["tax_amount"], total=quo["total"],
        sym=sym,
        valid_until=quo.get("valid_until", ""),
        customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=business.get("business_name", ""),
        business=business, client=client,
        channel=channel, customer_email=customer_email,
    )


async def _cancel_quote(
    sender: str, quo_id: str, client: httpx.AsyncClient
) -> None:
    """User tapped Cancel on a quote preview — delete it."""
    supabase = get_supabase()
    quo_result = supabase.table("quotes").select("quote_number").eq("id", quo_id).execute()
    quo_number = quo_result.data[0]["quote_number"] if quo_result.data else "?"
    supabase.table("line_items").delete().eq("parent_id", quo_id).eq("parent_type", "quote").execute()
    supabase.table("quotes").delete().eq("id", quo_id).execute()
    _wizard_end_session(sender)
    await send_text_message(
        client, sender,
        f"\u274c Quote {quo_number} cancelled.\n\nType /START for a new session.",
    )


# ──────────────────────────────────────────────
# Button / interactive reply handler
# ──────────────────────────────────────────────
async def _handle_button(
    sender: str, payload: str, client: httpx.AsyncClient
) -> None:
    # ── Demo flow buttons ──
    if payload.startswith("demo_"):
        if await _handle_demo_button(sender, payload, client):
            return

    # ── Demo message limit check ──
    if sender in _demo_sessions:
        if await _demo_check_limit(sender, client):
            return

    # ── Wizard flow buttons ──
    if payload.startswith("wiz_"):
        if await _wizard_handle_button(sender, payload, client):
            return

    supabase = get_supabase()
    sender_e164 = f"+{sender}" if not sender.startswith("+") else sender

    # ── Confirm / cancel invoice ──
    if payload.startswith("sendinv_"):
        inv_id = payload.removeprefix("sendinv_")
        await _confirm_send_invoice(sender, inv_id, client)
        return
    if payload.startswith("cancelinv_"):
        inv_id = payload.removeprefix("cancelinv_")
        await _cancel_invoice(sender, inv_id, client)
        return

    # ── Confirm / cancel booking ──
    if payload == "confirmbk":
        await _confirm_booking(sender, client)
        return
    if payload == "cancelbk":
        await _cancel_booking(sender, client)
        return

    # ── Confirm / cancel quote ──
    if payload.startswith("sendquo_"):
        quo_id = payload.removeprefix("sendquo_")
        await _confirm_send_quote(sender, quo_id, client)
        return
    if payload.startswith("cancelquo_"):
        quo_id = payload.removeprefix("cancelquo_")
        await _cancel_quote(sender, quo_id, client)
        return

    # ── 1. "approve_<draft_id>" — post the AI draft to Google ──
    if payload.startswith("approve_"):
        draft_id = payload.removeprefix("approve_")
        await _approve_draft(sender, draft_id, client)
        return

    # ── 2. "edit_<draft_id>" — ask owner to type a replacement ──
    if payload.startswith("edit_"):
        draft_id = payload.removeprefix("edit_")
        supabase.table("review_drafts").update({"status": "awaiting_edit"}).eq(
            "id", draft_id
        ).execute()
        await send_text_message(
            client,
            sender,
            "No problem. Just type your reply and I'll post it for you.",
        )
        return

    # ── 3. "great" — customer had a good experience ──
    if payload in ("great", "review_great"):
        await _handle_great(sender, sender_e164, client)
        return

    # ── 4. "could_be_better" — customer had a bad experience ──
    if payload == "could_be_better":
        await _handle_could_be_better(sender, sender_e164, client)
        return

    # ── Fallback: reject ──
    if payload.startswith("reject_"):
        draft_id = payload.removeprefix("reject_")
        supabase.table("review_drafts").update({"status": "rejected"}).eq(
            "id", draft_id
        ).execute()
        await send_text_message(
            client, sender, "Draft rejected. No reply will be posted."
        )
        return

    logger.debug("Unknown button payload: %s from %s", payload, sender)


# ──────────────────────────────────────────────
# Button sub-handlers
# ──────────────────────────────────────────────
async def _approve_draft(
    sender: str, draft_id: str, client: httpx.AsyncClient
) -> None:
    """Look up the draft, post it to Google, and confirm."""
    supabase = get_supabase()

    draft_result = (
        supabase.table("review_drafts")
        .select("*, businesses(*)")
        .eq("id", draft_id)
        .execute()
    )
    if not draft_result.data:
        await send_text_message(client, sender, "Draft not found.")
        return

    draft = draft_result.data[0]
    business = draft.get("businesses") or {}

    encrypted_refresh = business.get("google_refresh_token", "")
    account_id = business.get("google_account_id", "")
    location_id = business.get("google_location_id", "")

    if not (encrypted_refresh and account_id and location_id):
        await send_text_message(
            client, sender,
            "Google is not connected. Please connect your Google account first.",
        )
        return

    try:
        refresh_token = decrypt(encrypted_refresh)
        access_token = await refresh_access_token(refresh_token)
        review_name = (
            f"accounts/{account_id}/locations/{location_id}"
            f"/reviews/{draft['google_review_id']}"
        )
        await post_review_reply(access_token, review_name, draft["ai_draft_reply"])

        supabase.table("review_drafts").update({"status": "posted"}).eq(
            "id", draft_id
        ).execute()

        await send_text_message(
            client, sender, "Done! Your reply has been posted to Google."
        )
        logger.info("Approved draft %s posted to Google", draft_id)
    except Exception:
        logger.exception("Failed to post approved draft %s", draft_id)
        await send_text_message(
            client, sender,
            "Something went wrong posting to Google. Please try again.",
        )


async def _handle_great(
    sender: str, sender_e164: str, client: httpx.AsyncClient
) -> None:
    """Customer tapped 'Great' — send the Google review link."""
    supabase = get_supabase()

    cust_result = (
        supabase.table("customers")
        .select("id, business_id")
        .eq("phone_number", sender_e164)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not cust_result.data:
        return

    customer = cust_result.data[0]
    biz_result = (
        supabase.table("businesses")
        .select("google_review_link, business_name")
        .eq("id", customer["business_id"])
        .single()
        .execute()
    )
    biz = biz_result.data
    link = biz.get("google_review_link") if biz else None

    if link:
        await send_text_message(
            client,
            sender,
            f"Thank you! Please leave your review here:\n{link}",
        )
        log_message(
            business_id=customer["business_id"],
            to_phone=sender_e164,
            message_body=f"Google review link sent: {link}",
            message_type="review_link",
        )
    else:
        await send_text_message(
            client,
            sender,
            "Thank you for your feedback! We really appreciate it.",
        )

    # Update customer status
    supabase.table("customers").update(
        {"status": "clicked_great", "review_link_sent": True}
    ).eq("id", customer["id"]).execute()


async def _handle_could_be_better(
    sender: str, sender_e164: str, client: httpx.AsyncClient
) -> None:
    """Customer tapped 'Could be better' — alert the business owner."""
    supabase = get_supabase()

    cust_result = (
        supabase.table("customers")
        .select("id, business_id, name, phone_number")
        .eq("phone_number", sender_e164)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not cust_result.data:
        return

    customer = cust_result.data[0]
    biz_result = (
        supabase.table("businesses")
        .select("phone_number, business_name")
        .eq("id", customer["business_id"])
        .single()
        .execute()
    )
    biz = biz_result.data or {}
    business_name = biz.get("business_name", "the business")
    owner_phone = biz.get("phone_number", "").lstrip("+")

    # Reply to the customer
    await send_text_message(
        client,
        sender,
        f"We're sorry to hear that. Your feedback has been shared with "
        f"{business_name} and they'll be in touch shortly.",
    )

    # Alert the business owner
    if owner_phone:
        customer_name = customer.get("name", "A customer")
        customer_phone = customer.get("phone_number", "unknown")
        alert_body = (
            f"\u26a0\ufe0f {customer_name} ({customer_phone}) had a bad experience. "
            f"Reach out to them ASAP."
        )
        await send_text_message(client, owner_phone, alert_body)
        log_message(
            business_id=customer["business_id"],
            to_phone=biz.get("phone_number", ""),
            message_body=alert_body,
            message_type="negative_alert",
        )

    # Update customer status
    supabase.table("customers").update({"status": "clicked_bad"}).eq(
        "id", customer["id"]
    ).execute()
