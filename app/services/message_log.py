"""Helpers for logging outbound messages and sending receipts to business owners."""

from app.db.supabase import get_supabase


def log_message(
    business_id: str,
    to_phone: str,
    message_body: str,
    message_type: str = "text",
    direction: str = "outbound",
) -> dict:
    """Insert a row into the messages table and return it."""
    db = get_supabase()
    result = (
        db.table("messages")
        .insert({
            "business_id": business_id,
            "direction": direction,
            "to_phone": to_phone,
            "message_type": message_type,
            "message_body": message_body,
            "status": "sent",
        })
        .execute()
    )
    return result.data[0] if result.data else {}
