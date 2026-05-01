"""WhatsApp Review Engine — FastAPI application entry-point."""

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
import hashlib
import hmac
import logging
from base64 import b64encode
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.billing import router as billing_router
from app.api.cron import router as cron_router
from app.api.member import router as member_router, public_router as member_public_router
from app.api.oauth import router as oauth_router
from app.api.webhooks import router as webhook_router
from app.api.telegram_webhook import router as telegram_router

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application-wide resources (httpx client)."""
    app.state.http_client = httpx.AsyncClient(timeout=30.0)

    # Register Telegram webhook only when explicitly configured.
    from app.core.config import get_settings as _gs
    from app.services.telegram import set_webhook as _tg_set_webhook
    _settings = _gs()
    webhook_url = _settings.telegram_webhook_url.strip()
    if _settings.telegram_bot_token and webhook_url:
        try:
            result = await _tg_set_webhook(
                app.state.http_client,
                webhook_url,
                secret_token=_settings.telegram_webhook_secret,
            )
            logger.info("Telegram webhook registered at %s: %s", webhook_url, result)
        except Exception:
            logger.exception("Failed to register Telegram webhook")
    elif _settings.telegram_bot_token:
        logger.info("Telegram webhook auto-registration skipped (set TELEGRAM_WEBHOOK_URL to enable)")

    yield
    await app.state.http_client.aclose()


app = FastAPI(
    title="WhatsApp Review Engine",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — restrict to your own domain in production
from app.core.config import get_settings as _get_settings
_base = _get_settings().base_url.rstrip("/")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_base, "http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
app.include_router(telegram_router)
app.include_router(oauth_router)
app.include_router(cron_router)
app.include_router(billing_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(member_router)
app.include_router(member_public_router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


# ── Inbound Twilio SMS webhook ───────────────────────────────────

def _validate_twilio_signature(
    url: str, params: dict[str, str], signature: str, auth_token: str,
) -> bool:
    """Verify X-Twilio-Signature to prevent spoofed requests."""
    s = url
    for key in sorted(params.keys()):
        s += key + params[key]
    expected = b64encode(
        hmac.new(auth_token.encode(), s.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


@app.post("/webhook/twilio-inbound")
async def twilio_inbound_sms(request: Request) -> Response:
    """Receive inbound SMS on a per-business Twilio number and forward to the owner."""
    from app.core.config import get_settings
    from app.db.supabase import get_supabase
    from app.services.message_log import log_message
    from app.services.whatsapp import send_text_message

    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    # Validate Twilio signature
    settings = get_settings()
    sig = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    if settings.twilio_auth_token and sig:
        if not _validate_twilio_signature(url, params, sig, settings.twilio_auth_token):
            return Response(status_code=403)

    from_number = params.get("From", "")
    to_number = params.get("To", "")
    body = params.get("Body", "").strip()

    if not body:
        return Response(
            content="<Response></Response>", media_type="application/xml",
        )

    # Look up which business owns this Twilio number
    db = get_supabase()
    biz = (
        db.table("businesses")
        .select("id, phone_number, business_name")
        .eq("twilio_number", to_number)
        .execute()
    )
    if not biz.data:
        logger.warning("Inbound SMS to unknown Twilio number %s", to_number)
        return Response(
            content="<Response></Response>", media_type="application/xml",
        )

    business = biz.data[0]

    # Forward the customer's reply to the business owner via WhatsApp
    owner_phone = business["phone_number"].lstrip("+")
    client = request.app.state.http_client
    await send_text_message(
        client,
        owner_phone,
        f"\U0001f4e9 SMS reply from {from_number}:\n\n{body}",
    )

    log_message(
        business_id=business["id"],
        to_phone=from_number,
        message_body=body,
        message_type="sms",
        direction="inbound",
    )

    return Response(
        content="<Response></Response>", media_type="application/xml",
    )


# Serve frontend static files last (catch-all mount)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
