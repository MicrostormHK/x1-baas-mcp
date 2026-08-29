"""
Crawl routes — Phase 2 Work Stream 2.

Endpoints:
  POST   /v1/crawl                    — start a crawl job (async)
  GET    /v1/crawl                    — list the caller's jobs
  GET    /v1/crawl/{job_id}           — job status
  GET    /v1/crawl/{job_id}/results   — job results
  DELETE /v1/crawl/{job_id}           — cancel a job

Auth: API key (subscription), via the shared `get_current_user_from_api_key`
dependency from api_keys.py. The job executes in the background on the running
event loop (`asyncio.create_task`).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api_keys import get_current_user_from_api_key
from crawl_engine import CrawlEngine
from database import async_session_factory, get_db
from models import ApiKey, CrawlJob, CrawlJobStatus, CrawlResult, User
from usage_logger import record_usage_event, extract_domain
from webhook_manager import dispatch_webhook

logger = logging.getLogger("baas-crawl")

router = APIRouter(prefix="/v1/crawl", tags=["crawl"])

# Module-level engine instance (network transport; injectable for tests).
crawl_engine = CrawlEngine()

# Background job bookkeeping.
_running_tasks: dict[uuid.UUID, asyncio.Task] = {}
_cancel_events: dict[uuid.UUID, asyncio.Event] = {}

VALID_MODES = {"sitemap", "link", "pattern", "batch"}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CrawlOptions(BaseModel):
    respect_robots_txt: bool = Field(True, description="Respect robots.txt directives")
    delay_ms: int = Field(1000, ge=0, le=60_000, description="Delay between page fetches (ms)")
    timeout_per_page: int = Field(30_000, ge=1000, le=120_000, description="Per-page fetch timeout (ms)")
    parallel: int = Field(5, ge=1, le=50, description="Concurrency for batch mode")
    follow_external: bool = Field(False, description="Follow off-domain links (link mode)")
    include_patterns: Optional[list[str]] = Field(None, description="Glob/regex patterns to include (link mode)")
    exclude_patterns: Optional[list[str]] = Field(None, description="Glob/regex patterns to exclude (link mode)")


class CrawlRequest(BaseModel):
    start_url: Optional[HttpUrl] = Field(None, description="Starting URL (sitemap/link/pattern modes)")
    mode: Optional[str] = Field(None, description="sitemap | link | pattern | batch (auto-detected if omitted)")
    max_pages: int = Field(100, ge=1, le=1000, description="Maximum pages to crawl")
    depth: int = Field(3, ge=0, le=10, description="Link crawl depth")
    url_pattern: Optional[str] = Field(None, description="Glob/regex pattern for pattern mode")
    urls: Optional[list[HttpUrl]] = Field(None, description="Explicit URL list for batch mode")
    options: Optional[CrawlOptions] = Field(None, description="Crawl options")
    callback_url: Optional[HttpUrl] = Field(None, description="Webhook URL to notify on completion")
    webhook_secret: Optional[str] = Field(None, description="HMAC-SHA256 signing secret for the webhook")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _job_to_dict(job: CrawlJob) -> dict:
    return {
        "job_id": str(job.id),
        "status": job.status,
        "mode": job.mode,
        "start_url": job.start_url,
        "max_pages": job.max_pages,
        "depth": job.depth,
        "progress": job.progress or {},
        "result_count": job.result_count,
        "error_count": job.error_count,
        "error": job.error,
        "callback_url": job.callback_url,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "created_at": job.created_at,
    }


def _result_to_dict(result: CrawlResult) -> dict:
    return {
        "id": str(result.id),
        "url": result.url,
        "status": result.status,
        "markdown": result.markdown,
        "metadata": result.page_metadata or {},
        "error": result.error,
        "crawled_at": result.crawled_at,
    }


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def _resolve_mode(req: CrawlRequest) -> str:
    """Resolve the effective crawl mode, auto-detecting batch when only urls given."""
    if req.mode:
        mode = req.mode.lower()
        if mode not in VALID_MODES:
            raise HTTPException(status_code=400, detail={
                "error": "invalid_mode",
                "message": f"mode must be one of: {', '.join(sorted(VALID_MODES))}",
            })
        return mode
    if req.urls:
        return "batch"
    return "link"


def _build_options(req: CrawlRequest) -> dict:
    options = (req.options or CrawlOptions()).model_dump()
    if req.url_pattern:
        options["url_pattern"] = req.url_pattern
    return options


async def _get_job(db: AsyncSession, job_id: str, user_id: int) -> Optional[CrawlJob]:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Crawl job not found")

    result = await db.execute(
        select(CrawlJob).where(CrawlJob.id == job_uuid, CrawlJob.user_id == user_id)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Background job execution
# ---------------------------------------------------------------------------

def spawn_crawl_job(job_id: uuid.UUID) -> None:
    """Schedule a crawl job on the running event loop."""
    loop = asyncio.get_running_loop()
    task = loop.create_task(execute_crawl_job(job_id))
    _running_tasks[job_id] = task
    task.add_done_callback(lambda _t, jid=job_id: _running_tasks.pop(jid, None))


def _signal_cancel(job_id: uuid.UUID) -> None:
    """Cooperatively stop a running job and cancel its task."""
    cancel_event = _cancel_events.setdefault(job_id, asyncio.Event())
    cancel_event.set()
    task = _running_tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()


async def _mark_failed(job_id: uuid.UUID, exc: Exception) -> None:
    async with async_session_factory() as session:
        job = await session.get(CrawlJob, job_id)
        if job is not None and job.status not in (CrawlJobStatus.CANCELED,):
            job.status = CrawlJobStatus.FAILED
            job.error = str(exc)
            job.completed_at = datetime.now(timezone.utc)
            await session.commit()


def _finalize_job(job: CrawlJob, canceled: bool) -> None:
    if canceled:
        if job.status != CrawlJobStatus.CANCELED:
            job.status = CrawlJobStatus.CANCELED
        job.completed_at = job.completed_at or datetime.now(timezone.utc)
    else:
        job.status = CrawlJobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc)


async def _record_result(session: AsyncSession, job: CrawlJob, job_id: uuid.UUID,
                         result: dict) -> None:
    url = result["url"]
    if result.get("skipped"):
        job.progress = {
            **job.progress,
            "skipped": job.progress.get("skipped", 0) + 1,
            "current_url": url,
        }
        await session.commit()
        return

    failed = 1 if result.get("error") else 0
    job.progress = {
        **job.progress,
        "pages_crawled": job.progress.get("pages_crawled", 0) + 1,
        "pages_failed": job.progress.get("pages_failed", 0) + failed,
        "current_url": url,
    }
    job.result_count += (0 if failed else 1)
    job.error_count += failed

    session.add(CrawlResult(
        job_id=job_id,
        url=url,
        status=result.get("status"),
        markdown=result.get("markdown"),
        page_metadata=result.get("metadata") or {},
        error=result.get("error"),
        crawled_at=datetime.now(timezone.utc),
    ))
    await session.commit()


async def _run_crawl(job_id: uuid.UUID, urls: list[str], mode: str,
                     options: dict, cancel_event: asyncio.Event) -> None:
    respect_robots = options.get("respect_robots_txt", True)
    delay_ms = options.get("delay_ms", 1000)
    parallel = options.get("parallel", 5)

    if mode == "batch":
        # Respect robots.txt by pre-filtering disallowed URLs, then fetch the
        # allowed set in parallel.
        allowed: list[str] = []
        skipped: list[str] = []
        for url in urls:
            if await crawl_engine.is_allowed(url, respect_robots):
                allowed.append(url)
            else:
                skipped.append(url)

        results = await crawl_engine.crawl_batch(allowed, max_concurrent=parallel)
        async with async_session_factory() as session:
            job = await session.get(CrawlJob, job_id)
            if job is None:
                return
            for url in skipped:
                await _record_result(session, job, job_id, {
                    "url": url, "skipped": "robots.txt", "status": None,
                    "markdown": None, "metadata": {}, "error": None,
                })
            for url, result in zip(allowed, results):
                if cancel_event.is_set():
                    break
                await _record_result(session, job, job_id, {"url": url, **result})
            _finalize_job(job, cancel_event.is_set())
            await session.commit()
        return

    async def should_cancel() -> bool:
        return cancel_event.is_set()

    async with async_session_factory() as session:
        job = await session.get(CrawlJob, job_id)
        if job is None:
            return
        async for result in crawl_engine.crawl_many(
            urls,
            respect_robots=respect_robots,
            delay_ms=delay_ms,
            should_cancel=should_cancel,
        ):
            if cancel_event.is_set():
                break
            await _record_result(session, job, job_id, result)
        _finalize_job(job, cancel_event.is_set())
        await session.commit()


async def execute_crawl_job(job_id: uuid.UUID) -> None:
    """Run a crawl job to completion in the background."""
    cancel_event = _cancel_events.setdefault(job_id, asyncio.Event())

    # Mark running + capture job configuration.
    async with async_session_factory() as session:
        job = await session.get(CrawlJob, job_id)
        if job is None or job.status == CrawlJobStatus.CANCELED:
            return
        job.status = CrawlJobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)
        job.progress = {
            "pages_crawled": 0,
            "pages_total": 0,
            "pages_failed": 0,
            "skipped": 0,
            "current_url": None,
        }
        await session.commit()
        mode = job.mode
        start_url = job.start_url
        urls_batch = list(job.urls or [])
        max_pages = job.max_pages
        depth = job.depth
        options = dict(job.options or {})

    if cancel_event.is_set():
        return

    # Discovery phase.
    try:
        if mode == "batch":
            urls = urls_batch
        elif mode == "sitemap":
            urls = await crawl_engine.discover_sitemap(start_url, max_pages)
        elif mode == "link":
            urls = await crawl_engine.discover_links(
                start_url,
                max_pages=max_pages,
                depth=depth,
                follow_external=options.get("follow_external", False),
                include_patterns=options.get("include_patterns"),
                exclude_patterns=options.get("exclude_patterns"),
            )
        elif mode == "pattern":
            urls = await crawl_engine.discover_pattern(
                start_url, options.get("url_pattern") or "", max_pages,
            )
        else:
            raise ValueError(f"Unknown crawl mode: {mode}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Crawl discovery failed for job %s", job_id)
        await _mark_failed(job_id, exc)
        return

    # Record the discovered total for progress reporting.
    async with async_session_factory() as session:
        job = await session.get(CrawlJob, job_id)
        if job is not None and job.status != CrawlJobStatus.CANCELED:
            job.progress = {**job.progress, "pages_total": len(urls)}
            await session.commit()

    # Crawl + persist.
    try:
        await _run_crawl(job_id, urls, mode, options, cancel_event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Crawl execution failed for job %s", job_id)
        await _mark_failed(job_id, exc)
        return

    # Notify via signed webhook on successful completion.
    await _maybe_send_completion_webhook(job_id)


async def _maybe_send_completion_webhook(job_id: uuid.UUID) -> None:
    """Send a `crawl.completed` webhook if the job configured a callback_url."""
    async with async_session_factory() as session:
        job = await session.get(CrawlJob, job_id)
        if job is None or not job.callback_url:
            return
        if job.status != CrawlJobStatus.COMPLETED:
            return
        callback_url = job.callback_url
        secret = job.webhook_secret or ""
        user_id = job.user_id
        progress = job.progress or {}
        pages_crawled = progress.get("pages_crawled", job.result_count)
        pages_failed = progress.get("pages_failed", job.error_count)

    payload = {
        "event": "crawl.completed",
        "job_id": str(job_id),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": {
            "status": "completed",
            "pages_crawled": pages_crawled,
            "pages_failed": pages_failed,
        },
    }

    try:
        await dispatch_webhook(
            url=callback_url,
            payload=payload,
            secret=secret,
            user_id=user_id,
            event="crawl.completed",
        )
    except Exception as exc:
        # Webhook failures must never fail the crawl job itself.
        logger.exception("Webhook dispatch failed for job %s: %s", job_id, exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=202)
async def start_crawl(
    req: CrawlRequest,
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Start a multi-page crawl job. Returns immediately with the job id."""
    user, api_key = auth
    start = time.monotonic()
    mode = _resolve_mode(req)
    options = _build_options(req)

    if mode != "batch" and not req.start_url:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_request",
            "message": f"start_url is required for mode '{mode}'",
        })
    if mode == "batch" and not req.urls:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_request",
            "message": "urls is required for mode 'batch'",
        })
    if mode == "pattern" and not req.url_pattern:
        raise HTTPException(status_code=400, detail={
            "error": "invalid_request",
            "message": "url_pattern is required for mode 'pattern'",
        })

    job = CrawlJob(
        user_id=user.id,
        api_key_id=api_key.id,
        status=CrawlJobStatus.PENDING,
        mode=mode,
        start_url=str(req.start_url) if req.start_url else None,
        urls=[str(u) for u in req.urls] if req.urls else None,
        max_pages=req.max_pages,
        depth=req.depth,
        options=options,
        progress={},
        callback_url=str(req.callback_url) if req.callback_url else None,
        webhook_secret=req.webhook_secret,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    spawn_crawl_job(job.id)

    # Record a usage event for the crawl request (fire-and-forget).
    primary_url = str(req.start_url) if req.start_url else (str(req.urls[0]) if req.urls else "")
    record_usage_event(
        key_id=api_key.id,
        user_id=user.id,
        endpoint="/v1/crawl",
        url=primary_url,
        domain=extract_domain(primary_url),
        status_code=202,
        success=True,
        response_time_ms=int((time.monotonic() - start) * 1000),
        auth_method="api_key",
        session_factory=async_session_factory,
    )

    return {
        "job_id": str(job.id),
        "status": job.status,
        "mode": job.mode,
        "start_url": job.start_url,
        "max_pages": job.max_pages,
        "depth": job.depth,
        "created_at": job.created_at,
    }


@router.get("")
async def list_jobs(
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's crawl jobs, newest first."""
    user, _ = auth
    result = await db.execute(
        select(CrawlJob)
        .where(CrawlJob.user_id == user.id)
        .order_by(CrawlJob.created_at.desc())
    )
    jobs = result.scalars().all()
    return [_job_to_dict(j) for j in jobs]


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Get a crawl job's status and progress."""
    user, _ = auth
    job = await _get_job(db, job_id, user.id)
    if not job:
        raise HTTPException(status_code=404, detail="Crawl job not found")
    return _job_to_dict(job)


@router.get("/{job_id}/results")
async def get_job_results(
    job_id: str,
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """Get a crawl job's results (paginated)."""
    user, _ = auth
    job = await _get_job(db, job_id, user.id)
    if not job:
        raise HTTPException(status_code=404, detail="Crawl job not found")

    result = await db.execute(
        select(CrawlResult)
        .where(CrawlResult.job_id == job.id)
        .order_by(CrawlResult.crawled_at)
        .offset(offset)
        .limit(limit)
    )
    results = result.scalars().all()
    return {
        "job_id": str(job.id),
        "count": len(results),
        "results": [_result_to_dict(r) for r in results],
    }


@router.delete("/{job_id}", status_code=204)
async def cancel_job(
    job_id: str,
    auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a pending or running crawl job."""
    user, _ = auth
    job = await _get_job(db, job_id, user.id)
    if not job:
        raise HTTPException(status_code=404, detail="Crawl job not found")

    if job.status in (CrawlJobStatus.PENDING, CrawlJobStatus.RUNNING):
        job.status = CrawlJobStatus.CANCELED
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()
        _signal_cancel(uuid.UUID(job_id))

    return None
