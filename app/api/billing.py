"""Stripe billing — checkout sessions, webhooks, cancellation & info."""

import logging
import stripe

from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.config import get_settings
from app.db.supabase import get_supabase
from app.api.auth import get_current_business

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["billing"])


def _stripe():
    """Initialise and return the stripe module with the secret key."""
    settings = get_settings()
    stripe.api_key = settings.stripe_secret_key
    return stripe


# ── Models ────────────────────────────────────────────


class CheckoutRequest(BaseModel):
    business_id: str


class WebSignupRequest(BaseModel):
    business_name: str
    trade_type: str
    phone: str
    telegram_chat_id: str = ""  # Passed from Telegram signup link for auto-linking


def _create_session(db, business_id: str) -> dict:
    """Create a 30-day auth session token for the given business."""
    token = secrets.token_urlsafe(48)
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    db.table("auth_sessions").insert({
        "business_id": business_id,
        "token": token,
        "expires_at": expires,
    }).execute()
    return {"token": token, "expires_at": expires}


# ── Public: checkout info (no auth — linked from WhatsApp) ────────────


@router.get("/checkout-info")
async def checkout_info(business_id: str) -> dict:
    """Return public business name for the checkout page."""
    db = get_supabase()
    result = db.table("businesses").select("business_name").eq("id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Business not found")
    return {"business_name": result.data[0]["business_name"]}


# ── Web Signup (from login page — no auth) ────────────────────


@router.post("/web-signup")
async def web_signup(body: WebSignupRequest) -> dict:
    """Create a business from the web signup form.

    TEMP: paywall bypassed for testing — sets status to 'active' directly.
    TODO: restore checkout redirect before going live.
    """
    import uuid
    db = get_supabase()

    # Normalise phone
    phone = body.phone.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone

    # Check if phone already registered
    existing = db.table("businesses").select("id, subscription_status").eq(
        "phone_number", phone
    ).execute()

    if existing.data:
        biz = existing.data[0]
        if biz["subscription_status"] == "active":
            # Existing active account: for Telegram onboarding, auto-link and log in.
            if body.telegram_chat_id:
                db.table("businesses").update({
                    "telegram_chat_id": body.telegram_chat_id,
                }).eq("id", biz["id"]).execute()
                session = _create_session(db, biz["id"])
                biz_name = db.table("businesses").select("business_name").eq("id", biz["id"]).execute()
                return {
                    "redirect_url": "/portal.html",
                    "token": session["token"],
                    "business_id": biz["id"],
                    "business_name": biz_name.data[0]["business_name"] if biz_name.data else body.business_name,
                    "existing_account": True,
                }
            raise HTTPException(
                status_code=409,
                detail="This phone number already has an active account. Please login instead.",
            )
        # Inactive — activate directly (TEMP: bypassing checkout)
        update_data: dict = {
            "business_name": body.business_name,
            "trade_type": body.trade_type,
            "subscription_status": "active",
        }
        if body.telegram_chat_id:
            update_data["telegram_chat_id"] = body.telegram_chat_id
        db.table("businesses").update(update_data).eq("id", biz["id"]).execute()
        session = _create_session(db, biz["id"])
        biz_name = db.table("businesses").select("business_name").eq("id", biz["id"]).execute()
        return {
            "redirect_url": "/portal.html",
            "token": session["token"],
            "business_id": biz["id"],
            "business_name": biz_name.data[0]["business_name"] if biz_name.data else body.business_name,
        }

    biz_id = str(uuid.uuid4())
    insert_data: dict = {
        "id": biz_id,
        "owner_name": body.business_name,
        "business_name": body.business_name,
        "phone_number": phone,
        "trade_type": body.trade_type,
        "subscription_status": "active",  # TEMP: bypassing paywall for testing
    }
    if body.telegram_chat_id:
        insert_data["telegram_chat_id"] = body.telegram_chat_id
    db.table("businesses").insert(insert_data).execute()
    session = _create_session(db, biz_id)

    return {
        "redirect_url": "/portal.html",
        "token": session["token"],
        "business_id": biz_id,
        "business_name": body.business_name,
    }


# ── Create Stripe Checkout Session ────────────────────


@router.post("/create-checkout-session")
async def create_checkout_session(body: CheckoutRequest) -> dict[str, str]:
    """Create a Stripe Checkout session for the given business."""
    settings = get_settings()
    s = _stripe()
    db = get_supabase()

    # Look up the business
    biz = db.table("businesses").select("*").eq("id", body.business_id).execute()
    if not biz.data:
        raise HTTPException(status_code=404, detail="Business not found")
    business = biz.data[0]

    if not settings.stripe_secret_key or not settings.stripe_price_id:
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured yet. Please try again later.",
        )

    # Reuse existing Stripe customer or create one
    customer_id = business.get("stripe_customer_id")
    if not customer_id:
        customer = s.Customer.create(
            name=business["business_name"],
            phone=business["phone_number"],
            metadata={"business_id": business["id"]},
        )
        customer_id = customer.id
        db.table("businesses").update(
            {"stripe_customer_id": customer_id}
        ).eq("id", business["id"]).execute()

    # Create checkout session
    session = s.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
        success_url=f"{settings.base_url}/success.html?business_id={business['id']}&session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{settings.base_url}/checkout.html?business_id={business['id']}",
        metadata={"business_id": business["id"]},
        subscription_data={"metadata": {"business_id": business["id"]}},
    )

    return {"checkout_url": session.url}


# ── Stripe Webhook ────────────────────────────────────


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request) -> Response:
    """Handle Stripe webhook events (checkout completed, sub cancelled, etc.)."""
    settings = get_settings()
    s = _stripe()
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = s.Webhook.construct_event(
            payload, sig, settings.stripe_webhook_secret
        )
    except (ValueError, s.error.SignatureVerificationError):
        logger.warning("Stripe webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid signature")

    db = get_supabase()

    # ── Checkout completed — activate subscription ──
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        business_id = session.get("metadata", {}).get("business_id")
        subscription_id = session.get("subscription")
        if business_id:
            updates = {"subscription_status": "active"}
            if subscription_id:
                updates["stripe_subscription_id"] = subscription_id
            db.table("businesses").update(updates).eq("id", business_id).execute()
            logger.info("Activated subscription for business %s", business_id)

    # ── Subscription deleted / cancelled ──
    elif event["type"] in (
        "customer.subscription.deleted",
        "customer.subscription.canceled",
    ):
        sub = event["data"]["object"]
        business_id = sub.get("metadata", {}).get("business_id")
        if business_id:
            db.table("businesses").update(
                {"subscription_status": "inactive", "stripe_subscription_id": ""}
            ).eq("id", business_id).execute()
            logger.info("Deactivated subscription for business %s", business_id)

    # ── Payment failed ──
    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer")
        if customer_id:
            biz = db.table("businesses").select("id").eq(
                "stripe_customer_id", customer_id
            ).execute()
            if biz.data:
                db.table("businesses").update(
                    {"subscription_status": "past_due"}
                ).eq("id", biz.data[0]["id"]).execute()
                logger.info("Marked business %s as past_due", biz.data[0]["id"])

    return Response(status_code=200)


# ── Cancel Subscription (from portal — auth required) ──


@router.post("/cancel-subscription")
async def cancel_subscription(business: dict = Depends(get_current_business)) -> dict:
    """Cancel the business's Stripe subscription at period end."""
    s = _stripe()
    sub_id = business.get("stripe_subscription_id")
    if not sub_id:
        raise HTTPException(status_code=400, detail="No active subscription found")

    try:
        s.Subscription.modify(sub_id, cancel_at_period_end=True)
    except Exception as e:
        logger.error("Failed to cancel subscription %s: %s", sub_id, e)
        raise HTTPException(status_code=500, detail="Failed to cancel subscription")

    db = get_supabase()
    db.table("businesses").update(
        {"subscription_status": "inactive"}
    ).eq("id", business["id"]).execute()

    return {"cancelled": True, "message": "Your subscription will end at the end of the current billing period."}


# ── Subscription Status (from portal — auth required) ──


@router.get("/subscription-status")
async def subscription_status(business: dict = Depends(get_current_business)) -> dict:
    """Return the current subscription status and details."""
    s = _stripe()
    sub_id = business.get("stripe_subscription_id")
    status = business.get("subscription_status", "inactive")

    result = {
        "subscription_status": status,
        "stripe_subscription_id": sub_id or "",
        "current_period_end": None,
        "cancel_at_period_end": False,
    }

    if sub_id:
        try:
            sub = s.Subscription.retrieve(sub_id)
            result["current_period_end"] = sub.current_period_end
            result["cancel_at_period_end"] = sub.cancel_at_period_end
        except Exception:
            pass

    return result
