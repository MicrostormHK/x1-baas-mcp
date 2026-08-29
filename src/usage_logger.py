"""
Usage event logging — fire-and-forget metering for the BaaS engine.

Writes one `usage_events` row per tracked request (scrape, extract, crawl)
without blocking the HTTP response. Uses the same asyncpg session factory as
the rest of the app, and runs the insert as a background task on the running
event loop.

This is the single writer path for `usage_events`, which feeds:
  - `billing.check_tier_quota`  (monthly request counting)
  - `routes_dashboard`          (usage/stats endpoints)
  - `daily_usage_summaries`     (via `scripts/rollup_daily_usage.sql`)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

from database import async_session_factory
from models import UsageEvent

logger = logging.getLogger("baas-usage")


def extract_domain(url: str) -> str:
    """Return the host (netloc) of a URL, or the raw string if unparseable."""
    if not url:
        return ""
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


async def _persist_usage_event(
    *,
    key_id: int,
    user_id: int,
    endpoint: str,
    url: str,
    domain: str,
    status_code: int,
    success: bool,
    response_time_ms: int,
    error_message: Optional[str] = None,
    auth_method: str = "api_key",
    session_factory=None,
) -> None:
    """Insert a single UsageEvent row. Errors are logged, never raised."""
    factory = session_factory or async_session_factory
    try:
        async with factory() as session:
            session.add(UsageEvent(
                key_id=key_id,
                user_id=user_id,
                endpoint=endpoint,
                url=url,
                domain=domain,
                status_code=status_code,
                success=success,
                response_time_ms=response_time_ms,
                error_message=error_message,
                auth_method=auth_method,
            ))
            await session.commit()
    except Exception:
        logger.exception(
            "Failed to record usage event (key=%s endpoint=%s domain=%s)",
            key_id, endpoint, domain,
        )


def record_usage_event(
    *,
    key_id: int,
    user_id: int,
    endpoint: str,
    url: str,
    domain: str,
    status_code: int,
    success: bool,
    response_time_ms: int,
    error_message: Optional[str] = None,
    auth_method: str = "api_key",
    session_factory=None,
) -> None:
    """
    Fire-and-forget usage event logging.

    Schedules the insert on the running event loop and returns immediately so
    the HTTP response is never blocked by metering. Falls back to a
    synchronous insert when there is no running loop (e.g. some test contexts).
    """
    kwargs = dict(
        key_id=key_id,
        user_id=user_id,
        endpoint=endpoint,
        url=url,
        domain=domain,
        status_code=status_code,
        success=success,
        response_time_ms=response_time_ms,
        error_message=error_message,
        auth_method=auth_method,
        session_factory=session_factory,
    )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — insert synchronously in a fresh loop.
        try:
            asyncio.run(_persist_usage_event(**kwargs))
        except Exception:
            logger.exception("Failed to record usage event synchronously")
        return

    asyncio.create_task(_persist_usage_event(**kwargs))
