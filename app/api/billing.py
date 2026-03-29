"""Signup — creates a business record (no payment required for local testing)."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["billing"])


class SignupRequest(BaseModel):
    business_name: str
    trade_type: str
    phone: str
    email: EmailStr


@router.post("/create-checkout-session")
async def signup(body: SignupRequest) -> dict[str, str]:
    """Create a business record and return a redirect URL (no Stripe)."""
    db = get_supabase()

    # Normalise phone to E.164
    phone = body.phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("0"):
        phone = "+44" + phone[1:]
    if not phone.startswith("+"):
        phone = "+" + phone

    result = (
        db.table("businesses")
        .insert(
            {
                "owner_name": "",
                "business_name": body.business_name,
                "phone_number": phone,
                "trade_type": body.trade_type,
                "subscription_status": "active",
            }
        )
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create business record")

    business_id = result.data[0]["id"]
    return {"checkout_url": f"/success.html?business_id={business_id}"}
