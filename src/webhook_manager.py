"""
Webhook delivery manager — Phase 2 Work Stream 3.

Delivers HMAC-SHA256-signed webhook callbacks for async scrape/crawl
completion, with exponential-backoff retries and per-attempt logging to the
``webhook_deliveries`` table.

Public API:
  generate_signature(payload, secret)  — HMAC-SHA256 signature (``sha256=...``)
  verify_signature(payload, secret, sig) — constant-time signature check
  send_webhook(url, payload, secret)   — single signed POST (no retry)
  dispatch_webhook(...)                — record + deliver with retry/backoff
  retry_webhook(delivery_id, secret)   — re-deliver a failed/pending delivery
  get_webhook_logs(user_id)            — list a user's delivery logs

Retry schedule (exponential backoff, ``max_attempts`` = retries after the
initial attempt):

    attempt 1  -> immediate
    attempt 2  -> 10s
    attempt 3  -> 30s
    attempt 4  -> 60s

After the retry budget is exhausted the delivery is marked ``failed``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy import select

from database import async_session_factory
from models import CrawlJob, WebhookDelivery, WebhookDeliveryStatus

logger = logging.getLogger("baas-webhooks")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Delay (seconds) before each retry — exponential backoff 10s -> 30s -> 60s.
RETRY_DELAYS: tuple[float, ...] = (10.0, 30.0, 60.0)

DEFAULT_TIMEOUT = 10.0          # HTTP request timeout for a single delivery
DEFAULT_MAX_ATTEMPTS = 3        # retries after the initial attempt
MAX_RESPONSE_BODY_CHARS = 1000  # truncate stored response bodies

SIGNATURE_HEADER = "X-BaaS-Signature"

# Test hook: replace with an ``httpx.AsyncClient(transport=MockTransport(...))``
# so deliveries never hit the real network during tests.
default_client: Optional[httpx.AsyncClient] = None


# ---------------------------------------------------------------------------
# Signature helpers
# ---------------------------------------------------------------------------

def generate_signature(payload: bytes, secret: str) -> str:
    """Generate an HMAC-SHA256 signature for a webhook payload."""
    signature = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={signature}"


def verify_signature(payload: bytes, secret: str, signature: str) -> bool:
    """Constant-time verification of a webhook signature."""
    expected = generate_signature(payload, secret)
    return hmac.compare_digest(expected, signature)


def _canonicalize(payload: dict) -> bytes:
    """Canonical JSON serialization (stable key order, compact) for signing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _build_signed_payload(payload: dict, secret: str) -> tuple[bytes, dict, str]:
    """Return (canonical_body, signed_payload, signature).

    The signature is computed over the canonical JSON of ``payload`` (without
    the ``signature`` field), then attached to the body for the receiver to
    verify, and also sent in the ``X-BaaS-Signature`` header.
    """
    canonical = _canonicalize(payload)
    signature = generate_signature(canonical, secret)
    signed_payload = {**payload, "signature": signature}
    return canonical, signed_payload, signature


async def _get_client(
    client: Optional[httpx.AsyncClient], timeout: float,
) -> tuple[httpx.AsyncClient, bool]:
    """Resolve a client to use, returning (client, owns_client)."""
    if client is not None:
        return client, False
    if default_client is not None:
        return default_client, False
    return httpx.AsyncClient(timeout=timeout), True


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

async def send_webhook(
    url: str,
    payload: dict,
    secret: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> httpx.Response:
    """Send a single signed webhook POST. No retry, no DB record.

    Returns the ``httpx.Response``; raises on transport/connection errors so
    callers can decide how to handle failures.
    """
    _, signed_payload, signature = _build_signed_payload(payload, secret)
    headers = {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature,
    }

    resolved, owns = await _get_client(client, timeout)
    try:
        return await resolved.post(url, json=signed_payload, headers=headers)
    finally:
        if owns:
            await resolved.aclose()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _delivery_to_dict(delivery: WebhookDelivery) -> dict:
    return {
        "delivery_id": str(delivery.id),
        "event": delivery.event,
        "callback_url": delivery.callback_url,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "max_attempts": delivery.max_attempts,
        "last_attempt_at": _iso(delivery.last_attempt_at),
        "next_retry_at": _iso(delivery.next_retry_at),
        "response_status": delivery.response_status,
        "response_body": delivery.response_body,
        "error": delivery.error,
        "created_at": _iso(delivery.created_at),
    }


async def _resolve_secret(delivery: WebhookDelivery, secret: Optional[str]) -> str:
    """Resolve the signing secret, falling back to the source crawl job."""
    if secret is not None:
        return secret
    # Crawl events carry job_id in their payload — recover the job's secret.
    job_id = (delivery.payload or {}).get("job_id")
    if job_id:
        try:
            job_uuid = uuid.UUID(str(job_id))
        except (ValueError, TypeError):
            job_uuid = None
        if job_uuid is not None:
            async with async_session_factory() as session:
                job = await session.get(CrawlJob, job_uuid)
                if job is not None and job.webhook_secret:
                    return job.webhook_secret
    raise ValueError("Webhook secret is required to re-sign the delivery")


async def deliver_with_retries(
    delivery_id: uuid.UUID,
    secret: Optional[str],
    *,
    client: Optional[httpx.AsyncClient] = None,
    delays: Optional[tuple[float, ...]] = None,
) -> Optional[WebhookDelivery]:
    """Run the retry/backoff delivery loop for a persisted delivery.

    Attempts the delivery once immediately, then retries up to
    ``delivery.max_attempts`` more times with exponential backoff. Updates the
    delivery record (attempts, status, response, error) after each attempt.
    """
    delays = delays if delays is not None else RETRY_DELAYS

    attempt = 0
    while True:
        if attempt > 0:
            delay = delays[attempt - 1] if attempt - 1 < len(delays) else delays[-1]
            if delay > 0:
                await asyncio.sleep(delay)

        async with async_session_factory() as session:
            delivery = await session.get(WebhookDelivery, delivery_id)
            if delivery is None or delivery.status == WebhookDeliveryStatus.DELIVERED:
                return delivery

            try:
                secret = await _resolve_secret(delivery, secret)
            except ValueError as exc:
                delivery.status = WebhookDeliveryStatus.FAILED
                delivery.error = str(exc)
                await session.commit()
                return delivery

            max_retries = delivery.max_attempts
            delivery.attempts = attempt + 1
            delivery.last_attempt_at = datetime.now(timezone.utc)
            delivery.next_retry_at = None

            try:
                response = await send_webhook(
                    delivery.callback_url, delivery.payload, secret, client=client,
                )
                delivery.response_status = response.status_code
                delivery.response_body = (response.text or "")[:MAX_RESPONSE_BODY_CHARS]
                if 200 <= response.status_code < 300:
                    delivery.status = WebhookDeliveryStatus.DELIVERED
                    delivery.error = None
                    await session.commit()
                    logger.info("Webhook %s delivered (attempt %d)", delivery_id, attempt + 1)
                    return delivery
                delivery.error = f"HTTP {response.status_code}"
            except Exception as exc:
                delivery.response_status = None
                delivery.response_body = None
                delivery.error = str(exc)
                logger.warning("Webhook %s attempt %d failed: %s", delivery_id, attempt + 1, exc)

            attempt += 1
            if attempt > max_retries:
                delivery.status = WebhookDeliveryStatus.FAILED
                delivery.next_retry_at = None
                await session.commit()
                logger.error("Webhook %s failed after %d attempts", delivery_id, attempt)
                return delivery

            delay_next = delays[attempt - 1] if attempt - 1 < len(delays) else delays[-1]
            delivery.status = WebhookDeliveryStatus.PENDING
            delivery.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay_next)
            await session.commit()


async def dispatch_webhook(
    url: str,
    payload: dict,
    secret: str,
    *,
    user_id: int,
    event: Optional[str] = None,
    max_attempts: Optional[int] = None,
    client: Optional[httpx.AsyncClient] = None,
    delays: Optional[tuple[float, ...]] = None,
) -> dict:
    """Create a delivery record and deliver it (with retry/backoff).

    Returns the final delivery record as a dict.
    """
    event = event or payload.get("event") or "webhook"
    max_attempts = max_attempts if max_attempts is not None else DEFAULT_MAX_ATTEMPTS

    async with async_session_factory() as session:
        delivery = WebhookDelivery(
            user_id=user_id,
            event=event,
            payload=payload,
            callback_url=url,
            status=WebhookDeliveryStatus.PENDING,
            attempts=0,
            max_attempts=max_attempts,
        )
        session.add(delivery)
        await session.commit()
        await session.refresh(delivery)
        delivery_id = delivery.id

    final = await deliver_with_retries(delivery_id, secret, client=client, delays=delays)
    if final is None:
        async with async_session_factory() as session:
            final = await session.get(WebhookDelivery, delivery_id)
    return _delivery_to_dict(final) if final else {}


async def retry_webhook(
    delivery_id: uuid.UUID,
    secret: Optional[str] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
    delays: Optional[tuple[float, ...]] = None,
) -> Optional[dict]:
    """Re-deliver a failed or pending webhook delivery.

    ``secret`` may be omitted for crawl events — it will be recovered from the
    source crawl job's ``webhook_secret``.
    """
    async with async_session_factory() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        if delivery is None:
            return None
        if delivery.status == WebhookDeliveryStatus.DELIVERED:
            return _delivery_to_dict(delivery)
        # Reset a previously-failed delivery so it can be retried.
        delivery.status = WebhookDeliveryStatus.PENDING
        delivery.next_retry_at = None
        await session.commit()

    final = await deliver_with_retries(delivery_id, secret, client=client, delays=delays)
    return _delivery_to_dict(final) if final else None


async def get_webhook_logs(
    user_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Return a user's webhook delivery logs, newest first."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.user_id == user_id)
            .order_by(WebhookDelivery.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        deliveries = result.scalars().all()
        return [_delivery_to_dict(d) for d in deliveries]
