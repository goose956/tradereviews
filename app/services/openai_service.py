"""OpenAI service — generate professional review reply drafts and AI help."""

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
