"""Google Business Profile API helpers — token refresh, reviews, reply posting."""

import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GBP_API_BASE = "https://mybusiness.googleapis.com/v4"
PLACES_API_BASE = "https://places.googleapis.com/v1"


async def get_reviews_by_place_id(place_id: str) -> list[dict[str, Any]]:
    """Fetch reviews for a place using the Places API (New) — API key only.

    Returns a list of dicts with keys: author_name, text, rating, time,
    relative_time, author_url.  Up to 5 reviews sorted by newest.
    """
    settings = get_settings()
    api_key = settings.google_api_key
    if not api_key:
        logger.warning("GOOGLE_API_KEY not set — cannot fetch reviews via Places API")
        return []

    url = f"{PLACES_API_BASE}/places/{place_id}"
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "reviews",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()

    raw_reviews = data.get("reviews", [])
    reviews = []
    for r in raw_reviews:
        reviews.append({
            "author_name": r.get("authorAttribution", {}).get("displayName", "A customer"),
            "text": (r.get("text", {}).get("text", "") if isinstance(r.get("text"), dict)
                     else r.get("text", "")),
            "rating": r.get("rating", 5),
            "time": r.get("publishTime", ""),
            "relative_time": r.get("relativePublishTimeDescription", ""),
        })

    logger.info("Fetched %d reviews for place %s via Places API", len(reviews), place_id)
    return reviews


async def refresh_access_token(refresh_token: str) -> str:
    """Exchange a refresh token for a fresh Google access token.

    Returns the access_token string, or raises on failure.
    """
    settings = get_settings()
    payload = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=payload)
        response.raise_for_status()
        token = response.json().get("access_token", "")

    if not token:
        raise ValueError("Google token response did not contain an access_token")

    logger.debug("Refreshed Google access token (last4=%s)", token[-4:])
    return token


async def get_reviews(
    access_token: str,
    location_id: str,
    page_size: int = 5,
) -> list[dict[str, Any]]:
    """Fetch the most recent reviews for a GBP location.

    Returns a list of review dicts sorted by updateTime descending.
    The ``location_id`` should be a full resource name including the
    account prefix, e.g. ``accounts/123/locations/456``.
    """
    url = f"{GBP_API_BASE}/{location_id}/reviews"
    headers = {"Authorization": f"Bearer {access_token}"}
    params: dict[str, Any] = {
        "pageSize": page_size,
        "orderBy": "updateTime desc",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

    reviews: list[dict[str, Any]] = data.get("reviews", [])
    logger.info("Fetched %d reviews for %s", len(reviews), location_id)
    return reviews


async def post_review_reply(
    access_token: str,
    review_name: str,
    reply_text: str,
) -> bool:
    """Post an owner reply to a Google review.

    ``review_name`` is the full resource path, e.g.
    ``accounts/123/locations/456/reviews/789``.

    Returns True on success.
    """
    url = f"{GBP_API_BASE}/{review_name}/reply"
    headers = {"Authorization": f"Bearer {access_token}"}
    payload = {"comment": reply_text}

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.put(url, headers=headers, json=payload)
        response.raise_for_status()

    logger.info("Reply posted to review %s", review_name)
    return True
