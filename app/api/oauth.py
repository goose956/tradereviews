"""Google OAuth 2.0 flow for onboarding businesses."""

import logging
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.config import get_settings
from app.core.security import encrypt
from app.db.supabase import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/google", tags=["oauth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GBP_ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"


# ──────────────────────────────────────────────
# GET  /auth/google/login  — redirect to consent
# ──────────────────────────────────────────────
@router.get("/login")
async def google_login(business_id: str = Query(..., description="UUID of the business")) -> RedirectResponse:
    """Build the Google OAuth authorization URL and redirect."""
    settings = get_settings()

    # Validate that the business exists before redirecting.
    supabase = get_supabase()
    result = supabase.table("businesses").select("id").eq("id", business_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Business not found")

    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/business.manage",
        "access_type": "offline",
        "prompt": "consent",
        "state": business_id,
    }
    url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url=url)


# ──────────────────────────────────────────────
# GET  /auth/google/callback  — exchange code
# ──────────────────────────────────────────────
@router.get("/callback", response_class=HTMLResponse)
async def google_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
    error: str | None = Query(None),
) -> HTMLResponse:
    """Exchange the authorisation code for tokens and persist them."""
    if error:
        logger.warning("OAuth error from Google: %s", error)
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")

    business_id = state
    settings = get_settings()
    http_client: httpx.AsyncClient = request.app.state.http_client

    # 1. Exchange code for tokens ──────────────────────────────
    token_data = await _exchange_code(http_client, code, settings)

    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")

    if not access_token:
        raise HTTPException(status_code=502, detail="No access_token received from Google")
    if not refresh_token:
        raise HTTPException(status_code=502, detail="No refresh_token received — try re-authorising with prompt=consent")

    # 2. Fetch GBP account & first location ────────────────────
    account_id, location_id = await _fetch_first_location(http_client, access_token)

    # 3. Encrypt the refresh token ─────────────────────────────
    encrypted_refresh = encrypt(refresh_token)

    # 4. Persist to businesses table ───────────────────────────
    supabase = get_supabase()
    supabase.table("businesses").update(
        {
            "google_refresh_token": encrypted_refresh,
            "google_account_id": account_id,
            "google_location_id": location_id,
        }
    ).eq("id", business_id).execute()

    logger.info(
        "Google OAuth connected for business %s (account=%s, location=%s)",
        business_id,
        account_id,
        location_id,
    )

    return HTMLResponse(
        content=(
            "<html><body style='font-family:sans-serif;text-align:center;margin-top:80px'>"
            "<h1>&#10003; Google connected successfully!</h1>"
            "<p>You can close this tab.</p>"
            "</body></html>"
        )
    )


# ── Private helpers ───────────────────────────────


async def _exchange_code(
    client: httpx.AsyncClient,
    code: str,
    settings: Any,
) -> dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens."""
    payload = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }
    response = await client.post(GOOGLE_TOKEN_URL, data=payload)
    response.raise_for_status()
    return response.json()


async def _fetch_first_location(
    client: httpx.AsyncClient,
    access_token: str,
) -> tuple[str, str]:
    """Fetch the user's first GBP account and its first location.

    Returns (account_id, location_id).  Falls back to empty strings
    if the API returns no results.
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    # --- Accounts ---
    acct_resp = await client.get(GBP_ACCOUNTS_URL, headers=headers)
    acct_resp.raise_for_status()
    accounts = acct_resp.json().get("accounts", [])

    if not accounts:
        logger.warning("No GBP accounts found for this Google user")
        return ("", "")

    # accounts[0]["name"] is e.g. "accounts/123456"
    account_name: str = accounts[0]["name"]
    account_id = account_name.split("/")[-1]

    # --- Locations ---
    locations_url = (
        f"https://mybusinessbusinessinformation.googleapis.com/v1/"
        f"{account_name}/locations"
    )
    loc_resp = await client.get(locations_url, headers=headers, params={"readMask": "name"})
    loc_resp.raise_for_status()
    locations = loc_resp.json().get("locations", [])

    if not locations:
        logger.warning("No locations found for GBP account %s", account_id)
        return (account_id, "")

    # locations[0]["name"] is e.g. "locations/789012"
    location_name: str = locations[0]["name"]
    location_id = location_name.split("/")[-1]

    logger.info("Resolved GBP account=%s location=%s", account_id, location_id)
    return (account_id, location_id)
