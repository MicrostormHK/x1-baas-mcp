"""
Dashboard routes — profile, usage stats, subscription management.

Endpoints:
  GET  /v1/dashboard/profile              — Full profile + subscription + stats
  PATCH /v1/dashboard/profile             — Update profile
  POST /v1/dashboard/profile/password     — Change password
  GET  /v1/dashboard/stats/overview       — High-level stats
  GET  /v1/dashboard/stats/requests       — Request volume over time
  GET  /v1/dashboard/stats/domains        — Top domains
  GET  /v1/dashboard/stats/performance    — Response time percentiles
  GET  /v1/dashboard/stats/errors         — Error breakdown
  GET  /v1/dashboard/subscription         — Subscription details
  POST /v1/dashboard/subscription/cancel  — Cancel at period end
  POST /v1/dashboard/subscription/reactivate — Reactivate canceled
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, func as sqlfunc, case, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user, hash_password, verify_password
from billing import TIER_LIMITS, check_tier_limits
from database import get_db
from models import (
    User,
    ApiKey,
    UsageEvent,
    Subscription,
    SubscriptionTier,
    SubscriptionStatus,
)

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DashboardProfile(BaseModel):
    id: int
    email: str
    email_verified: bool
    auth_method: str
    created_at: datetime
    
    # Subscription
    tier: str
    subscription_status: str
    current_period_end: datetime | None
    
    # Quick stats
    total_api_keys: int
    active_api_keys: int
    total_requests_all_time: int
    requests_this_month: int
    remaining_this_month: int


class ProfileUpdate(BaseModel):
    email: EmailStr | None = None


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=8)
    new_password: str = Field(min_length=8, max_length=128)


class OverviewStats(BaseModel):
    requests_today: int
    requests_this_month: int
    success_rate: float
    avg_response_time_ms: int
    top_domain: str | None
    remaining_quota: int


class RequestVolume(BaseModel):
    period: str
    total: int
    successful: int
    failed: int


class DomainStats(BaseModel):
    domain: str
    request_count: int
    success_rate: float
    avg_response_time_ms: int


class PerformanceStats(BaseModel):
    p50_ms: int
    p90_ms: int
    p95_ms: int
    p99_ms: int


class ErrorStats(BaseModel):
    error_type: str
    count: int
    percentage: float


class SubscriptionDetails(BaseModel):
    tier: str
    status: str
    monthly_requests: int
    rate_limit_rpm: int
    price_monthly: float
    price_yearly: float
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    usage_this_month: int
    remaining: int
    percent_used: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_period(period: str) -> tuple[datetime, datetime]:
    """Parse period string (7d, 30d, 90d) into date range."""
    now = datetime.now(timezone.utc)
    
    if period == "7d":
        start = now - timedelta(days=7)
    elif period == "30d":
        start = now - timedelta(days=30)
    elif period == "90d":
        start = now - timedelta(days=90)
    else:
        raise HTTPException(status_code=400, detail="Invalid period. Use 7d, 30d, or 90d")
    
    return start, now


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@router.get("/profile", response_model=DashboardProfile)
async def get_profile(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get full user profile with subscription and stats."""
    # Get subscription
    sub_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = sub_result.scalar_one_or_none()
    
    tier = subscription.tier if subscription else SubscriptionTier.FREE
    limits = TIER_LIMITS[tier]
    
    # Get API key counts
    keys_result = await db.execute(
        select(
            sqlfunc.count(ApiKey.id).label("total"),
            sqlfunc.count(ApiKey.id).filter(ApiKey.is_active == True).label("active"),
        ).where(ApiKey.user_id == user.id)
    )
    keys = keys_result.one()
    
    # Get usage stats
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    usage_result = await db.execute(
        select(
            sqlfunc.count(UsageEvent.id).label("total"),
            sqlfunc.count(UsageEvent.id).filter(UsageEvent.created_at >= month_start).label("this_month"),
        ).where(UsageEvent.user_id == user.id)
    )
    usage = usage_result.one()
    
    requests_this_month = usage.this_month or 0
    
    return DashboardProfile(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified,
        auth_method=user.auth_method.value,
        created_at=user.created_at,
        tier=tier.value,
        subscription_status=subscription.status.value if subscription else "active",
        current_period_end=subscription.current_period_end if subscription else None,
        total_api_keys=keys.total or 0,
        active_api_keys=keys.active or 0,
        total_requests_all_time=usage.total or 0,
        requests_this_month=requests_this_month,
        remaining_this_month=max(0, limits.monthly_requests - requests_this_month),
    )


@router.patch("/profile")
async def update_profile(
    body: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update user profile."""
    if body.email:
        # Check if email already taken
        existing = await db.execute(
            select(User).where(User.email == body.email.lower(), User.id != user.id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered")
        
        user.email = body.email.lower()
        user.email_verified = False  # Require re-verification
        await db.flush()
    
    return {"message": "Profile updated", "email": user.email}


@router.post("/profile/password")
async def change_password(
    body: PasswordChange,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change user password."""
    if not user.password_hash:
        raise HTTPException(status_code=400, detail="Account uses OAuth, cannot change password")
    
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    
    user.password_hash = hash_password(body.new_password)
    await db.flush()
    
    return {"message": "Password changed successfully"}


# ---------------------------------------------------------------------------
# Usage Stats
# ---------------------------------------------------------------------------

@router.get("/stats/overview", response_model=OverviewStats)
async def get_stats_overview(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get high-level stats for dashboard cards."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # Get subscription limits
    sub_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = sub_result.scalar_one_or_none()
    tier = subscription.tier if subscription else SubscriptionTier.FREE
    limits = TIER_LIMITS[tier]
    
    # Today's stats
    today_result = await db.execute(
        select(
            sqlfunc.count(UsageEvent.id).label("total"),
            sqlfunc.count(UsageEvent.id).filter(UsageEvent.success == True).label("successful"),
            sqlfunc.coalesce(sqlfunc.avg(UsageEvent.response_time_ms), 0).label("avg_time"),
        ).where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= today_start,
        )
    )
    today = today_result.one()
    
    # Month stats
    month_result = await db.execute(
        select(
            sqlfunc.count(UsageEvent.id).label("total"),
        ).where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= month_start,
        )
    )
    month = month_result.one()
    
    # Top domain today
    domain_result = await db.execute(
        select(UsageEvent.domain, sqlfunc.count(UsageEvent.id).label("count"))
        .where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= today_start,
        )
        .group_by(UsageEvent.domain)
        .order_by(sqlfunc.count(UsageEvent.id).desc())
        .limit(1)
    )
    top_domain_row = domain_result.first()
    
    requests_today = today.total or 0
    requests_this_month = month.total or 0
    success_rate = (today.successful / requests_today) if requests_today > 0 else 1.0
    
    return OverviewStats(
        requests_today=requests_today,
        requests_this_month=requests_this_month,
        success_rate=round(success_rate, 4),
        avg_response_time_ms=int(today.avg_time or 0),
        top_domain=top_domain_row.domain if top_domain_row else None,
        remaining_quota=max(0, limits.monthly_requests - requests_this_month),
    )


@router.get("/stats/requests", response_model=list[RequestVolume])
async def get_stats_requests(
    period: str = Query("7d", regex="^(7d|30d|90d)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get request volume over time (daily)."""
    start, end = parse_period(period)
    
    # Query daily aggregation
    result = await db.execute(
        select(
            sqlfunc.date_trunc("day", UsageEvent.created_at).label("day"),
            sqlfunc.count(UsageEvent.id).label("total"),
            sqlfunc.count(UsageEvent.id).filter(UsageEvent.success == True).label("successful"),
            sqlfunc.count(UsageEvent.id).filter(UsageEvent.success == False).label("failed"),
        )
        .where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= start,
            UsageEvent.created_at <= end,
        )
        .group_by(text("day"))
        .order_by(text("day"))
    )
    
    return [
        RequestVolume(
            period=row.day.strftime("%Y-%m-%d"),
            total=row.total,
            successful=row.successful,
            failed=row.failed,
        )
        for row in result
    ]


@router.get("/stats/domains", response_model=list[DomainStats])
async def get_stats_domains(
    period: str = Query("30d", regex="^(7d|30d|90d)$"),
    limit: int = Query(10, ge=1, le=50),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get top domains breakdown."""
    start, end = parse_period(period)
    
    result = await db.execute(
        select(
            UsageEvent.domain,
            sqlfunc.count(UsageEvent.id).label("count"),
            sqlfunc.count(UsageEvent.id).filter(UsageEvent.success == True).label("successful"),
            sqlfunc.coalesce(sqlfunc.avg(UsageEvent.response_time_ms), 0).label("avg_time"),
        )
        .where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= start,
            UsageEvent.created_at <= end,
        )
        .group_by(UsageEvent.domain)
        .order_by(sqlfunc.count(UsageEvent.id).desc())
        .limit(limit)
    )
    
    return [
        DomainStats(
            domain=row.domain,
            request_count=row.count,
            success_rate=round(row.successful / row.count, 4) if row.count > 0 else 1.0,
            avg_response_time_ms=int(row.avg_time or 0),
        )
        for row in result
    ]


@router.get("/stats/performance", response_model=PerformanceStats)
async def get_stats_performance(
    period: str = Query("7d", regex="^(7d|30d|90d)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get response time percentiles."""
    start, end = parse_period(period)
    
    # Use PostgreSQL PERCENTILE_CONT for accurate percentiles
    result = await db.execute(
        select(
            sqlfunc.percentile_cont(0.5).within_group(UsageEvent.response_time_ms).label("p50"),
            sqlfunc.percentile_cont(0.9).within_group(UsageEvent.response_time_ms).label("p90"),
            sqlfunc.percentile_cont(0.95).within_group(UsageEvent.response_time_ms).label("p95"),
            sqlfunc.percentile_cont(0.99).within_group(UsageEvent.response_time_ms).label("p99"),
        ).where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= start,
            UsageEvent.created_at <= end,
        )
    )
    row = result.one()
    
    return PerformanceStats(
        p50_ms=int(row.p50 or 0),
        p90_ms=int(row.p90 or 0),
        p95_ms=int(row.p95 or 0),
        p99_ms=int(row.p99 or 0),
    )


@router.get("/stats/errors", response_model=list[ErrorStats])
async def get_stats_errors(
    period: str = Query("7d", regex="^(7d|30d|90d)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get error breakdown by type."""
    start, end = parse_period(period)
    
    # Get total requests for percentage calculation
    total_result = await db.execute(
        select(sqlfunc.count(UsageEvent.id)).where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= start,
            UsageEvent.created_at <= end,
            UsageEvent.success == False,
        )
    )
    total_errors = total_result.scalar() or 0
    
    if total_errors == 0:
        return []
    
    # Get error breakdown
    result = await db.execute(
        select(
            case(
                (UsageEvent.error_message.ilike("%timeout%"), "timeout"),
                (UsageEvent.status_code == 403, "forbidden"),
                (UsageEvent.status_code == 429, "rate_limited"),
                (UsageEvent.status_code >= 500, "server_error"),
                else_="other",
            ).label("error_type"),
            sqlfunc.count(UsageEvent.id).label("count"),
        )
        .where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= start,
            UsageEvent.created_at <= end,
            UsageEvent.success == False,
        )
        .group_by(text("error_type"))
        .order_by(sqlfunc.count(UsageEvent.id).desc())
    )
    
    return [
        ErrorStats(
            error_type=row.error_type,
            count=row.count,
            percentage=round(row.count / total_errors, 4),
        )
        for row in result
    ]


# ---------------------------------------------------------------------------
# Subscription Management
# ---------------------------------------------------------------------------

@router.get("/subscription", response_model=SubscriptionDetails)
async def get_subscription_details(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed subscription information."""
    sub_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = sub_result.scalar_one_or_none()
    
    tier = subscription.tier if subscription else SubscriptionTier.FREE
    limits = TIER_LIMITS[tier]
    
    # Get current usage
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    usage_result = await db.execute(
        select(sqlfunc.count(UsageEvent.id)).where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= month_start,
        )
    )
    usage = usage_result.scalar() or 0
    
    return SubscriptionDetails(
        tier=tier.value,
        status=subscription.status.value if subscription else "active",
        monthly_requests=limits.monthly_requests,
        rate_limit_rpm=limits.rate_limit_rpm,
        price_monthly=limits.price_monthly,
        price_yearly=limits.price_yearly,
        current_period_start=subscription.current_period_start if subscription else None,
        current_period_end=subscription.current_period_end if subscription else None,
        cancel_at_period_end=subscription.cancel_at_period_end if subscription else False,
        usage_this_month=usage,
        remaining=max(0, limits.monthly_requests - usage),
        percent_used=round(usage / limits.monthly_requests * 100, 2) if limits.monthly_requests > 0 else 0,
    )


@router.post("/subscription/cancel")
async def cancel_subscription(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel subscription at end of current billing period."""
    sub_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = sub_result.scalar_one_or_none()
    
    if not subscription:
        raise HTTPException(status_code=404, detail="No subscription found")
    
    if subscription.tier == SubscriptionTier.FREE:
        raise HTTPException(status_code=400, detail="Cannot cancel free tier")
    
    if subscription.cancel_at_period_end:
        raise HTTPException(status_code=400, detail="Subscription already scheduled for cancellation")
    
    subscription.cancel_at_period_end = True
    await db.flush()
    
    return {
        "message": "Subscription will be canceled at end of billing period",
        "cancels_at": subscription.current_period_end,
    }


@router.post("/subscription/reactivate")
async def reactivate_subscription(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reactivate a canceled subscription."""
    sub_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = sub_result.scalar_one_or_none()
    
    if not subscription:
        raise HTTPException(status_code=404, detail="No subscription found")
    
    if not subscription.cancel_at_period_end:
        raise HTTPException(status_code=400, detail="Subscription is not scheduled for cancellation")
    
    subscription.cancel_at_period_end = False
    await db.flush()
    
    return {"message": "Subscription reactivated"}
