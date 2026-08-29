"""
API Key routes — CRUD, scoping, usage tracking.

Endpoints:
  POST /v1/keys              — Create new API key
  GET  /v1/keys              — List user's API keys
  GET  /v1/keys/{key_id}     — Get key details
  PATCH /v1/keys/{key_id}    — Update key (name, scopes, limits)
  DELETE /v1/keys/{key_id}   — Revoke key
  GET  /v1/keys/{key_id}/usage — Get key usage stats
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from api_keys import (
    generate_api_key,
    hash_api_key,
    ApiKeyCreate,
    ApiKeyResponse,
    ApiKeyCreated,
)
from database import get_db
from models import ApiKey, User, UsageEvent

router = APIRouter(prefix="/v1/keys", tags=["api-keys"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ApiKeyUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    scopes: str | None = None
    rate_limit: int | None = Field(None, ge=1, le=1000)
    monthly_quota: int | None = Field(None, ge=1, le=1000000)


class KeyUsageResponse(BaseModel):
    key_id: int
    key_prefix: str
    total_requests: int
    successful_requests: int
    failed_requests: int
    avg_response_time_ms: int
    top_domains: list[dict]
    period_start: datetime
    period_end: datetime


# ---------------------------------------------------------------------------
# Create Key
# ---------------------------------------------------------------------------

@router.post("", response_model=ApiKeyCreated, status_code=201)
async def create_key(
    body: ApiKeyCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a new API key.
    
    The raw key is only shown once in the response.
    Store it securely — it cannot be retrieved later.
    """
    # Generate key
    full_key, key_hash, key_prefix = generate_api_key()
    
    # Calculate expiration
    expires_at = None
    if body.expires_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_days)
    
    # Create key record
    api_key = ApiKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=body.name,
        user_id=user.id,
        scopes=body.scopes,
        rate_limit=body.rate_limit or 30,
        monthly_quota=body.monthly_quota or 10000,
        expires_at=expires_at,
        is_active=True,
    )
    db.add(api_key)
    await db.flush()
    
    return ApiKeyCreated(
        id=api_key.id,
        name=api_key.name,
        key=full_key,
        key_prefix=key_prefix,
        scopes=api_key.scopes,
        rate_limit=api_key.rate_limit,
        monthly_quota=api_key.monthly_quota,
        expires_at=api_key.expires_at,
    )


# ---------------------------------------------------------------------------
# List Keys
# ---------------------------------------------------------------------------

@router.get("", response_model=list[ApiKeyResponse])
async def list_keys(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all API keys for the current user."""
    result = await db.execute(
        select(ApiKey)
        .where(ApiKey.user_id == user.id)
        .order_by(ApiKey.created_at.desc())
    )
    keys = result.scalars().all()
    
    return [ApiKeyResponse.model_validate(k) for k in keys]


# ---------------------------------------------------------------------------
# Get Key
# ---------------------------------------------------------------------------

@router.get("/{key_id}", response_model=ApiKeyResponse)
async def get_key(
    key_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get details for a specific API key."""
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == user.id,
        )
    )
    api_key = result.scalar_one_or_none()
    
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    
    return ApiKeyResponse.model_validate(api_key)


# ---------------------------------------------------------------------------
# Update Key
# ---------------------------------------------------------------------------

@router.patch("/{key_id}", response_model=ApiKeyResponse)
async def update_key(
    key_id: int,
    body: ApiKeyUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update an API key's name, scopes, or limits."""
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == user.id,
        )
    )
    api_key = result.scalar_one_or_none()
    
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    
    # Update fields
    if body.name is not None:
        api_key.name = body.name
    if body.scopes is not None:
        api_key.scopes = body.scopes
    if body.rate_limit is not None:
        api_key.rate_limit = body.rate_limit
    if body.monthly_quota is not None:
        api_key.monthly_quota = body.monthly_quota
    
    await db.flush()
    
    return ApiKeyResponse.model_validate(api_key)


# ---------------------------------------------------------------------------
# Revoke Key
# ---------------------------------------------------------------------------

@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    key_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Revoke an API key.
    
    This is a soft delete — the key is marked as revoked but not deleted.
    """
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == user.id,
        )
    )
    api_key = result.scalar_one_or_none()
    
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    
    api_key.is_active = False
    api_key.revoked_at = datetime.now(timezone.utc)
    await db.flush()
    
    return None


# ---------------------------------------------------------------------------
# Key Usage
# ---------------------------------------------------------------------------

@router.get("/{key_id}/usage", response_model=KeyUsageResponse)
async def get_key_usage(
    key_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get usage stats for a specific API key."""
    # Verify key belongs to user
    result = await db.execute(
        select(ApiKey).where(
            ApiKey.id == key_id,
            ApiKey.user_id == user.id,
        )
    )
    api_key = result.scalar_one_or_none()
    
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    
    # Get current billing period
    now = datetime.now(timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        period_end = period_start.replace(year=now.year + 1, month=1)
    else:
        period_end = period_start.replace(month=now.month + 1)
    
    # Get usage stats
    usage_result = await db.execute(
        select(
            sqlfunc.count(UsageEvent.id).label("total"),
            sqlfunc.count(UsageEvent.id).filter(UsageEvent.success == True).label("successful"),
            sqlfunc.count(UsageEvent.id).filter(UsageEvent.success == False).label("failed"),
            sqlfunc.coalesce(sqlfunc.avg(UsageEvent.response_time_ms), 0).label("avg_time"),
        ).where(
            UsageEvent.key_id == key_id,
            UsageEvent.created_at >= period_start,
            UsageEvent.created_at < period_end,
        )
    )
    stats = usage_result.one()
    
    # Get top domains
    domains_result = await db.execute(
        select(
            UsageEvent.domain,
            sqlfunc.count(UsageEvent.id).label("count"),
        )
        .where(
            UsageEvent.key_id == key_id,
            UsageEvent.created_at >= period_start,
            UsageEvent.created_at < period_end,
        )
        .group_by(UsageEvent.domain)
        .order_by(sqlfunc.count(UsageEvent.id).desc())
        .limit(10)
    )
    top_domains = [{"domain": row.domain, "count": row.count} for row in domains_result]
    
    return KeyUsageResponse(
        key_id=api_key.id,
        key_prefix=api_key.key_prefix,
        total_requests=stats.total or 0,
        successful_requests=stats.successful or 0,
        failed_requests=stats.failed or 0,
        avg_response_time_ms=int(stats.avg_time or 0),
        top_domains=top_domains,
        period_start=period_start,
        period_end=period_end,
    )
