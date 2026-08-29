"""
Billing layer — subscription tiers, Stripe integration (mocked), tier enforcement.

Provides:
- Subscription tier definitions with limits
- Stripe checkout session creation (mocked)
- Webhook handler for subscription events
- Tier enforcement for API usage
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import (
    Subscription,
    SubscriptionTier,
    SubscriptionStatus,
    User,
    ApiKey,
    UsageEvent,
    DailyUsageSummary,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_mock")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_mock")
STRIPE_API_VERSION = "2024-06-20"

# ---------------------------------------------------------------------------
# Tier Definitions
# ---------------------------------------------------------------------------

class TierLimits(BaseModel):
    """Limits for a subscription tier."""
    tier: SubscriptionTier
    name: str
    monthly_requests: int
    rate_limit_rpm: int  # requests per minute
    max_concurrent: int
    price_monthly: float  # USD
    price_yearly: float  # USD
    features: list[str]


TIER_LIMITS: dict[SubscriptionTier, TierLimits] = {
    SubscriptionTier.FREE: TierLimits(
        tier=SubscriptionTier.FREE,
        name="Free",
        monthly_requests=100,
        rate_limit_rpm=5,
        max_concurrent=1,
        price_monthly=0,
        price_yearly=0,
        features=["Basic scraping", "Markdown output", "Community support"],
    ),
    SubscriptionTier.STARTER: TierLimits(
        tier=SubscriptionTier.STARTER,
        name="Starter",
        monthly_requests=10_000,
        rate_limit_rpm=30,
        max_concurrent=3,
        price_monthly=29,
        price_yearly=290,
        features=["Everything in Free", "Anti-bot bypass", "Priority support", "API keys"],
    ),
    SubscriptionTier.PRO: TierLimits(
        tier=SubscriptionTier.PRO,
        name="Pro",
        monthly_requests=100_000,
        rate_limit_rpm=60,
        max_concurrent=10,
        price_monthly=99,
        price_yearly=990,
        features=["Everything in Starter", "Webhooks", "Custom headers", "Dedicated support"],
    ),
    SubscriptionTier.BUSINESS: TierLimits(
        tier=SubscriptionTier.BUSINESS,
        name="Business",
        monthly_requests=1_000_000,
        rate_limit_rpm=120,
        max_concurrent=50,
        price_monthly=299,
        price_yearly=2990,
        features=["Everything in Pro", "SLA", "Custom integrations", "Account manager"],
    ),
}


def get_tier_limits(tier: SubscriptionTier) -> TierLimits:
    """Get limits for a subscription tier."""
    return TIER_LIMITS[tier]


# ---------------------------------------------------------------------------
# Stripe Integration (Mocked)
# ---------------------------------------------------------------------------

class CheckoutSession(BaseModel):
    """Mock Stripe checkout session."""
    session_id: str
    url: str
    tier: SubscriptionTier
    customer_email: str
    success_url: str
    cancel_url: str


class WebhookEvent(BaseModel):
    """Mock Stripe webhook event."""
    event_type: str
    data: dict


async def create_checkout_session(
    user: User,
    tier: SubscriptionTier,
    success_url: str = "https://x1.baas.microstorm.biz/success",
    cancel_url: str = "https://x1.baas.microstorm.biz/pricing",
) -> CheckoutSession:
    """
    Create a Stripe checkout session (mocked).
    
    In production, this would call stripe.checkout.Session.create().
    For now, returns a mock session with a fake URL.
    """
    if tier == SubscriptionTier.FREE:
        raise ValueError("Cannot create checkout for free tier")
    
    # Mock session ID
    session_id = f"cs_mock_{user.id}_{tier.value}_{int(datetime.now(timezone.utc).timestamp())}"
    
    return CheckoutSession(
        session_id=session_id,
        url=f"https://checkout.stripe.com/mock/{session_id}",
        tier=tier,
        customer_email=user.email,
        success_url=success_url,
        cancel_url=cancel_url,
    )


async def handle_webhook(event: WebhookEvent, db: AsyncSession) -> dict:
    """
    Handle Stripe webhook events (mocked).
    
    In production, this would verify the webhook signature and process real events.
    For now, simulates the expected behavior.
    """
    event_type = event.event_type
    
    if event_type == "checkout.session.completed":
        return await _handle_checkout_completed(event.data, db)
    elif event_type == "customer.subscription.updated":
        return await _handle_subscription_updated(event.data, db)
    elif event_type == "customer.subscription.deleted":
        return await _handle_subscription_deleted(event.data, db)
    elif event_type == "invoice.payment_failed":
        return await _handle_payment_failed(event.data, db)
    else:
        return {"status": "ignored", "event_type": event_type}


async def _handle_checkout_completed(data: dict, db: AsyncSession) -> dict:
    """Handle successful checkout."""
    customer_email = data.get("customer_email")
    tier_str = data.get("metadata", {}).get("tier", "starter")
    
    try:
        tier = SubscriptionTier(tier_str)
    except ValueError:
        return {"status": "error", "message": f"Invalid tier: {tier_str}"}
    
    # Find user by email
    result = await db.execute(select(User).where(User.email == customer_email))
    user = result.scalar_one_or_none()
    
    if not user:
        return {"status": "error", "message": f"User not found: {customer_email}"}
    
    # Update or create subscription
    result = await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    subscription = result.scalar_one_or_none()
    
    if subscription:
        subscription.tier = tier
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.stripe_customer_id = data.get("customer")
        subscription.stripe_subscription_id = data.get("subscription")
        subscription.current_period_start = datetime.now(timezone.utc)
        subscription.current_period_end = datetime.now(timezone.utc).replace(
            month=datetime.now(timezone.utc).month % 12 + 1
        )
    else:
        subscription = Subscription(
            user_id=user.id,
            tier=tier,
            status=SubscriptionStatus.ACTIVE,
            stripe_customer_id=data.get("customer"),
            stripe_subscription_id=data.get("subscription"),
            current_period_start=datetime.now(timezone.utc),
            current_period_end=datetime.now(timezone.utc).replace(
                month=datetime.now(timezone.utc).month % 12 + 1
            ),
        )
        db.add(subscription)
    
    await db.flush()
    
    return {
        "status": "success",
        "message": f"Subscription upgraded to {tier.value}",
        "user_id": user.id,
        "tier": tier.value,
    }


async def _handle_subscription_updated(data: dict, db: AsyncSession) -> dict:
    """Handle subscription update."""
    subscription_id = data.get("id")
    
    result = await db.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == subscription_id)
    )
    subscription = result.scalar_one_or_none()
    
    if not subscription:
        return {"status": "error", "message": f"Subscription not found: {subscription_id}"}
    
    # Update status based on Stripe status
    stripe_status = data.get("status")
    status_map = {
        "active": SubscriptionStatus.ACTIVE,
        "past_due": SubscriptionStatus.PAST_DUE,
        "canceled": SubscriptionStatus.CANCELED,
        "trialing": SubscriptionStatus.TRIALING,
        "incomplete": SubscriptionStatus.INCOMPLETE,
    }
    
    subscription.status = status_map.get(stripe_status, SubscriptionStatus.ACTIVE)
    await db.flush()
    
    return {
        "status": "success",
        "message": f"Subscription updated to {subscription.status.value}",
        "subscription_id": subscription_id,
    }


async def _handle_subscription_deleted(data: dict, db: AsyncSession) -> dict:
    """Handle subscription cancellation."""
    subscription_id = data.get("id")
    
    result = await db.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == subscription_id)
    )
    subscription = result.scalar_one_or_none()
    
    if not subscription:
        return {"status": "error", "message": f"Subscription not found: {subscription_id}"}
    
    subscription.status = SubscriptionStatus.CANCELED
    subscription.tier = SubscriptionTier.FREE
    await db.flush()
    
    return {
        "status": "success",
        "message": "Subscription canceled, downgraded to free",
        "subscription_id": subscription_id,
    }


async def _handle_payment_failed(data: dict, db: AsyncSession) -> dict:
    """Handle failed payment."""
    subscription_id = data.get("subscription")
    
    result = await db.execute(
        select(Subscription).where(Subscription.stripe_subscription_id == subscription_id)
    )
    subscription = result.scalar_one_or_none()
    
    if not subscription:
        return {"status": "error", "message": f"Subscription not found: {subscription_id}"}
    
    subscription.status = SubscriptionStatus.PAST_DUE
    await db.flush()
    
    return {
        "status": "success",
        "message": "Payment failed, subscription marked as past_due",
        "subscription_id": subscription_id,
    }


# ---------------------------------------------------------------------------
# Tier Enforcement
# ---------------------------------------------------------------------------

class TierCheckResult(BaseModel):
    """Result of a tier enforcement check."""
    allowed: bool
    tier: SubscriptionTier
    limits: TierLimits
    current_usage: int
    remaining: int
    reason: Optional[str] = None


async def check_tier_limits(
    user: User,
    db: AsyncSession,
) -> TierCheckResult:
    """
    Check if a user is within their tier limits.
    
    Returns whether the request is allowed and current usage stats.
    """
    # Get user's subscription
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    subscription = result.scalar_one_or_none()
    
    tier = subscription.tier if subscription else SubscriptionTier.FREE
    limits = get_tier_limits(tier)
    
    # Check if subscription is active
    if subscription and subscription.status not in (
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.TRIALING,
    ):
        return TierCheckResult(
            allowed=False,
            tier=tier,
            limits=limits,
            current_usage=0,
            remaining=0,
            reason=f"Subscription is {subscription.status.value}",
        )
    
    # Get current month's usage
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    from sqlalchemy import func as sqlfunc
    
    usage_result = await db.execute(
        select(sqlfunc.count(UsageEvent.id)).where(
            UsageEvent.user_id == user.id,
            UsageEvent.created_at >= month_start,
        )
    )
    current_usage = usage_result.scalar() or 0
    
    remaining = max(0, limits.monthly_requests - current_usage)
    
    if current_usage >= limits.monthly_requests:
        return TierCheckResult(
            allowed=False,
            tier=tier,
            limits=limits,
            current_usage=current_usage,
            remaining=0,
            reason=f"Monthly limit reached ({limits.monthly_requests})",
        )
    
    return TierCheckResult(
        allowed=True,
        tier=tier,
        limits=limits,
        current_usage=current_usage,
        remaining=remaining,
    )
