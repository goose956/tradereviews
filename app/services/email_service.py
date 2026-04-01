"""Send emails via Resend API."""

import logging
from html import escape
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


async def send_email(
    client: httpx.AsyncClient,
    to_email: str,
    subject: str,
    html_body: str,
    plain_body: str = "",
) -> dict[str, Any]:
    """Send an email via Resend. Returns the response data."""
    settings = get_settings()

    if not settings.resend_api_key:
        logger.error("RESEND_API_KEY not configured")
        raise RuntimeError("Resend API key not configured")

    from_email = settings.resend_from_email or "ReviewEngine <noreply@jobping.co.uk>"

    payload: dict[str, Any] = {
        "from": from_email,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }
    if plain_body:
        payload["text"] = plain_body

    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }

    response = await client.post(RESEND_API_URL, headers=headers, json=payload)
    response.raise_for_status()
    data = response.json()
    logger.info("Email sent to %s — id %s", to_email, data.get("id"))
    return data


def build_invoice_email(
    *,
    customer_name: str,
    first_name: str,
    biz_name: str,
    invoice_number: str,
    description: str,
    subtotal: float,
    tax_rate: float,
    tax_amount: float,
    total: float,
    sym: str,
    pdf_url: str,
    personal_phone: str,
) -> tuple[str, str, str]:
    """Return (subject, html_body, plain_body) for an invoice email."""
    subject = f"Invoice {invoice_number} from {biz_name}"

    plain_body = (
        f"Hi {first_name},\n\n"
        f"Here is your invoice from {biz_name}:\n\n"
        f"Invoice {invoice_number}\n"
        f"- {description}\n"
        f"- Subtotal: {sym}{subtotal:.2f}\n"
        f"- VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
        f"- Total: {sym}{total:.2f}\n\n"
        f"Download PDF: {pdf_url}\n\n"
        f"To discuss this job, contact {biz_name} on {personal_phone}\n"
    )

    # Escape user-provided values for safe HTML rendering
    e_first = escape(first_name)
    e_biz = escape(biz_name)
    e_inv = escape(invoice_number)
    e_desc = escape(description)
    e_phone = escape(personal_phone)
    e_pdf = escape(pdf_url)

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #333;">Invoice {e_inv}</h2>
        <p>Hi {e_first},</p>
        <p>Here is your invoice from <strong>{e_biz}</strong>:</p>
        <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;">Description</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{e_desc}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;">Subtotal</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{sym}{subtotal:.2f}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;">VAT ({tax_rate:.0f}%)</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{sym}{tax_amount:.2f}</td></tr>
            <tr><td style="padding: 8px; font-weight: bold;">Total</td>
                <td style="padding: 8px; font-weight: bold;">{sym}{total:.2f}</td></tr>
        </table>
        <p><a href="{e_pdf}" style="display: inline-block; padding: 10px 20px; background: #2563eb; color: #fff; text-decoration: none; border-radius: 4px;">Download PDF</a></p>
        <hr style="margin: 24px 0; border: none; border-top: 1px solid #eee;">
        <p style="color: #666; font-size: 13px;">This is an automated message. To discuss this job, contact {e_biz} on <strong>{e_phone}</strong></p>
    </div>
    """

    return subject, html_body, plain_body


def build_quote_email(
    *,
    customer_name: str,
    first_name: str,
    biz_name: str,
    quote_number: str,
    description: str,
    subtotal: float,
    tax_rate: float,
    tax_amount: float,
    total: float,
    sym: str,
    valid_until: str,
    pdf_url: str,
    personal_phone: str,
) -> tuple[str, str, str]:
    """Return (subject, html_body, plain_body) for a quote email."""
    subject = f"Quote {quote_number} from {biz_name}"

    plain_body = (
        f"Hi {first_name},\n\n"
        f"Here is a quote from {biz_name}:\n\n"
        f"Quote {quote_number}\n"
        f"- {description}\n"
        f"- Subtotal: {sym}{subtotal:.2f}\n"
        f"- VAT ({tax_rate:.0f}%): {sym}{tax_amount:.2f}\n"
        f"- Total: {sym}{total:.2f}\n"
        f"- Valid until: {valid_until}\n\n"
        f"Download PDF: {pdf_url}\n\n"
        f"To discuss this job, contact {biz_name} on {personal_phone}\n"
    )

    e_first = escape(first_name)
    e_biz = escape(biz_name)
    e_quote = escape(quote_number)
    e_desc = escape(description)
    e_phone = escape(personal_phone)
    e_pdf = escape(pdf_url)
    e_valid = escape(valid_until)

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #333;">Quote {e_quote}</h2>
        <p>Hi {e_first},</p>
        <p>Here is a quote from <strong>{e_biz}</strong>:</p>
        <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;">Description</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{e_desc}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;">Subtotal</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{sym}{subtotal:.2f}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #eee;">VAT ({tax_rate:.0f}%)</td>
                <td style="padding: 8px; border-bottom: 1px solid #eee;">{sym}{tax_amount:.2f}</td></tr>
            <tr><td style="padding: 8px; font-weight: bold;">Total</td>
                <td style="padding: 8px; font-weight: bold;">{sym}{total:.2f}</td></tr>
            <tr><td style="padding: 8px;">Valid until</td>
                <td style="padding: 8px;">{e_valid}</td></tr>
        </table>
        <p><a href="{e_pdf}" style="display: inline-block; padding: 10px 20px; background: #2563eb; color: #fff; text-decoration: none; border-radius: 4px;">Download PDF</a></p>
        <hr style="margin: 24px 0; border: none; border-top: 1px solid #eee;">
        <p style="color: #666; font-size: 13px;">This is an automated message. To discuss this job, contact {e_biz} on <strong>{e_phone}</strong></p>
    </div>
    """

    return subject, html_body, plain_body


def build_review_email(
    *,
    customer_name: str,
    first_name: str,
    biz_name: str,
    review_link: str,
    job_description: str = "",
) -> tuple[str, str, str]:
    """Return (subject, html_body, plain_body) for a review request email."""
    subject = f"{biz_name} would love your feedback!"

    job_line = (
        f" for the {job_description}" if job_description else ""
    )
    job_thanks = (
        f"We hope you're happy with the {job_description} we completed for you. "
        if job_description
        else ""
    )

    plain_body = (
        f"Hi {first_name},\n\n"
        f"Thank you for choosing {biz_name}{job_line}! {job_thanks}"
        f"We'd really appreciate it "
        f"if you could take a moment to leave us a review.\n\n"
        f"Leave a review: {review_link}\n\n"
        f"It only takes 30 seconds and helps us a lot!\n\n"
        f"Thank you,\n{biz_name}\n"
    )

    e_first = escape(first_name)
    e_biz = escape(biz_name)
    e_link = escape(review_link)
    e_job_line = escape(job_line)
    e_job_thanks = escape(job_thanks)

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
        <h2 style="color: #333;">How was your experience?</h2>
        <p>Hi {e_first},</p>
        <p>Thank you for choosing <strong>{e_biz}</strong>{e_job_line}! {e_job_thanks}We'd really appreciate it if you could take a moment to leave us a review.</p>
        <p style="margin: 24px 0;">
            <a href="{e_link}" style="display: inline-block; padding: 12px 24px; background: #f59e0b; color: #fff; text-decoration: none; border-radius: 4px; font-weight: bold;">Leave a Review ⭐</a>
        </p>
        <p>It only takes 30 seconds and helps us a lot!</p>
        <p>Thank you,<br><strong>{e_biz}</strong></p>
    </div>
    """

    return subject, html_body, plain_body
