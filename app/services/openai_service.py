"""OpenAI service — generate professional review reply drafts and AI help."""

import json
import logging
from pathlib import Path

from openai import AsyncOpenAI

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_KNOWLEDGE_BASE_PATH = Path(__file__).resolve().parent.parent / "knowledge_base.md"

_SYSTEM_PROMPT_TEMPLATE = (
    "You are a friendly review reply assistant for a local service business "
    "called {business_name}. Write a short, warm, professional reply to this "
    "customer review. Keep it under 3 sentences. Do not use emojis."
)


async def generate_reply(
    business_name: str,
    review_text: str,
    star_rating: int,
) -> str:
    """Use GPT-4o-mini to draft a reply to a Google review."""
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(business_name=business_name)
    user_prompt = (
        f"Star rating: {star_rating}/5\n"
        f"Review: {review_text}\n\n"
        "Write a reply from the business owner."
    )

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=200,
        temperature=0.7,
    )

    draft = response.choices[0].message.content or ""
    logger.info("Draft generated for %d-star review (%s)", star_rating, business_name)
    return draft.strip()


def _load_knowledge_base() -> str:
    """Read the knowledge base markdown file."""
    try:
        return _KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Knowledge base file not found at %s", _KNOWLEDGE_BASE_PATH)
        return ""


_HELP_SYSTEM_PROMPT = (
    "You are a friendly, helpful support assistant for ReviewEngine — "
    "a WhatsApp-based business tool for UK tradespeople. "
    "Answer the user's question based ONLY on the knowledge base provided below. "
    "Keep answers concise and practical (under 200 words). "
    "Use simple language — the user is a tradesperson, not a developer. "
    "If you don't know the answer, say so and suggest they contact support. "
    "Format for WhatsApp: use *bold* for emphasis and line breaks for readability.\n\n"
    "--- KNOWLEDGE BASE ---\n{knowledge_base}\n--- END ---"
)


async def answer_help_question(question: str) -> str:
    """Use GPT-4o-mini + the knowledge base to answer a user question."""
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    kb = _load_knowledge_base()
    system_prompt = _HELP_SYSTEM_PROMPT.format(knowledge_base=kb)

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        max_tokens=400,
        temperature=0.3,
    )

    answer = response.choices[0].message.content or ""
    logger.info("Help answer generated for question: %s", question[:80])
    return answer.strip()


_BOOKING_SYSTEM_PROMPT = (
    "You are a booking details parser for a UK tradesperson's calendar. "
    "Extract structured booking information from the user's message.\n\n"
    "Return ONLY valid JSON with these fields:\n"
    '{{\n'
    '  "title": "Short description of the job/appointment (max 60 chars)",\n'
    '  "customer_name": "Customer/client name if mentioned, otherwise empty string",\n'
    '  "date": "YYYY-MM-DD format",\n'
    '  "time": "HH:MM in 24-hour format",\n'
    '  "duration_mins": 60,\n'
    '  "notes": "Any extra details mentioned"\n'
    "}}\n\n"
    "DATE REFERENCE (use these EXACT dates — do NOT calculate yourself):\n"
    "{date_reference}\n\n"
    "RULES:\n"
    "- Use the date reference above to map day names to dates. Do NOT do your own date maths.\n"
    "- If the user says a day name like 'Tuesday', use the 'this Tuesday' date from the reference.\n"
    "- If the user says 'next Tuesday', use the 'next Tuesday' date from the reference.\n"
    "- If the user gives an explicit date like '15th July', use that directly.\n"
    "- If no time is given, default to 09:00.\n"
    "- If no duration is given, default to 60 minutes.\n"
    "- For customer_name: ONLY include a name if explicitly mentioned. Do NOT invent or guess names.\n"
    "- If no customer name is mentioned, set customer_name to an empty string.\n"
    "Always return valid JSON, nothing else."
)


def _build_date_reference() -> str:
    """Build a pre-computed date reference string so GPT doesn't have to calculate dates."""
    from datetime import date as _date, timedelta

    today = _date.today()
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    today_weekday = today.weekday()  # 0=Monday
    today_name = day_names[today_weekday]

    lines = [
        f"- Today is {today_name} {today.isoformat()} ({today.strftime('%d %B %Y')})",
        f"- Tomorrow is {day_names[(today_weekday + 1) % 7]} {(today + timedelta(days=1)).isoformat()}",
    ]

    # This week: remaining days (including today)
    for i in range(7):
        target_weekday = (today_weekday + i) % 7
        target_date = today + timedelta(days=i)
        label = "today" if i == 0 else "tomorrow" if i == 1 else day_names[target_weekday]
        lines.append(
            f"- this {day_names[target_weekday]} = {target_date.isoformat()} ({target_date.strftime('%A %d %B')})"
        )

    # Next week: all 7 days
    next_monday_offset = (7 - today_weekday) % 7 + 7  # always next week's Monday
    if today_weekday == 0:
        next_monday_offset = 7
    next_monday = today + timedelta(days=next_monday_offset)
    for i in range(7):
        target_date = next_monday + timedelta(days=i)
        lines.append(
            f"- next {day_names[i]} = {target_date.isoformat()} ({target_date.strftime('%A %d %B')})"
        )

    return "\n".join(lines)


async def parse_booking_details(text: str) -> dict:
    """Use GPT-4o-mini to parse natural-language booking input."""
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    date_reference = _build_date_reference()

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _BOOKING_SYSTEM_PROMPT.format(date_reference=date_reference)},
            {"role": "user", "content": text},
        ],
        max_tokens=300,
        temperature=0.1,
    )

    raw = response.choices[0].message.content or "{}"
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    from datetime import date as _date
    today = _date.today().isoformat()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse booking JSON: %s", raw[:200])
        data = {"title": text[:60], "date": today, "time": "09:00", "duration_mins": 60, "notes": ""}

    logger.info("Booking parsed: title=%s date=%s time=%s", data.get("title", "?"), data.get("date", "?"), data.get("time", "?"))
    return data


_RECEIPT_SYSTEM_PROMPT = (
    "You are a receipt/invoice data extraction assistant. "
    "Extract structured data from the receipt image provided. "
    "Return ONLY valid JSON with these fields:\n"
    '{\n'
    '  "vendor": "Name of the supplier/shop",\n'
    '  "date": "YYYY-MM-DD format if visible, otherwise empty string",\n'
    '  "description": "Brief summary of what was purchased (max 100 chars)",\n'
    '  "category": "one of: materials, tools, fuel, food, office, travel, subcontractor, utilities, insurance, general",\n'
    '  "line_items": [{"description": "item", "quantity": 1, "amount": 0.00}],\n'
    '  "subtotal": 0.00,\n'
    '  "tax_amount": 0.00,\n'
    '  "total": 0.00,\n'
    '  "currency": "GBP"\n'
    "}\n\n"
    "If a field is not visible on the receipt, use a sensible default. "
    "Always return valid JSON, nothing else."
)


async def extract_receipt_data(image_url: str) -> dict:
    """Use GPT-4o vision to extract structured data from a receipt image."""
    settings = get_settings()
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _RECEIPT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract the data from this receipt:"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
        max_tokens=800,
        temperature=0.1,
    )

    raw = response.choices[0].message.content or "{}"
    # Strip markdown code fences if present
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse receipt JSON: %s", raw[:200])
        data = {"vendor": "", "description": "Receipt", "total": 0, "parse_error": True}

    logger.info("Receipt extracted: vendor=%s total=%s", data.get("vendor", "?"), data.get("total", "?"))
    return data
