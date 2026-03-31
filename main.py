"""WhatsApp Review Engine — FastAPI application entry-point."""

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.billing import router as billing_router
from app.api.cron import router as cron_router
from app.api.member import router as member_router, public_router as member_public_router
from app.api.oauth import router as oauth_router
from app.api.webhooks import router as webhook_router

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application-wide resources (httpx client)."""
    app.state.http_client = httpx.AsyncClient(timeout=30.0)
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


# Serve frontend static files last (catch-all mount)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
