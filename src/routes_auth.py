"""
Auth routes — registration, login, email verification, profile.

Endpoints:
  POST /v1/auth/register     — Create account
  POST /v1/auth/login        — Get JWT tokens
  POST /v1/auth/refresh      — Refresh access token
  GET  /v1/auth/me            — Current user profile
  POST /v1/auth/verify-email  — Verify email with token
  POST /v1/auth/resend-verification — Resend verification email
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import (
    UserRegister,
    UserLogin,
    UserResponse,
    TokenResponse,
    TokenPair,
    create_token_pair,
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
    create_verification_token,
    decode_verification_token,
    get_current_user,
)
from database import get_db
from models import User, AuthMethod, Subscription, SubscriptionTier, SubscriptionStatus

router = APIRouter(prefix="/v1/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: UserRegister, db: AsyncSession = Depends(get_db)):
    """
    Register a new user with email + password.
    Returns JWT tokens + user profile.
    """
    # Check if email already exists
    result = await db.execute(select(User).where(User.email == body.email.lower()))
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Create user
    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        auth_method=AuthMethod.PASSWORD,
        email_verified=False,
        is_active=True,
    )
    db.add(user)
    await db.flush()  # Get user.id

    # Create free subscription
    subscription = Subscription(
        user_id=user.id,
        tier=SubscriptionTier.FREE,
        status=SubscriptionStatus.ACTIVE,
    )
    db.add(subscription)
    await db.flush()

    # Generate tokens
    tokens = create_token_pair(user.id)

    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user=UserResponse.model_validate(user),
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

@router.post("/login", response_model=TokenResponse)
async def login(body: UserLogin, db: AsyncSession = Depends(get_db)):
    """
    Login with email + password.
    Returns JWT tokens + user profile.
    """
    result = await db.execute(select(User).where(User.email == body.email.lower()))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    tokens = create_token_pair(user.id)

    return TokenResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
        user=UserResponse.model_validate(user),
    )


# ---------------------------------------------------------------------------
# Token Refresh
# ---------------------------------------------------------------------------

@router.post("/refresh", response_model=TokenPair)
async def refresh_token(refresh_token: str, db: AsyncSession = Depends(get_db)):
    """
    Exchange a refresh token for a new access + refresh token pair.
    """
    payload = decode_token(refresh_token)
    if payload.type != "refresh":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Expected refresh token",
        )

    user_id = int(payload.sub)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    return create_token_pair(user.id)


# ---------------------------------------------------------------------------
# Current User Profile
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    """Return the current user's profile."""
    return UserResponse.model_validate(user)


# ---------------------------------------------------------------------------
# Email Verification
# ---------------------------------------------------------------------------

@router.post("/verify-email")
async def verify_email(token: str, db: AsyncSession = Depends(get_db)):
    """
    Verify a user's email address using the verification token.
    """
    user_id = decode_verification_token(token)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.email_verified:
        return {"message": "Email already verified"}

    user.email_verified = True
    await db.flush()

    return {"message": "Email verified successfully"}


@router.post("/resend-verification")
async def resend_verification(user: User = Depends(get_current_user)):
    """
    Resend email verification token.
    TODO: Send actual email via SMTP.
    """
    if user.email_verified:
        return {"message": "Email already verified"}

    token = create_verification_token(user.id)

    # TODO: Send email with verification link
    # In production, send email; in development, return token for testing
    IS_PRODUCTION = os.getenv("BAAAS_ENV") == "production"
    
    response = {"message": "Verification email sent"}
    if not IS_PRODUCTION:
        response["dev_token"] = token  # Only in development
    
    return response
