import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    OAuthGoogleRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)

router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(
        select(User).where((User.email == body.email) | (User.username == body.username))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email ou username já em uso")

    user = User(
        email=body.email,
        username=body.username,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa")

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    user_id = decode_token(body.refresh_token, "refresh")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/google", response_model=TokenResponse)
async def google_login(body: OAuthGoogleRequest, db: AsyncSession = Depends(get_db)):
    valid_client_ids = [
        cid for cid in [
            settings.GOOGLE_CLIENT_ID,
            settings.GOOGLE_ANDROID_CLIENT_ID,
            settings.GOOGLE_IOS_CLIENT_ID,
        ] if cid
    ]
    if not valid_client_ids:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Google login não configurado")

    payload = None
    for client_id in valid_client_ids:
        try:
            payload = google_id_token.verify_oauth2_token(
                body.id_token,
                google_requests.Request(),
                client_id,
            )
            break
        except Exception:
            continue

    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token Google inválido")

    google_sub = payload["sub"]
    email = payload.get("email", "")
    full_name = payload.get("name")

    # Find by oauth_id first, then by email
    result = await db.execute(
        select(User).where(User.oauth_provider == "google", User.oauth_id == google_sub)
    )
    user = result.scalar_one_or_none()

    if not user and email:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user:
            user.oauth_provider = "google"
            user.oauth_id = google_sub

    if not user:
        base = re.sub(r"[^a-z0-9]", "", (full_name or email.split("@")[0]).lower())[:20] or "user"
        username = base
        suffix = 1
        while (await db.execute(select(User).where(User.username == username))).scalar_one_or_none():
            username = f"{base}{suffix}"
            suffix += 1

        user = User(
            email=email,
            username=username,
            full_name=full_name,
            hashed_password=None,
            oauth_provider="google",
            oauth_id=google_sub,
            is_verified=True,
        )
        db.add(user)

    await db.commit()
    await db.refresh(user)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa")

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )
