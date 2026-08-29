"""
Database models — 5 tables for BaaS revenue infrastructure.

Tables:
  1. users          — email, password hash, email verified
  2. api_keys       — key hash, prefix, user_id, scopes, limits
  3. usage_events   — per-scrape metering
  4. subscriptions  — Stripe subscription state
  5. daily_usage_summaries — aggregated daily stats
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, date, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SubscriptionTier(str, enum.Enum):
    FREE = "free"
    STARTER = "starter"
    PRO = "pro"
    BUSINESS = "business"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    TRIALING = "trialing"
    INCOMPLETE = "incomplete"


class AuthMethod(str, enum.Enum):
    PASSWORD = "password"
    GITHUB = "github"
    GOOGLE = "google"


# ---------------------------------------------------------------------------
# 1. Users
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    # id, created_at, updated_at inherited from Base
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    auth_method: Mapped[AuthMethod] = mapped_column(
        Enum(AuthMethod, values_callable=lambda x: [e.value for e in x]), default=AuthMethod.PASSWORD, nullable=False,
    )
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # OAuth IDs (nullable — only set for OAuth users)
    github_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)
    google_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True)

    # Relationships
    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    subscription: Mapped[Optional["Subscription"]] = relationship(back_populates="user", uselist=False)

    def __repr__(self) -> str:
        return f"<User {self.email}>"


# ---------------------------------------------------------------------------
# 2. API Keys
# ---------------------------------------------------------------------------

class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_key_hash", "key_hash"),
        Index("ix_api_keys_user_id", "user_id"),
    )

    # id, created_at, updated_at inherited from Base
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)  # "baas_live_" + first 8 chars
    name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Scopes + limits
    scopes: Mapped[str] = mapped_column(Text, default="scrape:read", nullable=False)  # comma-separated
    rate_limit: Mapped[int] = mapped_column(Integer, default=30, nullable=False)  # requests per minute
    monthly_quota: Mapped[int] = mapped_column(Integer, default=10000, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # State
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="api_keys")
    usage_events: Mapped[list["UsageEvent"]] = relationship(back_populates="api_key")

    @property
    def is_valid(self) -> bool:
        """Check if key is active, not revoked, not expired."""
        if not self.is_active or self.revoked_at:
            return False
        if self.expires_at and self.expires_at < datetime.now(timezone.utc):
            return False
        return True

    def __repr__(self) -> str:
        return f"<ApiKey {self.key_prefix}... user={self.user_id}>"


# ---------------------------------------------------------------------------
# 3. Usage Events (per-scrape metering)
# ---------------------------------------------------------------------------

class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_key_id", "key_id"),
        Index("ix_usage_events_created_at", "created_at"),
        Index("ix_usage_events_key_date", "key_id", "created_at"),
    )

    # id = event ID, created_at = scrape timestamp
    # updated_at unused but inherited
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Request details
    endpoint: Mapped[str] = mapped_column(String(50), nullable=False)  # "/v1/scrape"
    url: Mapped[str] = mapped_column(Text, nullable=False)  # scraped URL
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)  # extracted domain

    # Result
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)  # HTTP status
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    response_time_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Auth method used
    auth_method: Mapped[str] = mapped_column(String(20), nullable=False, default="api_key")  # "api_key" or "x402"

    # Relationships
    api_key: Mapped["ApiKey"] = relationship(back_populates="usage_events")

    def __repr__(self) -> str:
        return f"<UsageEvent {self.id} key={self.key_id} {self.domain} {self.status_code}>"


# ---------------------------------------------------------------------------
# 4. Subscriptions (Stripe)
# ---------------------------------------------------------------------------

class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_user_id", "user_id"),
        UniqueConstraint("stripe_subscription_id", name="uq_subscriptions_stripe_sub_id"),
    )

    # id, created_at, updated_at inherited from Base
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)

    # Stripe IDs
    stripe_customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stripe_price_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # State
    tier: Mapped[SubscriptionTier] = mapped_column(
        Enum(SubscriptionTier, values_callable=lambda x: [e.value for e in x]), default=SubscriptionTier.FREE, nullable=False,
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus, values_callable=lambda x: [e.value for e in x]), default=SubscriptionStatus.ACTIVE, nullable=False,
    )

    # Billing period
    current_period_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="subscription")

    def __repr__(self) -> str:
        return f"<Subscription user={self.user_id} tier={self.tier.value} status={self.status.value}>"


# ---------------------------------------------------------------------------
# 5. Daily Usage Summaries (aggregated)
# ---------------------------------------------------------------------------

class DailyUsageSummary(Base):
    __tablename__ = "daily_usage_summaries"
    __table_args__ = (
        UniqueConstraint("key_id", "summary_date", name="uq_daily_usage_key_date"),
        Index("ix_daily_usage_summary_date", "summary_date"),
    )

    # id, created_at, updated_at inherited from Base
    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    summary_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Aggregated metrics
    total_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_response_time_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Top domains (JSON string: [{"domain": "example.com", "count": 42}, ...])
    top_domains: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<DailyUsageSummary key={self.key_id} date={self.summary_date} total={self.total_requests}>"


# ---------------------------------------------------------------------------
# 6. Crawl Jobs (Phase 2 Work Stream 2)
# ---------------------------------------------------------------------------

class CrawlJobStatus:
    """String constants for crawl job status (stored as VARCHAR, not PG enum)."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"
    __table_args__ = (
        Index("ix_crawl_jobs_user_id", "user_id"),
        Index("ix_crawl_jobs_status", "status"),
        Index("ix_crawl_jobs_created_at", "created_at"),
    )

    # NOTE: users.id / api_keys.id are INTEGER autoincrement in this codebase,
    # so user_id / api_key_id are integer FKs (the UUID types in the original
    # plan assumed a UUID user table that this schema does not use). The job id
    # itself is a UUID so the public job_id is an opaque, non-guessable string.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        default=uuid.uuid4, server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    api_key_id: Mapped[Optional[int]] = mapped_column(ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=CrawlJobStatus.PENDING, server_default="pending")
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    start_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    urls: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    options: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    progress: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Webhook callback (Phase 2 Work Stream 3) — notified on completion.
    callback_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    webhook_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    results: Mapped[list["CrawlResult"]] = relationship(
        back_populates="job", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<CrawlJob {self.id} mode={self.mode} status={self.status}>"


class CrawlResult(Base):
    __tablename__ = "crawl_results"
    __table_args__ = (
        Index("ix_crawl_results_job_id", "job_id"),
        Index("ix_crawl_results_url", "url"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        default=uuid.uuid4, server_default=text("gen_random_uuid()"),
    )
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("crawl_jobs.id", ondelete="CASCADE"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    markdown: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Python attribute is `page_metadata` because `metadata` is reserved by
    # the SQLAlchemy Declarative API; the DB column is still named `metadata`.
    page_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"),
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    crawled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), server_default=func.now(),
    )

    job: Mapped["CrawlJob"] = relationship(back_populates="results")

    def __repr__(self) -> str:
        return f"<CrawlResult {self.id} job={self.job_id} status={self.status}>"


# ---------------------------------------------------------------------------
# 7. Webhook Deliveries (Phase 2 Work Stream 3)
# ---------------------------------------------------------------------------

class WebhookDeliveryStatus:
    """String constants for webhook delivery status (VARCHAR, not PG enum)."""
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_user_id", "user_id"),
        Index("ix_webhook_deliveries_status", "status"),
        Index("ix_webhook_deliveries_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
        default=uuid.uuid4, server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    event: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    callback_url: Mapped[str] = mapped_column(Text, nullable=False)

    # Delivery state.
    # `max_attempts` counts retries AFTER the initial attempt, so a delivery is
    # tried once immediately, then up to `max_attempts` more times with
    # exponential backoff (10s -> 30s -> 60s). Default 3 -> 4 total attempts.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=WebhookDeliveryStatus.PENDING, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Last attempt result.
    response_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<WebhookDelivery {self.id} user={self.user_id} {self.event} {self.status}>"

