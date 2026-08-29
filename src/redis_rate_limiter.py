"""
Redis-backed rate limiting and quota tracking for X1-BaaS-Engine.

Uses sliding window rate limiting and monthly quota enforcement.
Falls back to in-memory limiter when Redis is unavailable.
"""

import redis.asyncio as redis
import os
import time
import logging
from typing import Optional

logger = logging.getLogger("baas-engine")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


class RedisRateLimiter:
    """Redis-backed rate limiter with sliding window and monthly quotas."""

    def __init__(self):
        self.redis: Optional[redis.Redis] = None
        self._connected = False

    async def connect(self) -> bool:
        """Connect to Redis. Returns True if successful, False otherwise."""
        try:
            self.redis = redis.from_url(REDIS_URL, decode_responses=True)
            await self.redis.ping()
            self._connected = True
            logger.info("Redis rate limiter connected to %s", REDIS_URL)
            return True
        except Exception as exc:
            logger.warning("Redis connection failed: %s — falling back to in-memory", exc)
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
            self._connected = False
            logger.info("Redis rate limiter disconnected")

    async def check_rate_limit(self, key: str, limit: int, window: int = 60) -> bool:
        """
        Sliding window rate limit check using Redis sorted sets.

        Args:
            key: Rate limit key (e.g., "rate:192.168.1.1")
            limit: Maximum requests allowed in the window
            window: Window size in seconds (default 60)

        Returns:
            True if request is allowed, False if rate limited
        """
        if not self._connected or not self.redis:
            return True  # Allow if Redis unavailable (fallback will handle)

        try:
            now = time.time()
            window_start = now - window

            # Use pipeline for atomic operations
            pipe = self.redis.pipeline()
            # Remove expired entries
            pipe.zremrangebyscore(key, 0, window_start)
            # Add current request
            pipe.zadd(key, {f"{now}": now})
            # Count requests in window
            pipe.zcard(key)
            # Set expiry on the key
            pipe.expire(key, window + 1)
            results = await pipe.execute()

            count = results[2]
            return count <= limit

        except Exception as exc:
            logger.error("Redis rate limit check failed: %s", exc)
            return True  # Allow on error (fail open)

    async def check_quota(self, user_id: str, monthly_quota: int) -> dict:
        """
        Check and increment monthly quota for a user.

        Args:
            user_id: User identifier (e.g., API key hash or user ID)
            monthly_quota: Maximum requests allowed per month

        Returns:
            dict with keys: allowed (bool), current (int), limit (int), remaining (int)
        """
        if not self._connected or not self.redis:
            return {
                "allowed": True,
                "current": 0,
                "limit": monthly_quota,
                "remaining": monthly_quota,
            }

        try:
            month_key = time.strftime("%Y-%m")
            key = f"quota:{user_id}:{month_key}"

            # Increment counter
            count = await self.redis.incr(key)

            # Set expiry only on first increment (31 days)
            if count == 1:
                await self.redis.expire(key, 60 * 60 * 24 * 31)

            return {
                "allowed": count <= monthly_quota,
                "current": count,
                "limit": monthly_quota,
                "remaining": max(0, monthly_quota - count),
            }

        except Exception as exc:
            logger.error("Redis quota check failed: %s", exc)
            return {
                "allowed": True,
                "current": 0,
                "limit": monthly_quota,
                "remaining": monthly_quota,
            }

    async def get_quota_status(self, user_id: str) -> Optional[dict]:
        """
        Get current quota status without incrementing.
        Returns None if Redis unavailable or key doesn't exist.
        """
        if not self._connected or not self.redis:
            return None

        try:
            month_key = time.strftime("%Y-%m")
            key = f"quota:{user_id}:{month_key}"

            count = await self.redis.get(key)
            if count is None:
                return None

            return {"current": int(count)}

        except Exception as exc:
            logger.error("Redis quota status failed: %s", exc)
            return None

    @property
    def is_connected(self) -> bool:
        return self._connected


# Global instance
redis_rate_limiter = RedisRateLimiter()
