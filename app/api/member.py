"""Member portal API — lets business owners manage their account."""

import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Depends, UploadFile, File
from fastapi.responses import Response, FileResponse
from pydantic import BaseModel

from app.api.auth import get_current_business
from app.db.supabase import get_supabase
from app.services.whatsapp import send_text_message, upload_media, send_document_message
from app.services.message_log import log_message
from app.services.pdf_generator import generate_invoice_pdf, generate_quote_pdf

logger = logging.getLogger(__name__)

_LOGO_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
_LOGO_ALLOWED_TYPES = {"image/png", "image/jpeg", "image/webp"}


def _logos_dir() -> Path:
    vol = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    base = Path(vol) if vol else Path(__file__).resolve().parent.parent.parent
    d = base / "logos"
    d.mkdir(exist_ok=True)
    return d
router = APIRouter(
    prefix="/member", tags=["member"],
    dependencies=[Depends(get_current_business)],
)

# Public router for PDF downloads (no auth required — customers tap these links)
public_router = APIRouter(prefix="/member", tags=["member-public"])

# ── Models ───────────────────────────────────────────

class BusinessUpdate(BaseModel):
    business_name: str | None = None
    owner_name: str | None = None
    trade_type: str | None = None
    phone_number: str | None = None
    email: str | None = None
    google_review_link: str | None = None
    google_place_id: str | None = None
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
    followup_enabled: bool | None = None
    followup_interval_days: int | None = None
    followup_max_count: int | None = None
    followup_message: str | None = None
    vat_registered: bool | None = None
    brand_color: str | None = None
    logo_url: str | None = None


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
    data = result.data
    # Replace sensitive token with a boolean flag
    data["google_connected"] = bool(
        data.get("google_refresh_token")
        and data.get("google_account_id")
        and data.get("google_location_id")
    )
    data.pop("google_refresh_token", None)
    data.pop("oauth_state", None)
    return data


@router.patch("/business/{business_id}")
async def update_business(business_id: str, body: BusinessUpdate) -> dict:
    """Update business profile fields."""
    db = get_supabase()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Moderate follow-up message before saving (sent to all customers automatically)
    if "followup_message" in updates and updates["followup_message"]:
        from app.services.moderation import moderate_outbound
        mod_warning = await moderate_outbound(updates["followup_message"])
        if mod_warning:
            raise HTTPException(status_code=400, detail="Follow-up message blocked by content moderation")

    result = db.table("businesses").update(updates).eq("id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Business not found")
    return result.data[0]


@router.post("/business/{business_id}/logo")
async def upload_logo(business_id: str, file: UploadFile = File(...)) -> dict:
    """Upload a logo image for the business (max 2 MB, PNG/JPEG/WebP)."""
    if file.content_type not in _LOGO_ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Only PNG, JPEG, and WebP images are allowed")

    data = await file.read()
    if len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(status_code=400, detail="Logo must be under 2 MB")

    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[file.content_type]
    filename = f"{business_id}{ext}"
    dest = _logos_dir() / filename
    dest.write_bytes(data)

    from app.core.config import get_settings
    logo_url = f"{get_settings().base_url}/member/business/{business_id}/logo/{filename}"

    db = get_supabase()
    db.table("businesses").update({"logo_url": logo_url}).eq("id", business_id).execute()

    return {"logo_url": logo_url}


@router.delete("/business/{business_id}/logo")
async def delete_logo(business_id: str) -> dict:
    """Remove the business logo."""
    db = get_supabase()
    db.table("businesses").update({"logo_url": ""}).eq("id", business_id).execute()
    # Clean up files
    for ext in (".png", ".jpg", ".webp"):
        p = _logos_dir() / f"{business_id}{ext}"
        if p.exists():
            p.unlink()
    return {"ok": True}


@public_router.get("/business/{business_id}/logo/{filename}")
async def serve_logo(business_id: str, filename: str) -> FileResponse:
    """Serve a logo image file."""
    # Sanitise filename
    safe = Path(filename).name
    if not safe.startswith(business_id):
        raise HTTPException(status_code=404)
    fpath = _logos_dir() / safe
    if not fpath.exists():
        raise HTTPException(status_code=404)
    return FileResponse(fpath)


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


@router.post("/business/{business_id}/drafts/{draft_id}/approve")
async def approve_draft(business_id: str, draft_id: str) -> dict:
    """Approve the AI draft and post it to Google as the owner reply."""
    from app.services.google import refresh_access_token, post_review_reply
    from app.core.security import decrypt

    db = get_supabase()
    draft = (
        db.table("review_drafts")
        .select("*")
        .eq("id", draft_id)
        .eq("business_id", business_id)
        .single()
        .execute()
    )
    if not draft.data:
        raise HTTPException(status_code=404, detail="Draft not found")
    d = draft.data

    biz = (
        db.table("businesses").select("*").eq("id", business_id).single().execute()
    )
    if not biz.data or not biz.data.get("google_refresh_token"):
        raise HTTPException(status_code=400, detail="Google not connected")
    b = biz.data

    try:
        refresh_token = decrypt(b["google_refresh_token"])
        access_token = await refresh_access_token(refresh_token)
        review_name = f"accounts/{b['google_account_id']}/locations/{b['google_location_id']}/reviews/{d['google_review_id']}"
        await post_review_reply(access_token, review_name, d["ai_draft_reply"])
    except Exception as e:
        logger.exception("Failed to post approved reply for draft %s", draft_id)
        raise HTTPException(status_code=502, detail=f"Failed to post to Google: {e}")

    from datetime import datetime, timezone
    db.table("review_drafts").update({
        "status": "posted",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", draft_id).execute()

    return {"posted": True, "reply": d["ai_draft_reply"]}


@router.post("/business/{business_id}/drafts/{draft_id}/reply")
async def post_custom_reply(business_id: str, draft_id: str, request: Request) -> dict:
    """Post a custom owner reply (edited by the business owner) to Google."""
    from app.services.google import refresh_access_token, post_review_reply
    from app.core.security import decrypt

    body = await request.json()
    reply_text = body.get("reply_text", "").strip()
    if not reply_text:
        raise HTTPException(status_code=400, detail="reply_text is required")

    db = get_supabase()
    draft = (
        db.table("review_drafts")
        .select("*")
        .eq("id", draft_id)
        .eq("business_id", business_id)
        .single()
        .execute()
    )
    if not draft.data:
        raise HTTPException(status_code=404, detail="Draft not found")
    d = draft.data

    biz = (
        db.table("businesses").select("*").eq("id", business_id).single().execute()
    )
    if not biz.data or not biz.data.get("google_refresh_token"):
        raise HTTPException(status_code=400, detail="Google not connected")
    b = biz.data

    try:
        refresh_token = decrypt(b["google_refresh_token"])
        access_token = await refresh_access_token(refresh_token)
        review_name = f"accounts/{b['google_account_id']}/locations/{b['google_location_id']}/reviews/{d['google_review_id']}"
        await post_review_reply(access_token, review_name, reply_text)
    except Exception as e:
        logger.exception("Failed to post custom reply for draft %s", draft_id)
        raise HTTPException(status_code=502, detail=f"Failed to post to Google: {e}")

    from datetime import datetime, timezone
    db.table("review_drafts").update({
        "status": "posted",
        "ai_draft_reply": reply_text,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", draft_id).execute()

    return {"posted": True, "reply": reply_text}


@router.post("/business/{business_id}/drafts/{draft_id}/reject")
async def reject_draft(business_id: str, draft_id: str) -> dict:
    """Reject a review draft — no reply will be posted."""
    from datetime import datetime, timezone
    db = get_supabase()
    result = (
        db.table("review_drafts")
        .select("id")
        .eq("id", draft_id)
        .eq("business_id", business_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Draft not found")

    db.table("review_drafts").update({
        "status": "rejected",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", draft_id).execute()

    return {"rejected": True}


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

@public_router.get("/business/{business_id}/invoices/{invoice_id}/pdf")
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

    # Content moderation check
    from app.services.moderation import moderate_outbound
    mod_warning = await moderate_outbound(caption)
    if mod_warning:
        raise HTTPException(status_code=400, detail="Message blocked by content moderation")

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

@public_router.get("/business/{business_id}/quotes/{quote_id}/pdf")
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

    # Content moderation check
    from app.services.moderation import moderate_outbound
    mod_warning = await moderate_outbound(caption)
    if mod_warning:
        raise HTTPException(status_code=400, detail="Message blocked by content moderation")

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


# ── Expense endpoints ────────────────────────────────

@router.get("/business/{business_id}/expenses")
async def list_expenses(business_id: str) -> list[dict]:
    """List all expenses for a business, newest first."""
    db = get_supabase()
    result = (
        db.table("expenses")
        .select("*")
        .eq("business_id", business_id)
        .order("date", desc=True)
        .execute()
    )
    expenses = result.data or []
    # Replace heavy receipt_image blob with a boolean flag for the list view
    for e in expenses:
        e["receipt_image"] = bool(e.get("receipt_image"))
    return expenses


@router.get("/business/{business_id}/expenses/summary")
async def expenses_summary(business_id: str) -> dict:
    """Return expense totals and monthly breakdown."""
    db = get_supabase()
    expenses = (
        db.table("expenses")
        .select("*")
        .eq("business_id", business_id)
        .order("date", desc=True)
        .execute()
    ).data or []

    total = sum(e.get("total", 0) for e in expenses)
    total_tax = sum(e.get("tax_amount", 0) for e in expenses)

    # Group by month
    months: dict[str, dict] = {}
    for e in expenses:
        d = e.get("date", "")[:7] or "unknown"
        if d not in months:
            months[d] = {"month": d, "total": 0, "tax": 0, "count": 0}
        months[d]["total"] += e.get("total", 0)
        months[d]["tax"] += e.get("tax_amount", 0)
        months[d]["count"] += 1

    # Group by category
    categories: dict[str, float] = {}
    for e in expenses:
        cat = e.get("category", "general")
        categories[cat] = categories.get(cat, 0) + e.get("total", 0)

    month_list = sorted(months.values(), key=lambda m: m["month"], reverse=True)

    return {
        "total": round(total, 2),
        "total_tax": round(total_tax, 2),
        "count": len(expenses),
        "months": month_list,
        "categories": categories,
    }


@router.get("/business/{business_id}/expenses/{expense_id}")
async def get_expense(business_id: str, expense_id: str) -> dict:
    """Get a single expense."""
    db = get_supabase()
    result = (
        db.table("expenses")
        .select("*")
        .eq("id", expense_id)
        .eq("business_id", business_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Expense not found")
    return result.data


@router.patch("/business/{business_id}/expenses/{expense_id}")
async def update_expense(business_id: str, expense_id: str, request: Request) -> dict:
    """Update an expense (vendor, description, category, total, etc.)."""
    db = get_supabase()
    body = await request.json()
    allowed = {"vendor", "description", "category", "date", "subtotal", "tax_amount", "total", "currency"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    db.table("expenses").update(updates).eq("id", expense_id).eq("business_id", business_id).execute()
    return {"updated": True}


@router.delete("/business/{business_id}/expenses/{expense_id}")
async def delete_expense(business_id: str, expense_id: str) -> dict:
    """Delete an expense."""
    db = get_supabase()
    db.table("expenses").delete().eq("id", expense_id).eq("business_id", business_id).execute()
    return {"deleted": True}


@router.get("/business/{business_id}/expenses/{expense_id}/receipt-image")
async def get_receipt_image(business_id: str, expense_id: str):
    """Return the stored receipt image as a downloadable file."""
    import base64
    db = get_supabase()
    result = (
        db.table("expenses")
        .select("receipt_image,vendor,date")
        .eq("id", expense_id)
        .eq("business_id", business_id)
        .single()
        .execute()
    )
    if not result.data or not result.data.get("receipt_image"):
        raise HTTPException(status_code=404, detail="Receipt image not found")
    data_url = result.data["receipt_image"]
    # Parse data URL: data:<mime>;base64,<data>
    header, b64data = data_url.split(",", 1)
    mime = header.split(":")[1].split(";")[0] if ":" in header else "image/jpeg"
    ext = mime.split("/")[-1].replace("jpeg", "jpg")
    image_bytes = base64.b64decode(b64data)
    vendor = result.data.get("vendor", "receipt").replace(" ", "_")[:30]
    date = result.data.get("date", "")
    filename = f"receipt_{vendor}_{date}.{ext}"
    return Response(
        content=image_bytes,
        media_type=mime,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ── Tax / MTD export endpoints ────────────────────────

import csv
import io
from datetime import datetime


def _quarter_range(year: int, quarter: int) -> tuple[str, str]:
    """Return (start_date, end_date) strings for a UK tax quarter.

    UK tax year runs 6 Apr – 5 Apr.  Standard MTD quarterly periods:
      Q1: 6 Apr – 5 Jul    Q2: 6 Jul – 5 Oct
      Q3: 6 Oct – 5 Jan    Q4: 6 Jan – 5 Apr
    """
    ranges = {
        1: (f"{year}-04-06", f"{year}-07-05"),
        2: (f"{year}-07-06", f"{year}-10-05"),
        3: (f"{year}-10-06", f"{year + 1}-01-05"),
        4: (f"{year + 1}-01-06", f"{year + 1}-04-05"),
    }
    return ranges[quarter]


@router.get("/business/{business_id}/tax/summary")
async def tax_quarter_summary(business_id: str, year: int, quarter: int):
    """Return a JSON summary of income & expenses for a UK tax quarter."""
    if quarter not in (1, 2, 3, 4):
        raise HTTPException(400, "quarter must be 1-4")
    start, end = _quarter_range(year, quarter)
    db = get_supabase()

    invoices = (
        db.table("invoices").select("*")
        .eq("business_id", business_id)
        .gte("created_at", start)
        .lte("created_at", end + "T23:59:59")
        .execute()
    ).data or []

    expenses = (
        db.table("expenses").select("*")
        .eq("business_id", business_id)
        .gte("date", start)
        .lte("date", end)
        .execute()
    ).data or []

    total_income = sum(i.get("total", 0) for i in invoices if i.get("status") == "paid")
    total_income_vat = sum(i.get("tax_amount", 0) for i in invoices if i.get("status") == "paid")
    total_outstanding = sum(i.get("total", 0) for i in invoices if i.get("status") not in ("paid", "cancelled"))
    total_expenses = sum(e.get("total", 0) for e in expenses)
    total_expenses_vat = sum(e.get("tax_amount", 0) for e in expenses)

    return {
        "year": year,
        "quarter": quarter,
        "period": f"{start} to {end}",
        "income": {
            "total_paid": round(total_income, 2),
            "vat_collected": round(total_income_vat, 2),
            "total_outstanding": round(total_outstanding, 2),
            "invoice_count": len(invoices),
        },
        "expenses": {
            "total": round(total_expenses, 2),
            "vat_reclaimable": round(total_expenses_vat, 2),
            "receipt_count": len(expenses),
        },
        "net_profit": round(total_income - total_expenses, 2),
        "vat_position": round(total_income_vat - total_expenses_vat, 2),
    }


@router.get("/business/{business_id}/tax/income-csv")
async def export_income_csv(business_id: str, year: int, quarter: int):
    """Download a CSV of paid invoices for a UK tax quarter."""
    if quarter not in (1, 2, 3, 4):
        raise HTTPException(400, "quarter must be 1-4")
    start, end = _quarter_range(year, quarter)
    db = get_supabase()

    invoices = (
        db.table("invoices").select("*")
        .eq("business_id", business_id)
        .gte("created_at", start)
        .lte("created_at", end + "T23:59:59")
        .order("created_at")
        .execute()
    ).data or []

    # Build customer name map
    cust_ids = list({i["customer_id"] for i in invoices if i.get("customer_id")})
    cust_map: dict[str, str] = {}
    for cid in cust_ids:
        c = db.table("customers").select("name").eq("id", cid).execute()
        if c.data:
            cust_map[cid] = c.data[0]["name"]

    # Fetch line items for each invoice
    inv_descs: dict[str, str] = {}
    for inv in invoices:
        items = (
            db.table("line_items").select("description")
            .eq("parent_id", inv["id"]).eq("parent_type", "invoice").execute()
        ).data or []
        inv_descs[inv["id"]] = "; ".join(it["description"] for it in items if it.get("description"))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Invoice Number", "Customer", "Description", "Net Amount", "VAT", "Total", "Status", "Payment Method", "Currency"])
    for inv in invoices:
        date = (inv.get("paid_at") or inv.get("created_at", ""))[:10]
        writer.writerow([
            date,
            inv.get("invoice_number", ""),
            cust_map.get(inv.get("customer_id", ""), ""),
            inv_descs.get(inv["id"], ""),
            inv.get("subtotal", 0),
            inv.get("tax_amount", 0),
            inv.get("total", 0),
            inv.get("status", ""),
            inv.get("payment_method", ""),
            inv.get("currency", "GBP"),
        ])

    filename = f"income_Q{quarter}_{year}-{year + 1}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/business/{business_id}/tax/expenses-csv")
async def export_expenses_csv(business_id: str, year: int, quarter: int):
    """Download a CSV of expenses for a UK tax quarter."""
    if quarter not in (1, 2, 3, 4):
        raise HTTPException(400, "quarter must be 1-4")
    start, end = _quarter_range(year, quarter)
    db = get_supabase()

    expenses = (
        db.table("expenses").select("*")
        .eq("business_id", business_id)
        .gte("date", start)
        .lte("date", end)
        .order("date")
        .execute()
    ).data or []

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Vendor", "Description", "Category", "Net Amount", "VAT", "Total", "Currency"])
    for exp in expenses:
        desc = exp.get("description", "")
        if not desc:
            try:
                import json
                rd = json.loads(exp.get("receipt_data", "{}"))
                desc = rd.get("description", "")
            except Exception:
                pass
        writer.writerow([
            exp.get("date", ""),
            exp.get("vendor", ""),
            desc,
            exp.get("category", ""),
            exp.get("subtotal", 0),
            exp.get("tax_amount", 0),
            exp.get("total", 0),
            exp.get("currency", "GBP"),
        ])

    filename = f"expenses_Q{quarter}_{year}-{year + 1}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Booking endpoints ────────────────────────────────

@router.get("/business/{business_id}/bookings")
async def list_bookings(business_id: str) -> list[dict]:
    """List all bookings for a business, soonest first."""
    db = get_supabase()
    result = (
        db.table("bookings")
        .select("*")
        .eq("business_id", business_id)
        .order("date")
        .execute()
    )
    return result.data or []


@router.get("/business/{business_id}/bookings/{booking_id}")
async def get_booking(business_id: str, booking_id: str) -> dict:
    """Get a single booking."""
    db = get_supabase()
    result = (
        db.table("bookings")
        .select("*")
        .eq("id", booking_id)
        .eq("business_id", business_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Booking not found")
    return result.data


@router.patch("/business/{business_id}/bookings/{booking_id}")
async def update_booking(business_id: str, booking_id: str, request: Request) -> dict:
    """Update a booking (title, date, time, duration, notes, status)."""
    db = get_supabase()
    body = await request.json()
    allowed = {"title", "date", "time", "duration_mins", "notes", "status",
               "customer_name", "customer_phone"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    db.table("bookings").update(updates).eq("id", booking_id).eq("business_id", business_id).execute()
    return {"updated": True}


@router.delete("/business/{business_id}/bookings/{booking_id}")
async def delete_booking(business_id: str, booking_id: str) -> dict:
    """Delete a booking."""
    db = get_supabase()
    db.table("bookings").delete().eq("id", booking_id).eq("business_id", business_id).execute()
    return {"deleted": True}
