"""
Webhook routes — Phase 2 Work Stream 3.

Endpoints:
  POST /v1/webhooks/test  — send a signed test webhook to a callback URL
  GET  /v1/webhooks/logs  — list the caller's webhook delivery logs

Auth: API key (subscription), via the shared `get_current_user_from_api_key`
dependency from api_keys.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl

from api_keys import get_current_user_from_api_key
from models import ApiKey, User
from webhook_manager import dispatch_webhook, get_webhook_logs

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TestWebhookRequest(BaseModel):
    url: HttpUrl = Field(..., description="Callback URL to deliver the test webhook to")
    secret: str = Field(..., min_length=1, description="HMAC-SHA256 signing secret")
    event: str = Field("test.webhook", max_length=50, description="Event name")
    data: dict = Field(default_factory=dict, description="Arbitrary payload data to include")


def _utc_now_iso() -> str:
    """Current UTC timestamp in ISO-8601 (``...Z``) format."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/test", status_code=200)
async def test_webhook(
    req: TestWebhookRequest,
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
):
    """
    Send a single signed test webhook to the given URL.

    Records the attempt in ``webhook_deliveries`` and returns the outcome
    (delivered / failed) plus the delivery record.
    """
    user, _ = auth

    payload = {
        "event": req.event,
        "timestamp": _utc_now_iso(),
        "data": req.data,
    }

    # max_attempts=0 -> a single immediate attempt, no retry/backoff.
    result = await dispatch_webhook(
        url=str(req.url),
        payload=payload,
        secret=req.secret,
        user_id=user.id,
        event=req.event,
        max_attempts=0,
    )

    if result.get("status") == "failed":
        raise HTTPException(status_code=502, detail={
            "error": "webhook_delivery_failed",
            "message": "Webhook delivery failed",
            "delivery": result,
        })

    return {
        "message": "Webhook delivered",
        "delivery": result,
    }


@router.get("/logs")
async def list_webhook_logs(
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """List the caller's webhook delivery logs, newest first."""
    user, _ = auth
    logs = await get_webhook_logs(user.id, limit=limit, offset=offset)
    return {"count": len(logs), "deliveries": logs}
