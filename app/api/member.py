"""Member portal API — lets business owners manage their account."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from app.db.supabase import get_supabase
from app.services.whatsapp import send_text_message, upload_media, send_document_message
from app.services.message_log import log_message
from app.services.pdf_generator import generate_invoice_pdf, generate_quote_pdf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/member", tags=["member"])

# ── Rate-limit constants ─────────────────────────
MAX_CAMPAIGN_SENDS_PER_DAY = 50        # per business
DELAY_BETWEEN_SENDS_SECS   = 2         # stagger to avoid Meta throttling


# ── Models ───────────────────────────────────────────

class BusinessUpdate(BaseModel):
    business_name: str | None = None
    owner_name: str | None = None
    trade_type: str | None = None
    phone_number: str | None = None
    email: str | None = None
    google_review_link: str | None = None
    follow_up_enabled: bool | None = None
    follow_up_days: int | None = None
    max_follow_ups: int | None = None
    follow_up_message: str | None = None
    follow_up_message_2: str | None = None
    follow_up_message_3: str | None = None
    auto_reply_enabled: bool | None = None
    auto_reply_threshold: int | None = None
    auto_reply_positive_msg: str | None = None
    auto_reply_negative_msg: str | None = None
    # Address & invoicing settings
    business_address: str | None = None
    business_city: str | None = None
    business_postcode: str | None = None
    business_country: str | None = None
    tax_label: str | None = None
    tax_number: str | None = None
    tax_rate: float | None = None
    default_payment_terms: str | None = None
    bank_details: str | None = None
    accepted_payment_methods: str | None = None
    payment_link: str | None = None
    currency: str | None = None
    confirm_before_send: bool | None = None


class LineItemIn(BaseModel):
    description: str
    quantity: float = 1
    unit_price: float = 0


class InvoiceCreate(BaseModel):
    customer_id: str | None = None
    payment_terms: str | None = None
    notes: str = ""
    due_date: str | None = None
    line_items: list[LineItemIn] = []


class InvoiceUpdate(BaseModel):
    customer_id: str | None = None
    status: str | None = None
    payment_terms: str | None = None
    notes: str | None = None
    due_date: str | None = None
    line_items: list[LineItemIn] | None = None


class QuoteCreate(BaseModel):
    customer_id: str | None = None
    valid_until: str | None = None
    notes: str = ""
    line_items: list[LineItemIn] = []


class QuoteUpdate(BaseModel):
    customer_id: str | None = None
    status: str | None = None
    valid_until: str | None = None
    notes: str | None = None
    line_items: list[LineItemIn] | None = None


class MarkPaid(BaseModel):
    payment_method: str = ""


# ── Business info ────────────────────────────────────

@router.get("/business/{business_id}")
async def get_business(business_id: str) -> dict:
    """Fetch the business profile for the member portal."""
    db = get_supabase()
    result = db.table("businesses").select("*").eq("id", business_id).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Business not found")
    return result.data


@router.patch("/business/{business_id}")
async def update_business(business_id: str, body: BusinessUpdate) -> dict:
    """Update business profile fields."""
    db = get_supabase()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = db.table("businesses").update(updates).eq("id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Business not found")
    return result.data[0]


# ── Customers list ───────────────────────────────────

@router.get("/business/{business_id}/customers")
async def list_customers(business_id: str) -> list[dict]:
    """List all customers for this business."""
    db = get_supabase()
    result = (
        db.table("customers")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


# ── Review drafts ────────────────────────────────────

@router.get("/business/{business_id}/drafts")
async def list_drafts(business_id: str) -> list[dict]:
    """List all review drafts for this business."""
    db = get_supabase()
    result = (
        db.table("review_drafts")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


# ── Stats ────────────────────────────────────────────

@router.get("/business/{business_id}/stats")
async def get_stats(business_id: str) -> dict:
    """Quick stats for the member dashboard."""
    db = get_supabase()

    customers = (
        db.table("customers")
        .select("*", count="exact")
        .eq("business_id", business_id)
        .execute()
    )
    reviews_sent = customers.count or 0

    clicked_great = (
        db.table("customers")
        .select("*", count="exact")
        .eq("business_id", business_id)
        .eq("status", "clicked_great")
        .execute()
    )

    pending_drafts = (
        db.table("review_drafts")
        .select("*", count="exact")
        .eq("business_id", business_id)
        .eq("status", "pending_approval")
        .execute()
    )

    return {
        "total_review_requests": reviews_sent,
        "positive_responses": clicked_great.count or 0,
        "pending_drafts": pending_drafts.count or 0,
    }


# ── Message history ──────────────────────────────────

@router.get("/business/{business_id}/messages")
async def list_messages(business_id: str) -> list[dict]:
    """List all logged messages for this business, newest first."""
    db = get_supabase()
    result = (
        db.table("messages")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


# ── Campaigns ────────────────────────────────────────

class CampaignCreate(BaseModel):
    message_body: str


@router.get("/business/{business_id}/campaigns")
async def list_campaigns(business_id: str) -> list[dict]:
    """List all campaigns for this business, newest first."""
    db = get_supabase()
    result = (
        db.table("campaigns")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@router.get("/business/{business_id}/opted-in-count")
async def opted_in_count(business_id: str) -> dict:
    """Return the number of marketing-opted-in customers."""
    db = get_supabase()
    result = (
        db.table("customers")
        .select("*", count="exact")
        .eq("business_id", business_id)
        .eq("marketing_opt_in", 1)
        .execute()
    )
    return {"opted_in": result.count or 0}


@router.post("/business/{business_id}/campaigns")
async def create_campaign(
    business_id: str, body: CampaignCreate, request: Request
) -> dict:
    """Create a campaign and send it to all opted-in customers (throttled)."""
    db = get_supabase()
    client = request.app.state.http_client
    message = body.message_body.strip()

    if not message:
        raise HTTPException(status_code=400, detail="Message body is required")
    if len(message) > 1000:
        raise HTTPException(status_code=400, detail="Message too long (max 1000 chars)")

    # Check daily send limit
    from datetime import datetime, timezone
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    today_campaigns = (
        db.table("campaigns")
        .select("sent_count")
        .eq("business_id", business_id)
        .gte("created_at", today_start)
        .execute()
    )
    sent_today = sum(c.get("sent_count", 0) for c in (today_campaigns.data or []))
    remaining = MAX_CAMPAIGN_SENDS_PER_DAY - sent_today
    if remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail=f"Daily send limit reached ({MAX_CAMPAIGN_SENDS_PER_DAY}/day). Try again tomorrow.",
        )

    # Fetch opted-in customers
    opted_in = (
        db.table("customers")
        .select("id, name, phone_number")
        .eq("business_id", business_id)
        .eq("marketing_opt_in", 1)
        .execute()
    )
    recipients = opted_in.data or []
    if not recipients:
        raise HTTPException(
            status_code=400,
            detail="No customers have opted in to marketing messages yet.",
        )

    # Cap to daily remaining
    recipients = recipients[:remaining]

    # Create campaign record
    campaign_result = (
        db.table("campaigns")
        .insert({
            "business_id": business_id,
            "message_body": message,
            "total_recipients": len(recipients),
            "status": "sending",
        })
        .execute()
    )
    campaign = campaign_result.data[0]
    campaign_id = campaign["id"]

    # Send messages with throttling
    sent = 0
    failed = 0
    for i, cust in enumerate(recipients):
        try:
            await send_text_message(client, cust["phone_number"], message)
            log_message(
                business_id=business_id,
                to_phone=cust["phone_number"],
                message_body=message,
                message_type="campaign",
            )
            sent += 1
        except Exception:
            logger.exception("Campaign send failed for %s", cust["phone_number"])
            failed += 1

        # Stagger sends to avoid rate limiting
        if i < len(recipients) - 1:
            await asyncio.sleep(DELAY_BETWEEN_SENDS_SECS)

    # Update campaign record
    final_status = "completed" if failed == 0 else ("failed" if sent == 0 else "partial")
    db.table("campaigns").update({
        "sent_count": sent,
        "failed_count": failed,
        "status": final_status,
    }).eq("id", campaign_id).execute()

    return {
        "campaign_id": campaign_id,
        "sent": sent,
        "failed": failed,
        "status": final_status,
    }


# ── Helper: recalculate totals & persist line items ──

def _save_line_items(db, parent_id: str, parent_type: str, items: list[LineItemIn], tax_rate: float):
    """Delete old line items, insert new ones, return (subtotal, tax_amount, total)."""
    # Remove existing
    db.table("line_items").delete().eq("parent_id", parent_id).eq("parent_type", parent_type).execute()

    subtotal = 0.0
    for idx, item in enumerate(items):
        line_total = round(item.quantity * item.unit_price, 2)
        subtotal += line_total
        db.table("line_items").insert({
            "parent_id": parent_id,
            "parent_type": parent_type,
            "description": item.description,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "total": line_total,
            "sort_order": idx,
        }).execute()

    tax_amount = round(subtotal * tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)
    return subtotal, tax_amount, total


def _next_number(db, business_id: str, table: str, prefix: str) -> str:
    """Generate next sequential number like INV-0001 or QUO-0001."""
    result = (
        db.table(table)
        .select("*", count="exact")
        .eq("business_id", business_id)
        .execute()
    )
    seq = (result.count or 0) + 1
    return f"{prefix}-{seq:04d}"


def _get_business_tax_rate(db, business_id: str) -> float:
    biz = db.table("businesses").select("tax_rate").eq("id", business_id).single().execute()
    return biz.data.get("tax_rate", 20.0) if biz.data else 20.0


# ── Invoices CRUD ────────────────────────────────────

@router.get("/business/{business_id}/invoices")
async def list_invoices(business_id: str) -> list[dict]:
    db = get_supabase()
    result = (
        db.table("invoices")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@router.get("/business/{business_id}/invoices/{invoice_id}")
async def get_invoice(business_id: str, invoice_id: str) -> dict:
    db = get_supabase()
    inv = db.table("invoices").select("*").eq("id", invoice_id).eq("business_id", business_id).single().execute()
    if not inv.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    items = (
        db.table("line_items")
        .select("*")
        .eq("parent_id", invoice_id)
        .eq("parent_type", "invoice")
        .order("sort_order")
        .execute()
    )
    inv.data["line_items"] = items.data or []
    return inv.data


@router.post("/business/{business_id}/invoices")
async def create_invoice(business_id: str, body: InvoiceCreate) -> dict:
    db = get_supabase()
    tax_rate = _get_business_tax_rate(db, business_id)
    inv_number = _next_number(db, business_id, "invoices", "INV")

    # Get default payment terms from business if not provided
    biz = db.table("businesses").select("default_payment_terms, currency").eq("id", business_id).single().execute()
    payment_terms = body.payment_terms or (biz.data.get("default_payment_terms", "Payment due within 14 days") if biz.data else "Payment due within 14 days")
    currency = biz.data.get("currency", "GBP") if biz.data else "GBP"

    inv_result = db.table("invoices").insert({
        "business_id": business_id,
        "customer_id": body.customer_id or "",
        "invoice_number": inv_number,
        "status": "draft",
        "tax_rate": tax_rate,
        "currency": currency,
        "payment_terms": payment_terms,
        "notes": body.notes,
        "due_date": body.due_date or "",
    }).execute()
    inv = inv_result.data[0]

    if body.line_items:
        subtotal, tax_amount, total = _save_line_items(db, inv["id"], "invoice", body.line_items, tax_rate)
        db.table("invoices").update({
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            "total": total,
        }).eq("id", inv["id"]).execute()
        inv.update(subtotal=subtotal, tax_amount=tax_amount, total=total)

    return inv


@router.patch("/business/{business_id}/invoices/{invoice_id}")
async def update_invoice(business_id: str, invoice_id: str, body: InvoiceUpdate) -> dict:
    db = get_supabase()
    existing = db.table("invoices").select("*").eq("id", invoice_id).eq("business_id", business_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Invoice not found")

    updates = {}
    if body.customer_id is not None:
        updates["customer_id"] = body.customer_id
    if body.status is not None:
        updates["status"] = body.status
        if body.status == "paid":
            from datetime import datetime, timezone
            updates["paid_at"] = datetime.now(timezone.utc).isoformat()
        elif body.status == "sent":
            from datetime import datetime, timezone
            updates["sent_at"] = datetime.now(timezone.utc).isoformat()
    if body.payment_terms is not None:
        updates["payment_terms"] = body.payment_terms
    if body.notes is not None:
        updates["notes"] = body.notes
    if body.due_date is not None:
        updates["due_date"] = body.due_date

    if body.line_items is not None:
        tax_rate = existing.data.get("tax_rate", 20.0)
        subtotal, tax_amount, total = _save_line_items(db, invoice_id, "invoice", body.line_items, tax_rate)
        updates.update(subtotal=subtotal, tax_amount=tax_amount, total=total)

    if updates:
        db.table("invoices").update(updates).eq("id", invoice_id).execute()

    return (await get_invoice(business_id, invoice_id))


@router.delete("/business/{business_id}/invoices/{invoice_id}")
async def delete_invoice(business_id: str, invoice_id: str) -> dict:
    db = get_supabase()
    existing = db.table("invoices").select("*").eq("id", invoice_id).eq("business_id", business_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    db.table("line_items").delete().eq("parent_id", invoice_id).eq("parent_type", "invoice").execute()
    db.table("invoices").delete().eq("id", invoice_id).execute()
    return {"deleted": True}


@router.post("/business/{business_id}/invoices/{invoice_id}/mark-paid")
async def mark_invoice_paid(business_id: str, invoice_id: str, body: MarkPaid) -> dict:
    """Mark an invoice as paid and record the payment method."""
    db = get_supabase()
    existing = db.table("invoices").select("*").eq("id", invoice_id).eq("business_id", business_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Invoice not found")

    from datetime import datetime, timezone
    db.table("invoices").update({
        "status": "paid",
        "payment_method": body.payment_method,
        "paid_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", invoice_id).execute()

    return {"marked_paid": True, "payment_method": body.payment_method}


# ── Quotes CRUD ──────────────────────────────────────

@router.get("/business/{business_id}/quotes")
async def list_quotes(business_id: str) -> list[dict]:
    db = get_supabase()
    result = (
        db.table("quotes")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@router.get("/business/{business_id}/quotes/{quote_id}")
async def get_quote(business_id: str, quote_id: str) -> dict:
    db = get_supabase()
    q = db.table("quotes").select("*").eq("id", quote_id).eq("business_id", business_id).single().execute()
    if not q.data:
        raise HTTPException(status_code=404, detail="Quote not found")
    items = (
        db.table("line_items")
        .select("*")
        .eq("parent_id", quote_id)
        .eq("parent_type", "quote")
        .order("sort_order")
        .execute()
    )
    q.data["line_items"] = items.data or []
    return q.data


@router.post("/business/{business_id}/quotes")
async def create_quote(business_id: str, body: QuoteCreate) -> dict:
    db = get_supabase()
    tax_rate = _get_business_tax_rate(db, business_id)
    q_number = _next_number(db, business_id, "quotes", "QUO")

    biz = db.table("businesses").select("currency").eq("id", business_id).single().execute()
    currency = biz.data.get("currency", "GBP") if biz.data else "GBP"

    q_result = db.table("quotes").insert({
        "business_id": business_id,
        "customer_id": body.customer_id or "",
        "quote_number": q_number,
        "status": "draft",
        "tax_rate": tax_rate,
        "currency": currency,
        "valid_until": body.valid_until or "",
        "notes": body.notes,
    }).execute()
    q = q_result.data[0]

    if body.line_items:
        subtotal, tax_amount, total = _save_line_items(db, q["id"], "quote", body.line_items, tax_rate)
        db.table("quotes").update({
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            "total": total,
        }).eq("id", q["id"]).execute()
        q.update(subtotal=subtotal, tax_amount=tax_amount, total=total)

    return q


@router.patch("/business/{business_id}/quotes/{quote_id}")
async def update_quote(business_id: str, quote_id: str, body: QuoteUpdate) -> dict:
    db = get_supabase()
    existing = db.table("quotes").select("*").eq("id", quote_id).eq("business_id", business_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Quote not found")

    updates = {}
    if body.customer_id is not None:
        updates["customer_id"] = body.customer_id
    if body.status is not None:
        updates["status"] = body.status
    if body.valid_until is not None:
        updates["valid_until"] = body.valid_until
    if body.notes is not None:
        updates["notes"] = body.notes

    if body.line_items is not None:
        tax_rate = existing.data.get("tax_rate", 20.0)
        subtotal, tax_amount, total = _save_line_items(db, quote_id, "quote", body.line_items, tax_rate)
        updates.update(subtotal=subtotal, tax_amount=tax_amount, total=total)

    if updates:
        db.table("quotes").update(updates).eq("id", quote_id).execute()

    return (await get_quote(business_id, quote_id))


@router.delete("/business/{business_id}/quotes/{quote_id}")
async def delete_quote(business_id: str, quote_id: str) -> dict:
    db = get_supabase()
    existing = db.table("quotes").select("*").eq("id", quote_id).eq("business_id", business_id).single().execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Quote not found")
    db.table("line_items").delete().eq("parent_id", quote_id).eq("parent_type", "quote").execute()
    db.table("quotes").delete().eq("id", quote_id).execute()
    return {"deleted": True}


# ── Accounts / Income summary ────────────────────────

@router.get("/business/{business_id}/accounts")
async def get_accounts(business_id: str) -> dict:
    """Return income tracking data: paid total, outstanding total, and monthly breakdown."""
    db = get_supabase()
    invoices = (
        db.table("invoices")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []

    total_paid = 0.0
    total_outstanding = 0.0
    monthly: dict[str, dict] = {}  # "2025-06" -> {paid, outstanding, count}

    for inv in invoices:
        total = float(inv.get("total", 0) or 0)
        is_paid = inv.get("status") == "paid"

        if is_paid:
            total_paid += total
        elif inv.get("status") not in ("cancelled",):
            total_outstanding += total

        # Group by month using paid_at for paid invoices, else created_at
        date_str = inv.get("paid_at") if is_paid else inv.get("created_at")
        if date_str:
            month_key = date_str[:7]  # "YYYY-MM"
        else:
            month_key = "unknown"

        if month_key not in monthly:
            monthly[month_key] = {"paid": 0.0, "outstanding": 0.0, "count": 0}
        monthly[month_key]["count"] += 1
        if is_paid:
            monthly[month_key]["paid"] += total
        elif inv.get("status") not in ("cancelled",):
            monthly[month_key]["outstanding"] += total

    # Sort months descending
    sorted_months = sorted(monthly.items(), key=lambda x: x[0], reverse=True)

    return {
        "total_paid": round(total_paid, 2),
        "total_outstanding": round(total_outstanding, 2),
        "months": [
            {"month": k, "paid": round(v["paid"], 2), "outstanding": round(v["outstanding"], 2), "count": v["count"]}
            for k, v in sorted_months
        ],
        "invoices": invoices,
    }


# ── PDF generation helpers ───────────────────────────

def _build_payment_link(base_link: str, total: float, currency: str) -> str:
    """Build a payment link with the invoice amount appended if possible.

    Supports PayPal.me style links (amount appended to path) and
    generic links returned as-is.
    """
    if not base_link:
        return ""
    base_link = base_link.strip()
    # PayPal.me: https://paypal.me/username/AMOUNT or paypal.me/username/AMOUNT
    if "paypal.me/" in base_link.lower():
        # Strip trailing slashes then append /AMOUNT/CURRENCY
        base = base_link.rstrip("/")
        return f"{base}/{total:.2f}"
    # For other links (Stripe, SumUp, generic), return as-is
    return base_link


def _fetch_pdf_context(db, business_id: str, record_id: str, table: str, item_type: str):
    """Fetch business, record and line items for PDF generation."""
    biz = db.table("businesses").select("*").eq("id", business_id).single().execute()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")

    rec = db.table(table).select("*").eq("id", record_id).eq("business_id", business_id).single().execute()
    if not rec.data:
        raise HTTPException(status_code=404, detail=f"{item_type.title()} not found")

    customer = None
    if rec.data.get("customer_id"):
        cust_res = db.table("customers").select("*").eq("id", rec.data["customer_id"]).single().execute()
        customer = cust_res.data

    items = (
        db.table("line_items")
        .select("*")
        .eq("parent_id", record_id)
        .eq("parent_type", item_type)
        .order("sort_order")
        .execute()
    )
    return biz.data, rec.data, customer, items.data or []


# ── Invoice PDF endpoints ────────────────────────────

@router.get("/business/{business_id}/invoices/{invoice_id}/pdf")
async def download_invoice_pdf(business_id: str, invoice_id: str) -> Response:
    """Download the invoice as a PDF."""
    db = get_supabase()
    biz, inv, customer, items = _fetch_pdf_context(db, business_id, invoice_id, "invoices", "invoice")
    pdf_bytes = generate_invoice_pdf(biz, customer, inv, items)
    filename = f"{inv.get('invoice_number', 'invoice')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/business/{business_id}/invoices/{invoice_id}/send-whatsapp")
async def send_invoice_whatsapp(business_id: str, invoice_id: str, request: Request) -> dict:
    """Generate PDF and send it to the customer via WhatsApp."""
    db = get_supabase()
    biz, inv, customer, items = _fetch_pdf_context(db, business_id, invoice_id, "invoices", "invoice")

    if not customer:
        raise HTTPException(status_code=400, detail="No customer linked to this invoice")

    pdf_bytes = generate_invoice_pdf(biz, customer, inv, items)
    filename = f"{inv.get('invoice_number', 'invoice')}.pdf"
    client = request.app.state.http_client

    media_id = await upload_media(client, pdf_bytes, filename=filename)
    caption = f"Invoice {inv.get('invoice_number', '')} from {biz.get('business_name', '')} — Total: {inv.get('total', 0):.2f}"
    await send_document_message(client, customer["phone_number"], media_id, filename=filename, caption=caption)

    log_message(
        business_id=business_id,
        to_phone=customer["phone_number"],
        message_body=f"Invoice PDF: {filename}",
        message_type="invoice",
    )

    # Build and send payment link if the business has one
    pay_link = _build_payment_link(biz.get("payment_link", ""), inv.get("total", 0), inv.get("currency", "GBP"))
    if pay_link:
        # Save the payment link on the invoice
        db.table("invoices").update({"payment_link": pay_link}).eq("id", invoice_id).execute()
        pay_msg = f"💳 Pay online: {pay_link}"
        await send_text_message(client, customer["phone_number"], pay_msg)
        log_message(
            business_id=business_id,
            to_phone=customer["phone_number"],
            message_body=pay_msg,
            message_type="invoice",
        )

    # Mark as sent
    from datetime import datetime, timezone
    db.table("invoices").update({
        "status": "sent",
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", invoice_id).execute()

    return {"sent": True, "filename": filename, "payment_link": pay_link}


# ── Quote PDF endpoints ──────────────────────────────

@router.get("/business/{business_id}/quotes/{quote_id}/pdf")
async def download_quote_pdf(business_id: str, quote_id: str) -> Response:
    """Download the quote as a PDF."""
    db = get_supabase()
    biz, q, customer, items = _fetch_pdf_context(db, business_id, quote_id, "quotes", "quote")
    pdf_bytes = generate_quote_pdf(biz, customer, q, items)
    filename = f"{q.get('quote_number', 'quote')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/business/{business_id}/quotes/{quote_id}/send-whatsapp")
async def send_quote_whatsapp(business_id: str, quote_id: str, request: Request) -> dict:
    """Generate PDF and send it to the customer via WhatsApp."""
    db = get_supabase()
    biz, q, customer, items = _fetch_pdf_context(db, business_id, quote_id, "quotes", "quote")

    if not customer:
        raise HTTPException(status_code=400, detail="No customer linked to this quote")

    pdf_bytes = generate_quote_pdf(biz, customer, q, items)
    filename = f"{q.get('quote_number', 'quote')}.pdf"
    client = request.app.state.http_client

    media_id = await upload_media(client, pdf_bytes, filename=filename)
    caption = f"Quote {q.get('quote_number', '')} from {biz.get('business_name', '')} — Total: {q.get('total', 0):.2f}"
    await send_document_message(client, customer["phone_number"], media_id, filename=filename, caption=caption)

    log_message(
        business_id=business_id,
        to_phone=customer["phone_number"],
        message_body=f"Quote PDF: {filename}",
        message_type="quote",
    )

    # Mark as sent
    db.table("quotes").update({"status": "sent"}).eq("id", quote_id).execute()

    return {"sent": True, "filename": filename}
