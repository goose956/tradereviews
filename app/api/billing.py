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
    """Create an inactive business from the web signup form → redirect to checkout."""
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
            raise HTTPException(
                status_code=409,
                detail="This phone number already has an active account. Please login instead.",
            )
        # Inactive — update and send to checkout
        db.table("businesses").update({
            "business_name": body.business_name,
            "trade_type": body.trade_type,
        }).eq("id", biz["id"]).execute()
        return {"redirect_url": f"/checkout.html?business_id={biz['id']}"}

    biz_id = str(uuid.uuid4())
    db.table("businesses").insert({
        "id": biz_id,
        "owner_name": body.business_name,
        "business_name": body.business_name,
        "phone_number": phone,
        "trade_type": body.trade_type,
        "subscription_status": "inactive",
    }).execute()

    return {"redirect_url": f"/checkout.html?business_id={biz_id}"}


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

            # Auto-provision a dedicated Twilio number for this business
            try:
                from app.services.twilio_sms import provision_uk_number
                http = request.app.state.http_client
                result = await provision_uk_number(http)
                if result:
                    db.table("businesses").update({
                        "twilio_number": result["phone_number"],
                        "twilio_number_sid": result["sid"],
                    }).eq("id", business_id).execute()
                    logger.info(
                        "Provisioned Twilio number %s for business %s",
                        result["phone_number"], business_id,
                    )
            except Exception:
                logger.exception(
                    "Failed to provision Twilio number for business %s (will use global fallback)",
                    business_id,
                )

    # ── Subscription deleted / cancelled ──
    elif event["type"] in (
        "customer.subscription.deleted",
        "customer.subscription.canceled",
    ):
        sub = event["data"]["object"]
        business_id = sub.get("metadata", {}).get("business_id")
        if business_id:
            # Release the per-business Twilio number if one was provisioned
            biz_row = db.table("businesses").select("twilio_number_sid").eq("id", business_id).execute()
            tw_sid = (biz_row.data[0].get("twilio_number_sid") if biz_row.data else "")
            if tw_sid:
                try:
                    from app.services.twilio_sms import release_number
                    http = request.app.state.http_client
                    await release_number(http, tw_sid)
                except Exception:
                    logger.exception("Failed to release Twilio number for business %s", business_id)

            db.table("businesses").update(
                {"subscription_status": "inactive", "stripe_subscription_id": "",
                 "twilio_number": "", "twilio_number_sid": ""}
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
