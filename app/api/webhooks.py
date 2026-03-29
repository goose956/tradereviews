"""WhatsApp Cloud API webhook — verification (GET) and inbound messages (POST)."""

import hmac
import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.core.config import get_settings
from app.core.security import decrypt
from app.db.supabase import get_supabase
from app.services.google import post_review_reply, refresh_access_token
from app.services.message_log import log_message
from app.services.parser import parse_review_command
from app.services.whatsapp import send_interactive_buttons, send_template_message, send_text_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook/whatsapp", tags=["webhook"])

# ──────────────────────────────────────────────
# Pending-command state  (collects missing info)
# ──────────────────────────────────────────────
# keyed by sender raw phone → {"cmd": "INVOICE", ...partial data}
_pending_commands: dict[str, dict[str, Any]] = {}


# ──────────────────────────────────────────────
# Interactive Demo — "The Flip" state machine
# ──────────────────────────────────────────────
_demo_sessions: dict[str, str] = {}  # sender raw phone → state

_DEMO_TRIGGERS = {
    "hi, show me how this works!",
    "hi, show me how this works",
    "hi show me how this works",
    "show me how this works",
    "demo",
}


async def _maybe_handle_demo(
    sender: str, text: str, client: httpx.AsyncClient
) -> bool:
    """Route demo flow messages.  Returns True if handled."""
    state = _demo_sessions.get(sender)
    trimmed = text.strip()
    lower = trimmed.lower().rstrip("!.")

    # ── New demo session trigger ──
    if state is None:
        if lower not in _DEMO_TRIGGERS:
            return False
        # Don't hijack registered businesses
        sender_e164 = f"+{sender}" if not sender.startswith("+") else sender
        supabase = get_supabase()
        biz = supabase.table("businesses").select("id").eq("phone_number", sender_e164).execute()
        if biz.data:
            return False

        _demo_sessions[sender] = "awaiting_review"
        await send_text_message(
            client, sender,
            "Hey! \U0001f44b Welcome to JobPing.\n\n"
            "I'm your new AI admin assistant. Let's pretend you just "
            "finished a job for a customer named John, and you want "
            "a 5-star review.\n\n"
            "To ask John for a review, just reply with:\n"
            "/REVIEW John",
        )
        return True

    # ── Awaiting /REVIEW John ──
    if state == "awaiting_review":
        if trimmed.upper().startswith("/REVIEW"):
            _demo_sessions[sender] = "awaiting_button"

            await send_text_message(
                client, sender,
                "\u2705 Boom. Review request sent to John. "
                "It's that easy.\n\n"
                "Your customer gets a friendly WhatsApp message like "
                "the one below — with buttons they can tap.",
            )

            await send_text_message(
                client, sender,
                "Now, let's switch roles. \U0001f504\n\n"
                "Imagine *you* are John. Your phone just buzzed. "
                "Here is exactly what John sees:",
            )

            await send_interactive_buttons(
                client, sender,
                "Hi John, thanks for choosing us today! "
                "How was your experience?",
                [
                    {"id": "demo_great", "title": "Great! \u2b50"},
                    {"id": "demo_bad", "title": "Could be better"},
                ],
            )
            return True

        await send_text_message(
            client, sender,
            "Just type /REVIEW John to see the magic! \U0001f446",
        )
        return True

    # ── Sent buttons, waiting for tap ──
    if state == "awaiting_button":
        await send_text_message(
            client, sender,
            "Tap one of the buttons above to see what happens! \u261d\ufe0f",
        )
        return True

    # ── Demo finished — clear and let normal routing take over ──
    if state == "completed":
        del _demo_sessions[sender]
        return False

    return False


async def _handle_demo_button(
    sender: str, payload: str, client: httpx.AsyncClient
) -> bool:
    """Handle button taps during the demo.  Returns True if handled."""
    state = _demo_sessions.get(sender)
    if state != "awaiting_button" and payload != "demo_start_trial":
        return False

    if payload == "demo_great":
        _demo_sessions[sender] = "completed"

        await send_text_message(
            client, sender,
            "\U0001f389 Awesome! Because John tapped \u2018Great\u2019, "
            "we instantly send him the link to your Google Business page "
            "to leave a 5-star review.\n\n"
            "If he'd tapped 'Could be better', we'd alert you privately "
            "so you can fix it — before he leaves a bad review online.",
        )

        await send_text_message(
            client, sender,
            "That's the whole system. No apps, no dashboards. "
            "Just WhatsApp me your customer's name and number, "
            "and I handle the rest.\n\n"
            "Ready to start getting real reviews?",
        )

        await send_interactive_buttons(
            client, sender,
            "Start your 14-day free trial — no card required.",
            [{"id": "demo_start_trial", "title": "Start Free Trial"}],
        )
        return True

    if payload == "demo_bad":
        _demo_sessions[sender] = "completed"

        await send_text_message(
            client, sender,
            "\u26a0\ufe0f John said 'Could be better'. In a real scenario, "
            "we'd alert you privately with their name and number so you "
            "can call them and fix it — before they leave a bad review "
            "online.\n\n"
            "This 'review gating' protects your Google rating.",
        )

        await send_text_message(
            client, sender,
            "That's the whole system. No apps, no dashboards. "
            "Just WhatsApp me your customer's name and number, "
            "and I handle the rest.\n\n"
            "Ready to start getting real reviews?",
        )

        await send_interactive_buttons(
            client, sender,
            "Start your 14-day free trial — no card required.",
            [{"id": "demo_start_trial", "title": "Start Free Trial"}],
        )
        return True

    if payload == "demo_start_trial":
        _demo_sessions.pop(sender, None)

        await send_text_message(
            client, sender,
            "\U0001f680 Let's get you set up!\n\n"
            "Tap the link below to create your account and "
            "connect your Google Business Profile:\n\n"
            "\U0001f449 https://yourdomain.com\n\n"
            "Your 14-day free trial starts the moment you sign up.",
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
    body: dict[str, Any] = await request.json()

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                await _handle_message(message, request)

    return {"status": "ok"}


# ──────────────────────────────────────────────
# Message router
# ──────────────────────────────────────────────
async def _handle_message(message: dict[str, Any], request: Request) -> None:
    msg_type = message.get("type")
    sender = message.get("from", "")
    http_client = request.app.state.http_client

    if msg_type == "button":
        payload = message.get("button", {}).get("payload", "")
        await _handle_button(sender, payload, http_client)
        return

    if msg_type == "interactive":
        button_id = (
            message.get("interactive", {})
            .get("button_reply", {})
            .get("id", "")
        )
        await _handle_button(sender, button_id, http_client)
        return

    if msg_type == "text":
        text = message.get("text", {}).get("body", "")
        await _handle_text(sender, text, http_client)
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

    # ── STOP opt-out (works regardless of sender role) ──
    if text.strip().upper() == "STOP":
        supabase.table("customers").update({"marketing_opt_in": 0}).eq(
            "phone_number", sender_e164
        ).execute()
        await send_text_message(
            client, sender,
            "You've been unsubscribed from marketing messages. "
            "You won't receive any more offers from us.",
        )
        return

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
        await _handle_tradesperson_text(sender, text, biz_result.data[0], client)
    else:
        await _handle_customer_text(sender, sender_e164, text, client)


# ──────────────────────────────────────────────
# Tradesperson command handling
# ──────────────────────────────────────────────
async def _handle_tradesperson_text(
    sender: str, text: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Process text from a registered business owner — commands or chat relay."""
    supabase = get_supabase()
    trimmed = text.strip()
    upper = trimmed.upper()

    # ── Priority: awaiting_edit draft (only for non-command text) ──
    if not upper.startswith("/"):
        # Check for pending command first
        if sender in _pending_commands:
            await _resume_pending(sender, trimmed, business, client)
            return

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

    # ── Command dispatch ──
    # Starting a new command cancels any pending one
    if upper.startswith("/"):
        _pending_commands.pop(sender, None)

    if upper.startswith("/SETUP"):
        await _cmd_setup(sender, trimmed[6:].strip(), business, client)
    elif upper.startswith("/REVIEW"):
        await _cmd_review(sender, trimmed[7:].strip(), business, client)
    elif upper.startswith("/INVOICE"):
        await _cmd_invoice(sender, trimmed[8:].strip(), business, client)
    elif upper.startswith("/QUOTE"):
        await _cmd_quote(sender, trimmed[6:].strip(), business, client)
    elif upper.startswith("/CHAT"):
        await _cmd_chat(sender, trimmed[5:].strip(), business, client)
    elif upper.startswith("/LOGIN"):
        await _cmd_login(sender, business, client)
    elif upper.startswith("/HELP"):
        await _cmd_help(sender, trimmed[5:].strip(), client)
    elif upper.startswith("/"):
        await send_text_message(
            client, sender,
            "❓ Unknown command. Send /HELP for a list of commands.",
        )
    else:
        # Normal free-text → relay to active customer
        await _relay_to_customer(sender, trimmed, business, client)


# ──────────────────────────────────────────────
# /SETUP <Name> <Phone>
# ──────────────────────────────────────────────
async def _cmd_setup(
    sender: str, args: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Add a customer and send them an icebreaker message."""
    supabase = get_supabase()

    parsed = parse_review_command(args)
    if not parsed:
        await send_text_message(
            client, sender,
            "Usage: /SETUP <Name> <Phone>\n"
            "Example: /SETUP John Smith 07845774563",
        )
        return

    # Upsert customer
    supabase.table("customers").upsert(
        {
            "business_id": business["id"],
            "phone_number": parsed.phone,
            "name": parsed.name,
            "status": "setup_sent",
        },
        on_conflict="business_id,phone_number",
    ).execute()

    # Set as active customer
    supabase.table("businesses").update(
        {"active_customer_phone": parsed.phone}
    ).eq("id", business["id"]).execute()

    # Send icebreaker to the customer
    first_name = parsed.name.split()[0]
    biz_name = business["business_name"]
    icebreaker = (
        f"Hi {first_name}! \U0001f44b This is the automated assistant for "
        f"{biz_name}.\n\n"
        f"We've set up this chat so we can easily send you updates, "
        f"quotes, and invoices for your job.\n\n"
        f"You can reply directly to this message anytime to chat "
        f"with the team!"
    )

    customer_phone = parsed.phone.lstrip("+")
    try:
        await send_text_message(client, customer_phone, icebreaker)
    except Exception:
        logger.exception("Failed to send icebreaker to %s", parsed.phone)
        await send_text_message(
            client, sender,
            f"\u2705 {parsed.name} ({parsed.phone}) saved, but the "
            f"icebreaker failed to send. They may need to message "
            f"this number first to open the chat window.",
        )
        return

    log_message(
        business_id=business["id"],
        to_phone=parsed.phone,
        message_body=icebreaker,
        message_type="icebreaker",
    )

    await send_text_message(
        client, sender,
        f"\u2705 {parsed.name} ({parsed.phone}) has been set up and an "
        f"icebreaker sent!\n\n"
        f"They're now your active chat \u2014 just type normally to "
        f"message them.",
    )
    logger.info("SETUP: biz=%s customer=%s", business["id"], parsed.phone)


# ──────────────────────────────────────────────
# /REVIEW [Name Phone]
# ──────────────────────────────────────────────
async def _cmd_review(
    sender: str, args: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Send a review request to a specific customer or the active one."""
    supabase = get_supabase()

    if args:
        parsed = parse_review_command(args)
        if not parsed:
            await send_text_message(
                client, sender,
                "Usage: /REVIEW <Name> <Phone>\n"
                "Or just /REVIEW to send to your active chat customer.",
            )
            return
        customer_phone = parsed.phone
        customer_name = parsed.name

        # Set as active customer
        supabase.table("businesses").update(
            {"active_customer_phone": customer_phone}
        ).eq("id", business["id"]).execute()
    else:
        # Use active customer
        customer_phone = business.get("active_customer_phone") or ""
        if not customer_phone:
            await send_text_message(
                client, sender,
                "No active customer. Use /REVIEW <Name> <Phone> "
                "or /CHAT <Name> to select one first.",
            )
            return
        cust_result = (
            supabase.table("customers")
            .select("name")
            .eq("business_id", business["id"])
            .eq("phone_number", customer_phone)
            .limit(1)
            .execute()
        )
        customer_name = (
            cust_result.data[0]["name"] if cust_result.data else "there"
        )

    # Upsert customer with review_requested_at timestamp
    from datetime import datetime, timezone
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
    await send_template_message(
        client,
        to_phone=customer_phone.lstrip("+"),
        customer_name=first_name,
        business_name=business["business_name"],
    )

    log_message(
        business_id=business["id"],
        to_phone=customer_phone,
        message_body=(
            f"Review request sent to {customer_name} ({customer_phone}) "
            f"on behalf of {business['business_name']}"
        ),
        message_type="review_request",
    )

    await send_text_message(
        client, sender,
        f"\u2705 Review request sent to {customer_name} "
        f"({customer_phone}).\nThey'll receive a WhatsApp asking "
        f"about their experience with {business['business_name']}.",
    )
    logger.info(
        "REVIEW: biz=%s customer=%s", business["id"], customer_phone
    )


# ──────────────────────────────────────────────
# /INVOICE <Amount> <Description>
# ──────────────────────────────────────────────
async def _cmd_invoice(
    sender: str, args: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Create & send an invoice.  Asks follow-up questions for missing info."""

    # ── Must have an active customer first ──
    customer_phone = business.get("active_customer_phone") or ""
    if not customer_phone:
        await send_text_message(
            client, sender,
            "\u26a0\ufe0f No active customer.\n\n"
            "Use /SETUP <Name> <Phone> to add a customer first, "
            "or /CHAT <Name> to select an existing one.",
        )
        return

    # ── Try to parse amount + description from args ──
    amount, description = _parse_invoice_args(args)

    # Nothing at all — ask for everything
    if amount is None and not description:
        _pending_commands[sender] = {
            "cmd": "INVOICE",
            "step": "need_amount",
            "business_id": business["id"],
            "customer_phone": customer_phone,
        }
        await send_text_message(
            client, sender,
            "\U0001f4dd *Let's create an invoice.*\n\n"
            "What is the *amount*? (e.g. 250)",
        )
        return

    # Have amount but no description
    if amount is not None and not description:
        _pending_commands[sender] = {
            "cmd": "INVOICE",
            "step": "need_description",
            "amount": amount,
            "business_id": business["id"],
            "customer_phone": customer_phone,
        }
        await send_text_message(
            client, sender,
            f"\u2705 Amount: \u00a3{amount:.2f}\n\n"
            f"What is this invoice *for*? "
            f"(e.g. Bathroom refit, Boiler service)",
        )
        return

    # Have description but no amount (unlikely but handle it)
    if amount is None and description:
        _pending_commands[sender] = {
            "cmd": "INVOICE",
            "step": "need_amount",
            "description": description,
            "business_id": business["id"],
            "customer_phone": customer_phone,
        }
        await send_text_message(
            client, sender,
            f"\u2705 Description: {description}\n\n"
            f"What is the *amount*? (e.g. 250)",
        )
        return

    # Have both — create the invoice
    await _finalise_invoice(sender, amount, description, business, client)


def _parse_invoice_args(args: str) -> tuple[float | None, str]:
    """Parse amount and description from command arguments.

    Accepts flexible formats like:
      250 Plumbing work
      Test 250 Plumbing work
      £250 Plumbing
      Plumbing work 250
    Returns (amount_or_None, description_string).
    """
    args = args.strip()
    if not args:
        return None, ""

    # Find a monetary amount anywhere in the string (£/$, commas, decimals)
    m = re.search(r"[\u00a3$]?\s*([\d,]+(?:\.\d{1,2})?)\b", args)
    if m:
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            return None, args
        # Everything except the matched number is the description
        desc = (args[:m.start()] + " " + args[m.end():]).strip()
        # Collapse double spaces and clean up any leftover £/$ at the boundary
        desc = re.sub(r"\s{2,}", " ", desc)
        desc = re.sub(r"^[\u00a3$]\s*", "", desc).strip()
        return amount, desc

    # No number found — the whole thing is a description
    return None, args


async def _resume_pending(
    sender: str, text: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Continue collecting info for a pending command."""
    pending = _pending_commands.get(sender)
    if not pending:
        return

    # Allow cancellation
    if text.strip().upper() in ("CANCEL", "/CANCEL", "STOP", "NEVERMIND"):
        _pending_commands.pop(sender, None)
        cmd = pending.get("cmd", "request")
        await send_text_message(
            client, sender, f"\u274c Cancelled. No {cmd.lower()} was created."
        )
        return

    cmd = pending.get("cmd")

    if cmd == "INVOICE":
        await _resume_invoice(sender, text, pending, business, client)
    elif cmd == "QUOTE":
        await _resume_quote(sender, text, pending, business, client)
    else:
        # Unknown pending command — clear it
        _pending_commands.pop(sender, None)


async def _resume_invoice(
    sender: str, text: str, pending: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Resume an in-progress invoice creation."""
    step = pending.get("step")

    if step == "need_amount":
        # Try to extract a number from their reply
        m = re.search(r"[\u00a3$]?\s*([\d,]+(?:\.\d{1,2})?)", text)
        if not m:
            await send_text_message(
                client, sender,
                "\u26a0\ufe0f I didn't catch a number there.\n"
                "Please enter the *amount* (e.g. 250).\n\n"
                "Type *CANCEL* to cancel.",
            )
            return
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            await send_text_message(
                client, sender,
                "\u26a0\ufe0f That doesn't look like a valid amount.\n"
                "Please enter a number (e.g. 250).\n\n"
                "Type *CANCEL* to cancel.",
            )
            return

        if amount <= 0:
            await send_text_message(
                client, sender,
                "\u26a0\ufe0f The amount must be greater than zero.\n\n"
                "Type *CANCEL* to cancel.",
            )
            return

        # Already have a description?
        description = pending.get("description", "")
        if description:
            _pending_commands.pop(sender, None)
            await _finalise_invoice(sender, amount, description, business, client)
            return

        # Still need description
        pending["amount"] = amount
        pending["step"] = "need_description"
        await send_text_message(
            client, sender,
            f"\u2705 Amount: \u00a3{amount:.2f}\n\n"
            f"What is this invoice *for*? "
            f"(e.g. Bathroom refit, Boiler service)",
        )
        return

    if step == "need_description":
        description = text.strip()
        if len(description) < 3:
            await send_text_message(
                client, sender,
                "\u26a0\ufe0f Please provide a short description of the work "
                "(at least a few words).\n\n"
                "Type *CANCEL* to cancel.",
            )
            return

        amount = pending.get("amount")
        if amount is None:
            # Shouldn't happen, but handle gracefully
            pending["description"] = description
            pending["step"] = "need_amount"
            await send_text_message(
                client, sender,
                f"\u2705 Description: {description}\n\n"
                f"What is the *amount*? (e.g. 250)",
            )
            return

        _pending_commands.pop(sender, None)
        await _finalise_invoice(sender, amount, description, business, client)
        return

    # Unknown step — reset
    _pending_commands.pop(sender, None)


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

    # Pick the right currency symbol
    sym = "\u00a3" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "

    # Build PDF link
    settings = get_settings()
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/invoices/{inv['id']}/pdf"

    # ── Check if confirm-before-send is enabled ──
    if business.get("confirm_before_send"):
        preview_msg = (
            f"\U0001f50d *Invoice Preview — {inv_number}*\n"
            f"To: {customer_name} ({customer_phone})\n\n"
            f"\u2022 {description}\n"
            f"\u2022 Subtotal: {sym}{subtotal:.2f}\n"
            f"\u2022 VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
            f"\u2022 *Total: {sym}{total:.2f}*\n\n"
            f"\U0001f4e4 PDF: {pdf_url}\n\n"
            f"Happy with this? Tap *Send* to deliver it to {first_name}, "
            f"or *Cancel* to discard."
        )
        await send_interactive_buttons(client, sender, preview_msg, [
            {"id": f"sendinv_{inv['id']}", "title": "\u2705 Send"},
            {"id": f"cancelinv_{inv['id']}", "title": "\u274c Cancel"},
        ])
        logger.info(
            "INVOICE PREVIEW: biz=%s inv=%s total=%s (awaiting confirm)",
            business["id"], inv_number, total,
        )
        return

    # ── No confirmation needed — send straight away ──
    await _send_invoice_to_customer(
        sender, inv, invoice_number=inv_number, description=description,
        subtotal=subtotal, tax_rate=tax_rate, tax_amount=tax_amount,
        total=total, sym=sym, pdf_url=pdf_url,
        customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=biz_name,
        business=business, client=client,
    )


async def _send_invoice_to_customer(
    sender: str, inv: dict, *, invoice_number: str, description: str,
    subtotal: float, tax_rate: float, tax_amount: float, total: float,
    sym: str, pdf_url: str, customer_name: str, customer_phone: str,
    first_name: str, biz_name: str,
    business: dict[str, Any], client: httpx.AsyncClient,
) -> None:
    """Actually deliver the invoice to the customer and confirm to the tradesperson."""

    supabase = get_supabase()

    invoice_msg = (
        f"Hi {first_name}, here is your invoice from {biz_name}:\n\n"
        f"\U0001f4c4 *Invoice {invoice_number}*\n"
        f"\u2022 {description}\n"
        f"\u2022 Subtotal: {sym}{subtotal:.2f}\n"
        f"\u2022 VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
        f"\u2022 *Total: {sym}{total:.2f}*\n\n"
        f"\U0001f4e4 View/download PDF:\n{pdf_url}\n\n"
        f"\U0001f4b3 Payment details will follow shortly."
    )

    customer_raw = customer_phone.lstrip("+")
    try:
        await send_text_message(client, customer_raw, invoice_msg)
    except Exception:
        logger.exception("Failed to send invoice to %s", customer_phone)
        await send_text_message(
            client, sender,
            f"\u2705 Invoice {invoice_number} created for {sym}{total:.2f}, "
            f"but failed to send to {customer_name}. "
            f"You can send the PDF from your dashboard.",
        )
        return

    # Mark sent timestamp & status
    from datetime import datetime, timezone
    supabase.table("invoices").update({
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", inv["id"]).execute()

    log_message(
        business_id=business["id"],
        to_phone=customer_phone,
        message_body=invoice_msg,
        message_type="invoice",
    )

    # ── Confirm to the tradesperson ──
    await send_text_message(
        client, sender,
        f"\u2705 Invoice {invoice_number} created & sent to {customer_name} "
        f"({customer_phone}).\n\n"
        f"\u2022 {description}: {sym}{total:.2f} (inc. VAT)\n\n"
        f"View or download the PDF from your dashboard.",
    )
    logger.info(
        "INVOICE: biz=%s customer=%s inv=%s total=%s",
        business["id"], customer_phone, invoice_number, total,
    )


# ──────────────────────────────────────────────
# /QUOTE [Amount] [Description]
# ──────────────────────────────────────────────
async def _cmd_quote(
    sender: str, args: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Create & send a quote.  Asks follow-up questions for missing info."""

    # ── Must have an active customer first ──
    customer_phone = business.get("active_customer_phone") or ""
    if not customer_phone:
        await send_text_message(
            client, sender,
            "\u26a0\ufe0f No active customer.\n\n"
            "Use /SETUP <Name> <Phone> to add a customer first, "
            "or /CHAT <Name> to select an existing one.",
        )
        return

    amount, description = _parse_invoice_args(args)

    if amount is None and not description:
        _pending_commands[sender] = {
            "cmd": "QUOTE",
            "step": "need_amount",
            "business_id": business["id"],
            "customer_phone": customer_phone,
        }
        await send_text_message(
            client, sender,
            "\U0001f4dd *Let's create a quote.*\n\n"
            "What is the *amount*? (e.g. 450)",
        )
        return

    if amount is not None and not description:
        _pending_commands[sender] = {
            "cmd": "QUOTE",
            "step": "need_description",
            "amount": amount,
            "business_id": business["id"],
            "customer_phone": customer_phone,
        }
        await send_text_message(
            client, sender,
            f"\u2705 Amount: \u00a3{amount:.2f}\n\n"
            f"What is this quote *for*? "
            f"(e.g. Full bathroom refit, New boiler install)",
        )
        return

    if amount is None and description:
        _pending_commands[sender] = {
            "cmd": "QUOTE",
            "step": "need_amount",
            "description": description,
            "business_id": business["id"],
            "customer_phone": customer_phone,
        }
        await send_text_message(
            client, sender,
            f"\u2705 Description: {description}\n\n"
            f"What is the *amount*? (e.g. 450)",
        )
        return

    await _finalise_quote(sender, amount, description, business, client)


async def _resume_quote(
    sender: str, text: str, pending: dict, business: dict[str, Any],
    client: httpx.AsyncClient,
) -> None:
    """Resume an in-progress quote creation."""
    step = pending.get("step")

    if step == "need_amount":
        m = re.search(r"[\u00a3$]?\s*([\d,]+(?:\.\d{1,2})?)", text)
        if not m:
            await send_text_message(
                client, sender,
                "\u26a0\ufe0f I didn't catch a number there.\n"
                "Please enter the *amount* (e.g. 450).\n\n"
                "Type *CANCEL* to cancel.",
            )
            return
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            await send_text_message(
                client, sender,
                "\u26a0\ufe0f That doesn't look like a valid amount.\n"
                "Please enter a number (e.g. 450).\n\n"
                "Type *CANCEL* to cancel.",
            )
            return

        if amount <= 0:
            await send_text_message(
                client, sender,
                "\u26a0\ufe0f The amount must be greater than zero.\n\n"
                "Type *CANCEL* to cancel.",
            )
            return

        description = pending.get("description", "")
        if description:
            _pending_commands.pop(sender, None)
            await _finalise_quote(sender, amount, description, business, client)
            return

        pending["amount"] = amount
        pending["step"] = "need_description"
        await send_text_message(
            client, sender,
            f"\u2705 Amount: \u00a3{amount:.2f}\n\n"
            f"What is this quote *for*? "
            f"(e.g. Full bathroom refit, New boiler install)",
        )
        return

    if step == "need_description":
        description = text.strip()
        if len(description) < 3:
            await send_text_message(
                client, sender,
                "\u26a0\ufe0f Please provide a short description of the work "
                "(at least a few words).\n\n"
                "Type *CANCEL* to cancel.",
            )
            return

        amount = pending.get("amount")
        if amount is None:
            pending["description"] = description
            pending["step"] = "need_amount"
            await send_text_message(
                client, sender,
                f"\u2705 Description: {description}\n\n"
                f"What is the *amount*? (e.g. 450)",
            )
            return

        _pending_commands.pop(sender, None)
        await _finalise_quote(sender, amount, description, business, client)
        return

    _pending_commands.pop(sender, None)


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

    # Valid for 30 days
    from datetime import datetime, timezone, timedelta
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

    # Build PDF link
    settings = get_settings()
    pdf_url = f"{settings.base_url}/member/business/{business['id']}/quotes/{quo['id']}/pdf"

    # ── Check if confirm-before-send is enabled ──
    if business.get("confirm_before_send"):
        preview_msg = (
            f"\U0001f50d *Quote Preview — {quo_number}*\n"
            f"To: {customer_name} ({customer_phone})\n\n"
            f"\u2022 {description}\n"
            f"\u2022 Subtotal: {sym}{subtotal:.2f}\n"
            f"\u2022 VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
            f"\u2022 *Total: {sym}{total:.2f}*\n"
            f"\u2022 Valid until: {valid_until}\n\n"
            f"\U0001f4e4 PDF: {pdf_url}\n\n"
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

    # ── No confirmation needed — send straight away ──
    await _send_quote_to_customer(
        sender, quo, quote_number=quo_number, description=description,
        subtotal=subtotal, tax_rate=tax_rate, tax_amount=tax_amount,
        total=total, sym=sym, pdf_url=pdf_url, valid_until=valid_until,
        customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=biz_name,
        business=business, client=client,
    )


async def _send_quote_to_customer(
    sender: str, quo: dict, *, quote_number: str, description: str,
    subtotal: float, tax_rate: float, tax_amount: float, total: float,
    sym: str, pdf_url: str, valid_until: str,
    customer_name: str, customer_phone: str,
    first_name: str, biz_name: str,
    business: dict[str, Any], client: httpx.AsyncClient,
) -> None:
    """Actually deliver the quote to the customer and confirm to the tradesperson."""

    supabase = get_supabase()

    quote_msg = (
        f"Hi {first_name}, here is a quote from {biz_name}:\n\n"
        f"\U0001f4c4 *Quote {quote_number}*\n"
        f"\u2022 {description}\n"
        f"\u2022 Subtotal: {sym}{subtotal:.2f}\n"
        f"\u2022 VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
        f"\u2022 *Total: {sym}{total:.2f}*\n"
        f"\u2022 Valid until: {valid_until}\n\n"
        f"\U0001f4e4 View/download PDF:\n{pdf_url}\n\n"
        f"Reply to accept or ask any questions!"
    )

    customer_raw = customer_phone.lstrip("+")
    try:
        await send_text_message(client, customer_raw, quote_msg)
    except Exception:
        logger.exception("Failed to send quote to %s", customer_phone)
        await send_text_message(
            client, sender,
            f"\u2705 Quote {quote_number} created for {sym}{total:.2f}, "
            f"but failed to send to {customer_name}. "
            f"You can send the PDF from your dashboard.",
        )
        return

    # Mark sent timestamp & status
    from datetime import datetime, timezone
    supabase.table("quotes").update({
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", quo["id"]).execute()

    log_message(
        business_id=business["id"],
        to_phone=customer_phone,
        message_body=quote_msg,
        message_type="quote",
    )

    await send_text_message(
        client, sender,
        f"\u2705 Quote {quote_number} created & sent to {customer_name} "
        f"({customer_phone}).\n\n"
        f"\u2022 {description}: {sym}{total:.2f} (inc. VAT)\n"
        f"\u2022 Valid until {valid_until}\n\n"
        f"View or download the PDF from your dashboard.",
    )
    logger.info(
        "QUOTE: biz=%s customer=%s quo=%s total=%s",
        business["id"], customer_phone, quote_number, total,
    )


# ──────────────────────────────────────────────
# /CHAT <Name or Phone>
# ──────────────────────────────────────────────
async def _cmd_chat(
    sender: str, args: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Switch the active customer for free-text chat."""
    supabase = get_supabase()

    if not args:
        await send_text_message(
            client, sender,
            "Usage: /CHAT <Customer Name or Phone>\n"
            "Example: /CHAT John  or  /CHAT 07845774563",
        )
        return

    # Try to match by phone if the arg looks like a number
    phone_digits = re.sub(r"\D", "", args)
    if len(phone_digits) >= 7:
        normalised = _normalise_phone(phone_digits)
        cust_result = (
            supabase.table("customers")
            .select("name, phone_number")
            .eq("business_id", business["id"])
            .eq("phone_number", normalised)
            .limit(1)
            .execute()
        )
    else:
        # Match by name (case-insensitive)
        all_custs = (
            supabase.table("customers")
            .select("name, phone_number")
            .eq("business_id", business["id"])
            .execute()
        )
        name_lower = args.lower()
        matches = [
            c for c in (all_custs.data or [])
            if name_lower in c["name"].lower()
        ]
        cust_result = type(all_custs)(matches[:1], len(matches))

    if not cust_result.data:
        await send_text_message(
            client, sender,
            "Customer not found. Use /SETUP to add them first.",
        )
        return

    customer = cust_result.data[0]
    supabase.table("businesses").update(
        {"active_customer_phone": customer["phone_number"]}
    ).eq("id", business["id"]).execute()

    await send_text_message(
        client, sender,
        f"\U0001f4ac Now chatting with {customer['name']} "
        f"({customer['phone_number']}). Just type your message.",
    )


# ──────────────────────────────────────────────
# /HELP
# ──────────────────────────────────────────────
async def _cmd_login(sender: str, business: dict[str, Any], client: httpx.AsyncClient) -> None:
    """Generate a one-time login code and send a direct login link."""
    import secrets
    from datetime import datetime, timezone, timedelta
    from app.core.config import get_settings

    supabase = get_supabase()
    settings = get_settings()

    code = f"{secrets.randbelow(900000) + 100000}"
    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    supabase.table("auth_codes").insert({
        "phone": sender,
        "code": code,
        "expires_at": expires,
    }).execute()

    base = settings.base_url.rstrip("/")
    login_url = f"{base}/login.html"

    msg = (
        f"\U0001f513 *Your login code is:* *{code}*\n\n"
        f"\U0001f449 Open your dashboard:\n{login_url}\n\n"
        f"Enter your phone number and this code to log in.\n"
        f"Code expires in 5 minutes."
    )
    await send_text_message(client, sender, msg)


async def _cmd_help(sender: str, question: str, client: httpx.AsyncClient) -> None:
    if not question:
        # No question — show quick command reference
        help_text = (
            "\U0001f4cb *Available Commands*\n\n"
            "/SETUP <Name> <Phone>\n"
            "  Add a new customer & send a welcome message\n\n"
            "/REVIEW [Name Phone]\n"
            "  Send a review request (active customer if no args)\n\n"
            "/INVOICE [Amount] [Description]\n"
            "  Create & send an invoice — I'll ask for any missing info\n\n"
            "/QUOTE [Amount] [Description]\n"
            "  Create & send a quote — same guided flow as invoices\n\n"
            "/CHAT <Name or Phone>\n"
            "  Switch your active chat to a different customer\n\n"
            "/LOGIN\n"
            "  Get a login code & link to your dashboard\n\n"
            "/HELP <question>\n"
            "  Ask the AI assistant anything about the app\n\n"
            "\U0001f4ac Type any normal text to message your active customer.\n\n"
            "\U0001f4a1 *Tip:* Try /HELP how do I send an invoice?"
        )
        await send_text_message(client, sender, help_text)
        return

    # Has a question — use AI to answer
    from app.services.openai_service import answer_help_question

    await send_text_message(client, sender, "\U0001f914 Let me look that up for you...")

    try:
        answer = await answer_help_question(question)
        await send_text_message(client, sender, f"\U0001f4a1 *Help*\n\n{answer}")
    except Exception as exc:
        logger.error("AI help failed: %s", exc)
        await send_text_message(
            client, sender,
            "Sorry, I couldn't get an answer right now. Please try again or send /HELP for the command list.",
        )


# ──────────────────────────────────────────────
# Chat relay: tradesperson → customer
# ──────────────────────────────────────────────
async def _relay_to_customer(
    sender: str, text: str, business: dict[str, Any], client: httpx.AsyncClient
) -> None:
    """Wrap the tradesperson's message with their business name and forward."""
    customer_phone = business.get("active_customer_phone") or ""
    if not customer_phone:
        await send_text_message(
            client, sender,
            "No active customer. Use /SETUP or /CHAT first.",
        )
        return

    biz_name = business["business_name"]
    wrapped = f"[{biz_name}]: {text}"

    customer_raw = customer_phone.lstrip("+")
    try:
        await send_text_message(client, customer_raw, wrapped)
    except Exception:
        logger.exception("Failed to relay message to %s", customer_phone)
        await send_text_message(
            client, sender,
            "\u26a0\ufe0f Failed to deliver your message. The customer may "
            "need to reply to this number first to open the chat window.",
        )
        return

    log_message(
        business_id=business["id"],
        to_phone=customer_phone,
        message_body=wrapped,
        message_type="chat",
    )


# ──────────────────────────────────────────────
# Chat relay: customer → tradesperson
# ──────────────────────────────────────────────
async def _handle_customer_text(
    sender: str, sender_e164: str, text: str, client: httpx.AsyncClient
) -> None:
    """Relay a customer's message back to their tradesperson."""
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
    owner_phone = biz["phone_number"].lstrip("+")
    customer_name = customer.get("name", "Customer")

    # Relay to tradesperson with customer name prefix
    relay_msg = f"\U0001f4ac {customer_name}: {text}"
    await send_text_message(client, owner_phone, relay_msg)

    log_message(
        business_id=customer["business_id"],
        to_phone=biz["phone_number"],
        message_body=relay_msg,
        message_type="chat_relay",
        direction="inbound",
    )

    # Auto-set this customer as active so the tradesperson can reply directly
    supabase.table("businesses").update(
        {"active_customer_phone": sender_e164}
    ).eq("id", customer["business_id"]).execute()


def _normalise_phone(raw: str) -> str:
    """Normalise a raw phone input to E.164."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0"):
        digits = "44" + digits[1:]
    return f"+{digits}"


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
    inv_result = supabase.table("invoices").select("*").eq("id", inv_id).execute()
    if not inv_result.data:
        await send_text_message(client, sender, "Invoice not found.")
        return
    inv = inv_result.data[0]

    biz_result = supabase.table("businesses").select("*").eq("id", inv["business_id"]).execute()
    business = biz_result.data[0] if biz_result.data else {}

    cust_result = (
        supabase.table("customers").select("id, name, phone_number")
        .eq("id", inv["customer_id"]).execute()
    )
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_phone = customer["phone_number"] if customer else ""
    first_name = customer_name.split()[0]

    currency = inv.get("currency", "GBP")
    sym = "\u00a3" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "
    settings = get_settings()
    pdf_url = f"{settings.base_url}/member/business/{inv['business_id']}/invoices/{inv_id}/pdf"

    await _send_invoice_to_customer(
        sender, inv, invoice_number=inv["invoice_number"],
        description=inv.get("notes", ""),
        subtotal=inv["subtotal"], tax_rate=inv["tax_rate"],
        tax_amount=inv["tax_amount"], total=inv["total"],
        sym=sym, pdf_url=pdf_url,
        customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=business.get("business_name", ""),
        business=business, client=client,
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
    await send_text_message(
        client, sender,
        f"\u274c Invoice {inv_number} has been cancelled and deleted.",
    )


async def _confirm_send_quote(
    sender: str, quo_id: str, client: httpx.AsyncClient
) -> None:
    """User tapped Send on a quote preview — deliver it now."""
    supabase = get_supabase()
    quo_result = supabase.table("quotes").select("*").eq("id", quo_id).execute()
    if not quo_result.data:
        await send_text_message(client, sender, "Quote not found.")
        return
    quo = quo_result.data[0]

    biz_result = supabase.table("businesses").select("*").eq("id", quo["business_id"]).execute()
    business = biz_result.data[0] if biz_result.data else {}

    cust_result = (
        supabase.table("customers").select("id, name, phone_number")
        .eq("id", quo["customer_id"]).execute()
    )
    customer = cust_result.data[0] if cust_result.data else None
    customer_name = customer["name"] if customer else "there"
    customer_phone = customer["phone_number"] if customer else ""
    first_name = customer_name.split()[0]

    currency = quo.get("currency", "GBP")
    sym = "\u00a3" if currency == "GBP" else "$" if currency == "USD" else f"{currency} "
    settings = get_settings()
    pdf_url = f"{settings.base_url}/member/business/{quo['business_id']}/quotes/{quo_id}/pdf"

    await _send_quote_to_customer(
        sender, quo, quote_number=quo["quote_number"],
        description=quo.get("notes", ""),
        subtotal=quo["subtotal"], tax_rate=quo["tax_rate"],
        tax_amount=quo["tax_amount"], total=quo["total"],
        sym=sym, pdf_url=pdf_url,
        valid_until=quo.get("valid_until", ""),
        customer_name=customer_name, customer_phone=customer_phone,
        first_name=first_name, biz_name=business.get("business_name", ""),
        business=business, client=client,
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
    await send_text_message(
        client, sender,
        f"\u274c Quote {quo_number} has been cancelled and deleted.",
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

    # ── 5. "yes_offers" — customer opts in to marketing ──
    if payload == "yes_offers":
        supabase.table("customers").update({"marketing_opt_in": 1}).eq(
            "phone_number", sender_e164
        ).execute()
        await send_text_message(
            client, sender,
            "Great, you're signed up! 🎉 We'll only send you the best offers. Reply STOP anytime to unsubscribe.",
        )
        return

    # ── 6. "no_offers" — customer declines marketing ──
    if payload == "no_offers":
        await send_text_message(
            client, sender,
            "No problem at all! You won't receive any marketing messages from us.",
        )
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

    # Ask customer if they want to receive special offers
    biz_name = biz.get("business_name", "us") if biz else "us"
    await send_interactive_buttons(
        client,
        sender,
        f"Would you like to receive occasional special offers from {biz_name}? You can opt out anytime by replying STOP.",
        [
            {"id": "yes_offers", "title": "Yes please!"},
            {"id": "no_offers", "title": "No thanks"},
        ],
    )


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
            f"⚠️ {customer_name} ({customer_phone}) had a bad experience. "
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
