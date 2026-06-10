import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel, EmailStr
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
    RegisterResponse,
    ResendVerificationRequest,
    TokenResponse,
    UserResponse,
    VerifyEmailRequest,
)
from app.services.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.services.email import (
    send_password_reset_email,
    send_verification_email,
    send_welcome_email,
)
from app.services.redis_client import (
    create_email_verification_otp,
    create_otp,
    delete_email_verification_otp,
    delete_otp,
    verify_email_verification_otp,
    verify_otp,
)

router = APIRouter()


# ─── Schemas locais ───────────────────────────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    code: str
    new_password: str


# ─── Cadastro ─────────────────────────────────────────────────────────────────

@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """
    Cria a conta com is_verified=False e envia OTP por email.
    Retorna apenas o email — tokens só são emitidos após verificação.
    """
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
        is_verified=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    code = await create_email_verification_otp(str(body.email))
    try:
        await send_verification_email(
            to_email=str(body.email),
            name=body.full_name or body.username,
            code=code,
        )
    except Exception:
        pass  # não bloqueia cadastro por falha de email

    return RegisterResponse(email=str(body.email))


# ─── Verificação de email ─────────────────────────────────────────────────────

@router.post("/verify-email", response_model=TokenResponse)
async def verify_email(body: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    """
    Valida o OTP, marca is_verified=True e emite tokens de acesso.
    Também envia email de boas-vindas.
    """
    valid = await verify_email_verification_otp(str(body.email), body.code)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código inválido ou expirado",
        )

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código inválido ou expirado",
        )

    user.is_verified = True
    await db.commit()
    await delete_email_verification_otp(str(body.email))

    # Boas-vindas (não bloqueia em caso de falha)
    try:
        await send_welcome_email(
            to_email=str(body.email),
            name=user.full_name or user.username,
        )
    except Exception:
        pass

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/resend-verification", status_code=status.HTTP_204_NO_CONTENT)
async def resend_verification(body: ResendVerificationRequest, db: AsyncSession = Depends(get_db)):
    """Reenvia o OTP de verificação. Responde sempre 204."""
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or user.is_verified or not user.is_active:
        return  # silencioso

    code = await create_email_verification_otp(str(body.email))
    try:
        await send_verification_email(
            to_email=str(body.email),
            name=user.full_name or user.username,
            code=code,
        )
    except Exception:
        pass


# ─── Login ────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa")

    if not user.is_verified:
        # Reenvia OTP automaticamente e informa o cliente
        code = await create_email_verification_otp(str(user.email))
        try:
            await send_verification_email(
                to_email=str(user.email),
                name=user.full_name or user.username,
                code=code,
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_not_verified",
        )

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


# ─── Refresh ──────────────────────────────────────────────────────────────────

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


# ─── Google OAuth ─────────────────────────────────────────────────────────────

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
            is_verified=True,  # Google já verificou o email
        )
        db.add(user)

    await db.commit()
    await db.refresh(user)

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa")

    # Garante que contas Google existentes também ficam verificadas
    if not user.is_verified:
        user.is_verified = True
        await db.commit()

    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
    )


# ─── Recuperação de senha ─────────────────────────────────────────────────────

@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        return  # resposta idêntica — evita enumeração

    code = await create_otp(str(body.email))
    try:
        await send_password_reset_email(to_email=str(body.email), code=code)
    except Exception:
        pass


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A senha deve ter ao menos 8 caracteres",
        )

    valid = await verify_otp(str(body.email), body.code)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código inválido ou expirado",
        )

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Código inválido ou expirado",
        )

    user.hashed_password = hash_password(body.new_password)
    await db.commit()
    await delete_otp(str(body.email))
