"""Admin API — CRUD for businesses, customers, and review drafts."""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db.supabase import get_supabase
from app.services.whatsapp import send_text_message
from app.services.message_log import log_message

MAX_CAMPAIGN_SENDS_PER_DAY = 50
DELAY_BETWEEN_SENDS_SECS = 2

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ── Pydantic models ──────────────────────────────────────────

class BusinessUpdate(BaseModel):
    owner_name: str | None = None
    business_name: str | None = None
    phone_number: str | None = None
    trade_type: str | None = None
    google_place_id: str | None = None
    google_review_link: str | None = None
    subscription_status: str | None = None


class CustomerUpdate(BaseModel):
    name: str | None = None
    phone_number: str | None = None
    status: str | None = None


class DraftUpdate(BaseModel):
    ai_draft_reply: str | None = None
    status: str | None = None


class AdminCampaignCreate(BaseModel):
    message_body: str
    filter_status: str = "all"
    filter_trade: str = "all"


# ── Businesses ───────────────────────────────────────────────

@router.get("/businesses")
async def list_businesses() -> list[dict[str, Any]]:
    result = get_supabase().table("businesses").select("*").order("created_at", desc=True).execute()
    return result.data or []


@router.get("/businesses/{business_id}")
async def get_business(business_id: str) -> dict[str, Any]:
    result = get_supabase().table("businesses").select("*").eq("id", business_id).execute()
    if not result.data:
        raise HTTPException(404, "Business not found")
    return result.data[0]


@router.patch("/businesses/{business_id}")
async def update_business(business_id: str, body: BusinessUpdate) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(400, "No fields to update")
    result = get_supabase().table("businesses").update(payload).eq("id", business_id).execute()
    if not result.data:
        raise HTTPException(404, "Business not found")
    return result.data[0]


@router.delete("/businesses/{business_id}")
async def delete_business(business_id: str) -> dict[str, str]:
    get_supabase().table("businesses").delete().eq("id", business_id).execute()
    return {"status": "deleted"}


# ── Customers ────────────────────────────────────────────────

@router.get("/businesses/{business_id}/customers")
async def list_customers(business_id: str) -> list[dict[str, Any]]:
    result = (
        get_supabase().table("customers")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@router.get("/customers/{customer_id}")
async def get_customer(customer_id: str) -> dict[str, Any]:
    result = get_supabase().table("customers").select("*").eq("id", customer_id).execute()
    if not result.data:
        raise HTTPException(404, "Customer not found")
    return result.data[0]


@router.patch("/customers/{customer_id}")
async def update_customer(customer_id: str, body: CustomerUpdate) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(400, "No fields to update")
    result = get_supabase().table("customers").update(payload).eq("id", customer_id).execute()
    if not result.data:
        raise HTTPException(404, "Customer not found")
    return result.data[0]


@router.delete("/customers/{customer_id}")
async def delete_customer(customer_id: str) -> dict[str, str]:
    get_supabase().table("customers").delete().eq("id", customer_id).execute()
    return {"status": "deleted"}


# ── Review Drafts ────────────────────────────────────────────

@router.get("/businesses/{business_id}/drafts")
async def list_drafts(business_id: str) -> list[dict[str, Any]]:
    result = (
        get_supabase().table("review_drafts")
        .select("*")
        .eq("business_id", business_id)
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@router.patch("/drafts/{draft_id}")
async def update_draft(draft_id: str, body: DraftUpdate) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(400, "No fields to update")
    result = get_supabase().table("review_drafts").update(payload).eq("id", draft_id).execute()
    if not result.data:
        raise HTTPException(404, "Draft not found")
    return result.data[0]


@router.delete("/drafts/{draft_id}")
async def delete_draft(draft_id: str) -> dict[str, str]:
    get_supabase().table("review_drafts").delete().eq("id", draft_id).execute()
    return {"status": "deleted"}


# ── Admin Campaigns (message YOUR businesses) ───────────────

def _filter_businesses(db, filter_status: str, filter_trade: str) -> list[dict]:
    """Return businesses matching the given status/trade filters."""
    q = db.table("businesses").select("id, business_name, owner_name, phone_number, subscription_status, trade_type")
    if filter_status != "all":
        q = q.eq("subscription_status", filter_status)
    if filter_trade != "all":
        q = q.eq("trade_type", filter_trade)
    return q.execute().data or []


@router.get("/admin-campaigns")
async def list_admin_campaigns() -> list[dict[str, Any]]:
    result = (
        get_supabase().table("admin_campaigns")
        .select("*")
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


@router.get("/admin-campaigns/preview")
async def preview_recipients(
    filter_status: str = "all", filter_trade: str = "all"
) -> dict[str, Any]:
    """Preview how many businesses match the current filters."""
    db = get_supabase()
    recipients = _filter_businesses(db, filter_status, filter_trade)
    return {
        "count": len(recipients),
        "businesses": [
            {"business_name": r["business_name"], "owner_name": r["owner_name"],
             "subscription_status": r["subscription_status"], "trade_type": r.get("trade_type", "")}
            for r in recipients
        ],
    }


@router.post("/admin-campaigns")
async def create_admin_campaign(
    body: AdminCampaignCreate, request: Request
) -> dict[str, Any]:
    db = get_supabase()
    client = request.app.state.http_client
    message = body.message_body.strip()

    if not message:
        raise HTTPException(400, "Message body is required")
    if len(message) > 1000:
        raise HTTPException(400, "Message too long (max 1000 chars)")

    from datetime import datetime, timezone
    today_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    today_camps = (
        db.table("admin_campaigns")
        .select("sent_count")
        .gte("created_at", today_start)
        .execute()
    )
    sent_today = sum(c.get("sent_count", 0) for c in (today_camps.data or []))
    remaining = MAX_CAMPAIGN_SENDS_PER_DAY - sent_today
    if remaining <= 0:
        raise HTTPException(429, f"Daily limit reached ({MAX_CAMPAIGN_SENDS_PER_DAY}/day). Try tomorrow.")

    recipients = _filter_businesses(db, body.filter_status, body.filter_trade)
    if not recipients:
        raise HTTPException(400, "No businesses match these filters.")

    recipients = recipients[:remaining]

    campaign_result = (
        db.table("admin_campaigns")
        .insert({
            "message_body": message,
            "filter_status": body.filter_status,
            "filter_trade": body.filter_trade,
            "total_recipients": len(recipients),
            "status": "sending",
        })
        .execute()
    )
    campaign = campaign_result.data[0]
    campaign_id = campaign["id"]

    sent = 0
    failed = 0
    for i, biz in enumerate(recipients):
        try:
            phone = biz["phone_number"].lstrip("+")
            await send_text_message(client, phone, message)
            log_message(
                business_id=biz["id"],
                to_phone=biz["phone_number"],
                message_body=message,
                message_type="admin_campaign",
            )
            sent += 1
        except Exception:
            logger.exception("Admin campaign send failed for %s", biz.get("business_name"))
            failed += 1
        if i < len(recipients) - 1:
            await asyncio.sleep(DELAY_BETWEEN_SENDS_SECS)

    final_status = "completed" if failed == 0 else ("failed" if sent == 0 else "partial")
    db.table("admin_campaigns").update({
        "sent_count": sent,
        "failed_count": failed,
        "status": final_status,
    }).eq("id", campaign_id).execute()

    return {"campaign_id": campaign_id, "sent": sent, "failed": failed, "status": final_status}


@router.delete("/admin-campaigns/{campaign_id}")
async def delete_admin_campaign(campaign_id: str) -> dict[str, str]:
    get_supabase().table("admin_campaigns").delete().eq("id", campaign_id).execute()
    return {"status": "deleted"}


# ── Dashboard stats ──────────────────────────────────────────

@router.get("/stats")
async def dashboard_stats() -> dict[str, Any]:
    sb = get_supabase()
    biz = sb.table("businesses").select("id", count="exact").execute()
    cust = sb.table("customers").select("id", count="exact").execute()
    drafts = sb.table("review_drafts").select("id", count="exact").eq("status", "pending_approval").execute()
    admin_camps = sb.table("admin_campaigns").select("id", count="exact").execute()
    trial = sb.table("businesses").select("id", count="exact").eq("subscription_status", "trial").execute()
    inactive = sb.table("businesses").select("id", count="exact").eq("subscription_status", "inactive").execute()
    return {
        "total_businesses": biz.count or 0,
        "total_customers": cust.count or 0,
        "pending_drafts": drafts.count or 0,
        "trial_businesses": trial.count or 0,
        "inactive_businesses": inactive.count or 0,
        "admin_campaigns": admin_camps.count or 0,
    }
