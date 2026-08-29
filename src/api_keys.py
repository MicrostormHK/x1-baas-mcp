"""
API Key management — CRUD, scoping, validation.

Provides:
- Key generation with secure random tokens
- Key hashing (SHA-256) for storage
- Key prefix for identification (baas_live_ + first 8 chars)
- Key validation + usage tracking
- FastAPI dependency for key-based auth
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import ApiKey, User

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY_PREFIX = "baas_live_"
API_KEY_LENGTH = 32  # bytes (64 hex chars)

# ---------------------------------------------------------------------------
# Key Generation
# ---------------------------------------------------------------------------

def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.
    
    Returns:
        (full_key, key_hash, key_prefix)
        - full_key: The raw key to show to the user (only shown once)
        - key_hash: SHA-256 hash for storage
        - key_prefix: First 8 chars for identification
    """
    # Generate random bytes
    random_bytes = secrets.token_bytes(API_KEY_LENGTH)
    
    # Create full key with prefix
    full_key = f"{API_KEY_PREFIX}{random_bytes.hex()}"
    
    # Hash for storage
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    
    # Prefix for identification
    key_prefix = full_key[: len(API_KEY_PREFIX) + 8]
    
    return full_key, key_hash, key_prefix


def hash_api_key(full_key: str) -> str:
    """Hash an API key for storage/comparison."""
    return hashlib.sha256(full_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class ApiKeyCreate(BaseModel):
    name: Optional[str] = Field(None, max_length=100, description="Friendly name for the key")
    scopes: str = Field("scrape:read", description="Comma-separated scopes")
    rate_limit: Optional[int] = Field(None, ge=1, le=1000, description="Custom rate limit (rpm)")
    monthly_quota: Optional[int] = Field(None, ge=1, le=1000000, description="Custom monthly quota")
    expires_days: Optional[int] = Field(None, ge=1, le=365, description="Days until expiration")


class ApiKeyResponse(BaseModel):
    id: int
    name: Optional[str]
    key_prefix: str
    scopes: str
    rate_limit: int
    monthly_quota: int
    usage_count: int
    is_active: bool
    created_at: datetime
    last_used_at: Optional[datetime]
    expires_at: Optional[datetime]
    
    class Config:
        from_attributes = True


class ApiKeyCreated(BaseModel):
    """Response when creating a new key — includes the raw key (shown once)."""
    id: int
    name: Optional[str]
    key: str  # Only shown once!
    key_prefix: str
    scopes: str
    rate_limit: int
    monthly_quota: int
    expires_at: Optional[datetime]
    message: str = "Save this key — it will not be shown again."


# ---------------------------------------------------------------------------
# FastAPI Dependencies
# ---------------------------------------------------------------------------

api_key_security = HTTPBearer(auto_error=False)


async def get_current_user_from_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(api_key_security),
    db: AsyncSession = Depends(get_db),
) -> tuple[User, ApiKey]:
    """
    Extract and validate the current user from API key.
    
    Returns:
        (user, api_key) tuple
    
    Raises 401 if key is missing, invalid, revoked, or expired.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )
    
    full_key = credentials.credentials
    
    # Hash the provided key
    key_hash = hash_api_key(full_key)
    
    # Look up key in database
    result = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash)
    )
    api_key = result.scalar_one_or_none()
    
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    
    # Check if key is valid
    if not api_key.is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key is revoked or expired",
        )
    
    # Get user
    result = await db.execute(
        select(User).where(User.id == api_key.user_id)
    )
    user = result.scalar_one_or_none()
    
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    
    # Update last used timestamp
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    
    return user, api_key


async def require_scope(scope: str):
    """
    Dependency factory — require a specific scope on the API key.
    
    Usage:
        @router.get("/endpoint", dependencies=[Depends(require_scope("scrape:read"))])
    """
    async def _check_scope(
        auth: tuple[User, ApiKey] = Depends(get_current_user_from_api_key),
    ):
        user, api_key = auth
        scopes = api_key.scopes.split(",")
        if scope not in scopes and "*" not in scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope: {scope}",
            )
        return auth
    
    return _check_scope
