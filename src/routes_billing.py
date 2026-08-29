"""
Billing routes — subscription management, checkout, webhooks.

Endpoints:
  GET  /v1/billing/tiers              — List available tiers
  GET  /v1/billing/subscription       — Current subscription
  POST /v1/billing/checkout           — Create checkout session
  POST /v1/billing/webhook            — Stripe webhook handler
  GET  /v1/billing/usage              — Current usage stats
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select, func as sqlfunc
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from billing import (
    TIER_LIMITS,
    TierLimits,
    CheckoutSession,
    WebhookEvent,
    create_checkout_session,
    handle_webhook,
    check_tier_limits,
    TierCheckResult,
)
from database import get_db
from models import (
    User,
    Subscription,
    SubscriptionTier,
    SubscriptionStatus,
    UsageEvent,
)

router = APIRouter(prefix="/v1/billing", tags=["billing"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class CheckoutRequest(BaseModel):
    tier: SubscriptionTier
    success_url: str = "https://x1.baas.microstorm.biz/success"
    cancel_url: str = "https://x1.baas.microstorm.biz/pricing"


class SubscriptionResponse(BaseModel):
    tier: str
    status: str
    monthly_requests: int
    rate_limit_rpm: int
    current_usage: int
    remaining: int
    price_monthly: float
    price_yearly: float
    current_period_end: datetime | None = None


class UsageResponse(BaseModel):
    tier: str
    monthly_requests: int
    current_usage: int
    remaining: int
    rate_limit_rpm: int
    percent_used: float


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

@router.get("/tiers")
async def list_tiers():
    """List all available subscription tiers with limits and pricing."""
    return {
        "tiers": [
            {
                "id": tier.value,
                "name": limits.name,
                "monthly_requests": limits.monthly_requests,
                "rate_limit_rpm": limits.rate_limit_rpm,
                "max_concurrent": limits.max_concurrent,
                "price_monthly": limits.price_monthly,
                "price_yearly": limits.price_yearly,
                "features": limits.features,
            }
            for tier, limits in TIER_LIMITS.items()
        ]
    }


# ---------------------------------------------------------------------------
# Current Subscription
# ---------------------------------------------------------------------------

@router.get("/subscription", response_model=SubscriptionResponse)
async def get_subscription(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the current user's subscription details."""
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = result.scalar_one_or_none()
    
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
    current_usage = usage_result.scalar() or 0
    
    return SubscriptionResponse(
        tier=tier.value,
        status=subscription.status.value if subscription else "active",
        monthly_requests=limits.monthly_requests,
        rate_limit_rpm=limits.rate_limit_rpm,
        current_usage=current_usage,
        remaining=max(0, limits.monthly_requests - current_usage),
        price_monthly=limits.price_monthly,
        price_yearly=limits.price_yearly,
        current_period_end=subscription.current_period_end if subscription else None,
    )


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

@router.post("/checkout", response_model=CheckoutSession)
async def create_checkout(
    body: CheckoutRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a Stripe checkout session for subscription upgrade.
    
    In production, this would create a real Stripe checkout session.
    For now, returns a mock session.
    """
    if body.tier == SubscriptionTier.FREE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot checkout for free tier",
        )
    
    # Check if user already has this tier
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = result.scalar_one_or_none()
    
    if subscription and subscription.tier == body.tier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Already subscribed to {body.tier.value} tier",
        )
    
    return await create_checkout_session(
        user=user,
        tier=body.tier,
        success_url=body.success_url,
        cancel_url=body.cancel_url,
    )


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Handle Stripe webhook events.
    
    In production, this would verify the webhook signature.
    For now, accepts any payload.
    """
    body = await request.json()
    
    event = WebhookEvent(
        event_type=body.get("type", "unknown"),
        data=body.get("data", {}).get("object", {}),
    )
    
    result = await handle_webhook(event, db)
    return result


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current usage stats for the billing period."""
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = result.scalar_one_or_none()
    
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
    current_usage = usage_result.scalar() or 0
    
    percent_used = (current_usage / limits.monthly_requests * 100) if limits.monthly_requests > 0 else 0
    
    return UsageResponse(
        tier=tier.value,
        monthly_requests=limits.monthly_requests,
        current_usage=current_usage,
        remaining=max(0, limits.monthly_requests - current_usage),
        rate_limit_rpm=limits.rate_limit_rpm,
        percent_used=round(percent_used, 2),
    )
