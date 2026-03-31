"""Send SMS via Twilio API."""

import logging
from base64 import b64encode
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _twilio_url() -> str:
    settings = get_settings()
    return f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"


def _twilio_auth() -> tuple[str, str]:
    settings = get_settings()
    return (settings.twilio_account_sid, settings.twilio_auth_token)


async def send_sms(
    client: httpx.AsyncClient,
    to_phone: str,
    body: str,
) -> dict[str, Any]:
    """Send an SMS via Twilio. to_phone should be E.164 format."""
    settings = get_settings()

    if not settings.twilio_account_sid:
        logger.error("TWILIO_ACCOUNT_SID not configured")
        raise RuntimeError("Twilio credentials not configured")

    # Ensure + prefix
    if not to_phone.startswith("+"):
        to_phone = f"+{to_phone}"

    payload = {
        "To": to_phone,
        "From": settings.twilio_phone_number,
        "Body": body,
    }

    sid = settings.twilio_account_sid
    token = settings.twilio_auth_token
    auth_str = b64encode(f"{sid}:{token}".encode()).decode()

    headers = {
        "Authorization": f"Basic {auth_str}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    response = await client.post(_twilio_url(), headers=headers, data=payload)
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    logger.info("SMS sent to %s — SID: %s", to_phone, data.get("sid"))
    return data


def build_invoice_sms(
    *,
    first_name: str,
    biz_name: str,
    invoice_number: str,
    description: str,
    total: float,
    sym: str,
    pdf_url: str,
    personal_phone: str,
) -> str:
    """Build a plain-text SMS for an invoice."""
    return (
        f"Hi {first_name}, invoice {invoice_number} from {biz_name}:\n"
        f"{description} - {sym}{total:.2f} (inc. VAT)\n"
        f"PDF: {pdf_url}\n"
        f"Contact: {personal_phone}"
    )


def build_quote_sms(
    *,
    first_name: str,
    biz_name: str,
    quote_number: str,
    description: str,
    total: float,
    sym: str,
    valid_until: str,
    pdf_url: str,
    personal_phone: str,
) -> str:
    """Build a plain-text SMS for a quote."""
    return (
        f"Hi {first_name}, quote {quote_number} from {biz_name}:\n"
        f"{description} - {sym}{total:.2f} (inc. VAT)\n"
        f"Valid until {valid_until}\n"
        f"PDF: {pdf_url}\n"
        f"Contact: {personal_phone}"
    )


def build_review_sms(
    *,
    first_name: str,
    biz_name: str,
    review_link: str,
    job_description: str = "",
) -> str:
    """Build a plain-text SMS for a review request."""
    job_bit = f" for the {job_description}" if job_description else ""
    return (
        f"Hi {first_name}, thanks for choosing {biz_name}{job_bit}! "
        f"We'd love your feedback - it only takes 30 secs: {review_link}"
    )
