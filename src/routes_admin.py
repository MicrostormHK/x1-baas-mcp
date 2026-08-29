"""
Admin routes — user management, system stats.

Endpoints:
  GET  /v1/admin/users          — List all users (paginated)
  GET  /v1/admin/users/{id}     — User details
  PATCH /v1/admin/users/{id}    — Update user (admin action)
  GET  /v1/admin/stats          — System-wide statistics
  GET  /v1/admin/keys           — All API keys
  GET  /v1/admin/subscriptions  — All subscriptions
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_admin
from billing import TIER_LIMITS
from database import get_db
from models import (
    User,
    ApiKey,
    UsageEvent,
    Subscription,
    SubscriptionTier,
    SubscriptionStatus,
)

router = APIRouter(prefix="/v1/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AdminUserSummary(BaseModel):
    id: int
    email: str
    email_verified: bool
    is_active: bool
    is_admin: bool
    auth_method: str
    tier: str
    total_keys: int
    total_requests: int
    created_at: datetime
    last_active: datetime | None


class AdminUserList(BaseModel):
    users: list[AdminUserSummary]
    total: int
    page: int
    per_page: int


class AdminUserDetail(BaseModel):
    id: int
    email: str
    email_verified: bool
    is_active: bool
    is_admin: bool
    auth_method: str
    created_at: datetime
    
    # Subscription
    tier: str
    subscription_status: str
    current_period_end: datetime | None
    
    # Stats
    total_keys: int
    active_keys: int
    total_requests: int
    requests_this_month: int


class SystemStats(BaseModel):
    total_users: int
    active_users_30d: int
    total_api_keys: int
    active_api_keys: int
    total_requests_30d: int
    revenue_mrr: float
    tier_breakdown: dict[str, int]


class AdminApiKeySummary(BaseModel):
    id: int
    key_prefix: str
    name: str | None
    user_email: str
    is_active: bool
    usage_count: int
    created_at: datetime
    last_used_at: datetime | None


class AdminSubscriptionSummary(BaseModel):
    id: int
    user_email: str
    tier: str
    status: str
    stripe_subscription_id: str | None
    current_period_end: datetime | None
    cancel_at_period_end: bool


# ---------------------------------------------------------------------------
# User Management
# ---------------------------------------------------------------------------

@router.get("/users", response_model=AdminUserList)
async def list_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all users (paginated)."""
    offset = (page - 1) * per_page
    
    # Get total count
    total_result = await db.execute(select(sqlfunc.count(User.id)))
    total = total_result.scalar() or 0
    
    # Get users with subscription and stats
    users_result = await db.execute(
        select(User)
        .order_by(User.created_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    users = users_result.scalars().all()
    
    # Build summaries
    summaries = []
    for u in users:
        # Get subscription
        sub_result = await db.execute(
            select(Subscription).where(Subscription.user_id == u.id)
        )
        subscription = sub_result.scalar_one_or_none()
        tier = subscription.tier if subscription else SubscriptionTier.FREE
        
        # Get key count
        keys_result = await db.execute(
            select(
                sqlfunc.count(ApiKey.id).label("total"),
            ).where(ApiKey.user_id == u.id)
        )
        keys = keys_result.one()
        
        # Get total requests
        req_result = await db.execute(
            select(sqlfunc.count(UsageEvent.id)).where(UsageEvent.user_id == u.id)
        )
        total_requests = req_result.scalar() or 0
        
        # Get last activity
        last_result = await db.execute(
            select(sqlfunc.max(UsageEvent.created_at)).where(UsageEvent.user_id == u.id)
        )
        last_active = last_result.scalar()
        
        summaries.append(AdminUserSummary(
            id=u.id,
            email=u.email,
            email_verified=u.email_verified,
            is_active=u.is_active,
            is_admin=u.is_admin,
            auth_method=u.auth_method.value,
            tier=tier.value,
            total_keys=keys.total or 0,
            total_requests=total_requests,
            created_at=u.created_at,
            last_active=last_active,
        ))
    
    return AdminUserList(
        users=summaries,
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/users/{user_id}", response_model=AdminUserDetail)
async def get_user_detail(
    user_id: int,
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get detailed user information."""
    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get subscription
    sub_result = await db.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    subscription = sub_result.scalar_one_or_none()
    tier = subscription.tier if subscription else SubscriptionTier.FREE
    
    # Get key counts
    keys_result = await db.execute(
        select(
            sqlfunc.count(ApiKey.id).label("total"),
            sqlfunc.count(ApiKey.id).filter(ApiKey.is_active == True).label("active"),
        ).where(ApiKey.user_id == user_id)
    )
    keys = keys_result.one()
    
    # Get request counts
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    req_result = await db.execute(
        select(
            sqlfunc.count(UsageEvent.id).label("total"),
            sqlfunc.count(UsageEvent.id).filter(UsageEvent.created_at >= month_start).label("this_month"),
        ).where(UsageEvent.user_id == user_id)
    )
    reqs = req_result.one()
    
    return AdminUserDetail(
        id=target_user.id,
        email=target_user.email,
        email_verified=target_user.email_verified,
        is_active=target_user.is_active,
        is_admin=target_user.is_admin,
        auth_method=target_user.auth_method.value,
        created_at=target_user.created_at,
        tier=tier.value,
        subscription_status=subscription.status.value if subscription else "active",
        current_period_end=subscription.current_period_end if subscription else None,
        total_keys=keys.total or 0,
        active_keys=keys.active or 0,
        total_requests=reqs.total or 0,
        requests_this_month=reqs.this_month or 0,
    )


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    is_active: bool | None = None,
    is_admin: bool | None = None,
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update user (admin action)."""
    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()
    
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if is_active is not None:
        target_user.is_active = is_active
    if is_admin is not None:
        target_user.is_admin = is_admin
    
    await db.flush()
    
    return {
        "message": "User updated",
        "user_id": user_id,
        "is_active": target_user.is_active,
        "is_admin": target_user.is_admin,
    }


# ---------------------------------------------------------------------------
# System Stats
# ---------------------------------------------------------------------------

@router.get("/stats", response_model=SystemStats)
async def get_system_stats(
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get system-wide statistics."""
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)
    
    # Total users
    users_result = await db.execute(select(sqlfunc.count(User.id)))
    total_users = users_result.scalar() or 0
    
    # Active users (30d)
    active_result = await db.execute(
        select(sqlfunc.count(sqlfunc.distinct(UsageEvent.user_id))).where(
            UsageEvent.created_at >= thirty_days_ago
        )
    )
    active_users = active_result.scalar() or 0
    
    # API keys
    keys_result = await db.execute(
        select(
            sqlfunc.count(ApiKey.id).label("total"),
            sqlfunc.count(ApiKey.id).filter(ApiKey.is_active == True).label("active"),
        )
    )
    keys = keys_result.one()
    
    # Requests (30d)
    req_result = await db.execute(
        select(sqlfunc.count(UsageEvent.id)).where(
            UsageEvent.created_at >= thirty_days_ago
        )
    )
    total_requests = req_result.scalar() or 0
    
    # Tier breakdown
    tier_result = await db.execute(
        select(
            Subscription.tier,
            sqlfunc.count(Subscription.id).label("count"),
        )
        .where(Subscription.status == SubscriptionStatus.ACTIVE)
        .group_by(Subscription.tier)
    )
    tier_breakdown = {row.tier.value: row.count for row in tier_result}
    
    # Calculate MRR
    mrr = 0.0
    for tier_str, count in tier_breakdown.items():
        try:
            tier = SubscriptionTier(tier_str)
            mrr += TIER_LIMITS[tier].price_monthly * count
        except (ValueError, KeyError):
            pass
    
    return SystemStats(
        total_users=total_users,
        active_users_30d=active_users,
        total_api_keys=keys.total or 0,
        active_api_keys=keys.active or 0,
        total_requests_30d=total_requests,
        revenue_mrr=mrr,
        tier_breakdown=tier_breakdown,
    )


# ---------------------------------------------------------------------------
# All Keys
# ---------------------------------------------------------------------------

@router.get("/keys", response_model=list[AdminApiKeySummary])
async def list_all_keys(
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all API keys."""
    result = await db.execute(
        select(ApiKey, User.email)
        .join(User, ApiKey.user_id == User.id)
        .order_by(ApiKey.created_at.desc())
        .limit(limit)
    )
    
    return [
        AdminApiKeySummary(
            id=row.ApiKey.id,
            key_prefix=row.ApiKey.key_prefix,
            name=row.ApiKey.name,
            user_email=row.email,
            is_active=row.ApiKey.is_active,
            usage_count=row.ApiKey.usage_count,
            created_at=row.ApiKey.created_at,
            last_used_at=row.ApiKey.last_used_at,
        )
        for row in result
    ]


# ---------------------------------------------------------------------------
# All Subscriptions
# ---------------------------------------------------------------------------

@router.get("/subscriptions", response_model=list[AdminSubscriptionSummary])
async def list_all_subscriptions(
    limit: int = Query(100, ge=1, le=500),
    user: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all subscriptions."""
    result = await db.execute(
        select(Subscription, User.email)
        .join(User, Subscription.user_id == User.id)
        .order_by(Subscription.created_at.desc())
        .limit(limit)
    )
    
    return [
        AdminSubscriptionSummary(
            id=row.Subscription.id,
            user_email=row.email,
            tier=row.Subscription.tier.value,
            status=row.Subscription.status.value,
            stripe_subscription_id=row.Subscription.stripe_subscription_id,
            current_period_end=row.Subscription.current_period_end,
            cancel_at_period_end=row.Subscription.cancel_at_period_end,
        )
        for row in result
    ]
