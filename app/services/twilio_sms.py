"""Twilio number provisioning — buy/release per-business UK numbers."""

import logging
from base64 import b64encode
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_BASE = "https://api.twilio.com/2010-04-01/Accounts"


def _auth_header() -> dict[str, str]:
    settings = get_settings()
    creds = b64encode(
        f"{settings.twilio_account_sid}:{settings.twilio_auth_token}".encode()
    ).decode()
    return {"Authorization": f"Basic {creds}"}


async def provision_uk_number(client: httpx.AsyncClient) -> dict[str, str] | None:
    """Buy a UK mobile number and return {'phone_number': '+44...', 'sid': 'PN...'}.

    Returns None on failure so callers can fall back to the global number.
    """
    settings = get_settings()
    sid = settings.twilio_account_sid
    if not sid:
        logger.error("TWILIO_ACCOUNT_SID not configured — skipping provisioning")
        return None

    # 1) Search for available UK mobile numbers with SMS capability
    search_url = f"{_BASE}/{sid}/AvailablePhoneNumbers/GB/Mobile.json"
    params: dict[str, str] = {"SmsEnabled": "true", "PageSize": "1"}
    resp = await client.get(search_url, headers=_auth_header(), params=params)
    if resp.status_code != 200:
        logger.error(
            "Twilio available-numbers search failed: %s %s",
            resp.status_code, resp.text,
        )
        return None

    numbers = resp.json().get("available_phone_numbers", [])
    if not numbers:
        logger.error("No available UK mobile numbers on Twilio")
        return None

    chosen = numbers[0]["phone_number"]

    # 2) Purchase the number and set the inbound-SMS webhook
    buy_url = f"{_BASE}/{sid}/IncomingPhoneNumbers.json"
    webhook_url = f"{settings.base_url}/webhook/twilio-inbound"
    buy_data = {
        "PhoneNumber": chosen,
        "SmsUrl": webhook_url,
        "SmsMethod": "POST",
    }
    resp = await client.post(buy_url, headers=_auth_header(), data=buy_data)
    if resp.status_code not in (200, 201):
        logger.error(
            "Twilio number purchase failed: %s %s",
            resp.status_code, resp.text,
        )
        return None

    result: dict[str, Any] = resp.json()
    logger.info("Purchased Twilio number %s (SID %s)", result["phone_number"], result["sid"])
    return {"phone_number": result["phone_number"], "sid": result["sid"]}


async def release_number(client: httpx.AsyncClient, number_sid: str) -> bool:
    """Release a previously purchased Twilio number."""
    settings = get_settings()
    sid = settings.twilio_account_sid
    url = f"{_BASE}/{sid}/IncomingPhoneNumbers/{number_sid}.json"
    resp = await client.delete(url, headers=_auth_header())
    if resp.status_code == 204:
        logger.info("Released Twilio number SID %s", number_sid)
        return True
    logger.error("Failed to release Twilio number %s: %s %s", number_sid, resp.status_code, resp.text)
    return False
