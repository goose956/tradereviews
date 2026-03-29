"""WhatsApp OTP authentication for tradespeople."""

import secrets
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Request, Depends, Header
from pydantic import BaseModel

from app.db.supabase import get_supabase
from app.services.whatsapp import send_text_message

router = APIRouter(prefix="/auth", tags=["auth"])

OTP_EXPIRY_MINUTES = 5
SESSION_EXPIRY_DAYS = 30


# ── Models ────────────────────────────────────────

class RequestCode(BaseModel):
    phone: str

class VerifyCode(BaseModel):
    phone: str
    code: str


# ── Helpers ───────────────────────────────────────

def _normalise_phone(raw: str) -> str:
    """Strip spaces/dashes, convert UK 07… to 447…"""
    phone = raw.strip().replace(" ", "").replace("-", "")
    if phone.startswith("07") and len(phone) == 11:
        phone = "44" + phone[1:]
    if not phone.startswith("+"):
        phone = "+" + phone if not phone.startswith("+") else phone
    # Remove leading + for consistent storage
    return phone.lstrip("+")


def _generate_otp() -> str:
    """Generate a 6-digit numeric code."""
    return f"{secrets.randbelow(900000) + 100000}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_expired(expires_at: str) -> bool:
    exp = datetime.fromisoformat(expires_at)
    return datetime.now(timezone.utc) > exp


# ── Auth dependency ───────────────────────────────

async def get_current_business(authorization: str = Header(default="")) -> dict:
    """Validate the session token and return the business record."""
    token = authorization.replace("Bearer ", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    db = get_supabase()
    session = (
        db.table("auth_sessions")
        .select("*")
        .eq("token", token)
        .single()
        .execute()
    )
    if not session.data:
        raise HTTPException(status_code=401, detail="Invalid session")

    if _is_expired(session.data["expires_at"]):
        db.table("auth_sessions").delete().eq("id", session.data["id"]).execute()
        raise HTTPException(status_code=401, detail="Session expired")

    biz = (
        db.table("businesses")
        .select("*")
        .eq("id", session.data["business_id"])
        .single()
        .execute()
    )
    if not biz.data:
        raise HTTPException(status_code=401, detail="Business not found")

    return biz.data


# ── Endpoints ─────────────────────────────────────

@router.post("/request-code")
async def request_code(body: RequestCode, request: Request) -> dict:
    """Send a 6-digit OTP to the tradesperson's WhatsApp number."""
    phone = _normalise_phone(body.phone)
    if len(phone) < 10:
        raise HTTPException(status_code=400, detail="Invalid phone number")

    db = get_supabase()

    # Check that this phone belongs to a registered business
    # Business phones are stored with '+' prefix (e.g. +447870160777)
    phone_e164 = f"+{phone}"
    biz = db.table("businesses").select("id, business_name").eq("phone_number", phone_e164).single().execute()
    if not biz.data:
        # Don't reveal if account exists — just say code sent
        return {"sent": True, "message": "If this number is registered, you'll receive a code on WhatsApp."}

    # Rate limit: max 3 codes in the last 5 minutes for this phone
    five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    recent = db.table("auth_codes").select("id", count="exact").eq("phone", phone).gte("created_at", five_min_ago).execute()
    if recent.count and recent.count >= 3:
        raise HTTPException(status_code=429, detail="Too many attempts. Please wait a few minutes.")

    code = _generate_otp()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)).isoformat()

    db.table("auth_codes").insert({
        "phone": phone,
        "code": code,
        "expires_at": expires,
    }).execute()

    # Send OTP via WhatsApp
    client: httpx.AsyncClient = request.app.state.http_client
    otp_message = f"🔐 Your ReviewEngine login code is: *{code}*\n\nThis code expires in {OTP_EXPIRY_MINUTES} minutes. Do not share it with anyone."
    try:
        await send_text_message(client, phone, otp_message)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to send code. Please try again.")

    return {"sent": True, "message": "If this number is registered, you'll receive a code on WhatsApp."}


@router.post("/verify-code")
async def verify_code(body: VerifyCode) -> dict:
    """Verify the OTP and return a session token + business_id."""
    phone = _normalise_phone(body.phone)
    code = body.code.strip()

    if not code or len(code) != 6:
        raise HTTPException(status_code=400, detail="Invalid code format")

    db = get_supabase()

    # Find the latest unused code for this phone
    codes = (
        db.table("auth_codes")
        .select("*")
        .eq("phone", phone)
        .eq("code", code)
        .eq("used", 0)
        .order("created_at", desc=True)
        .execute()
    )

    if not codes.data:
        raise HTTPException(status_code=401, detail="Invalid or expired code")

    auth_code = codes.data[0]

    if _is_expired(auth_code["expires_at"]):
        db.table("auth_codes").update({"used": 1}).eq("id", auth_code["id"]).execute()
        raise HTTPException(status_code=401, detail="Code has expired. Please request a new one.")

    # Mark code as used
    db.table("auth_codes").update({"used": 1}).eq("id", auth_code["id"]).execute()

    # Find the business (phones stored with '+' prefix)
    phone_e164 = f"+{phone}"
    biz = db.table("businesses").select("id, business_name").eq("phone_number", phone_e164).single().execute()
    if not biz.data:
        raise HTTPException(status_code=401, detail="No account found for this number")

    # Create session token
    token = secrets.token_urlsafe(48)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_EXPIRY_DAYS)).isoformat()

    db.table("auth_sessions").insert({
        "business_id": biz.data["id"],
        "token": token,
        "expires_at": expires,
    }).execute()

    return {
        "token": token,
        "business_id": biz.data["id"],
        "business_name": biz.data["business_name"],
        "expires_at": expires,
    }


@router.post("/logout")
async def logout(authorization: str = Header(default="")) -> dict:
    """Invalidate the current session."""
    token = authorization.replace("Bearer ", "").strip()
    if token:
        db = get_supabase()
        db.table("auth_sessions").delete().eq("token", token).execute()
    return {"logged_out": True}


@router.get("/me")
async def get_me(biz: dict = Depends(get_current_business)) -> dict:
    """Return the current authenticated business."""
    return {"business_id": biz["id"], "business_name": biz["business_name"]}
