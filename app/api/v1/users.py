"""
Endpoints de perfil do usuário autenticado.

PATCH  /users/me              — editar nome e username
POST   /users/me/change-password — trocar senha
POST   /users/me/avatar/presign  — obter URL pré-assinada para upload de avatar
PATCH  /users/me/avatar          — confirmar upload e salvar URL no perfil
GET    /users/me/plan            — status do plano atual com limites e consumo
"""
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.user import User
from app.schemas.auth import UserResponse
from app.services.auth import get_current_user, hash_password, verify_password
from app.services.storage import BUCKET_AVATARS, presign_upload
from app.services.subscription import get_plan_status

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas locais
# ---------------------------------------------------------------------------

class ProfileUpdateIn(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    username: str | None = Field(default=None, min_length=3, max_length=50)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class AvatarPresignResponse(BaseModel):
    upload_url: str
    object_key: str


class AvatarConfirmIn(BaseModel):
    object_key: str


class PlanStatusResponse(BaseModel):
    plan: str
    effective_plan: str
    is_expired: bool
    plan_expires_at: datetime | None
    # Limites
    max_plants: int
    max_grows: int
    max_pots_per_grow: int
    ai_queries_per_month: int | None       # None = ilimitado (grower/pro)
    # Uso atual
    plants_used: int
    grows_used: int
    ai_queries_used: int
    ai_queries_reset_at: datetime


# ---------------------------------------------------------------------------
# PATCH /users/me
# ---------------------------------------------------------------------------

@router.patch("/me", response_model=UserResponse)
async def update_profile(
    body: ProfileUpdateIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atualiza nome e/ou username do usuário autenticado."""
    if body.username is not None:
        username = body.username.strip().lower()
        if not re.match(r'^[a-z0-9_-]+$', username):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Username deve conter apenas letras, números, _ ou -",
            )
        # Verifica conflito com outro usuário
        conflict = await db.execute(
            select(User).where(User.username == username, User.id != current_user.id)
        )
        if conflict.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username já está em uso",
            )
        current_user.username = username

    if body.full_name is not None:
        current_user.full_name = body.full_name.strip() or None

    await db.commit()
    await db.refresh(current_user)
    return current_user


# ---------------------------------------------------------------------------
# POST /users/me/change-password
# ---------------------------------------------------------------------------

@router.post("/me/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Troca a senha do usuário. Requer a senha atual."""
    if not current_user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Conta OAuth não possui senha local para alterar",
        )
    if not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Senha atual incorreta",
        )
    current_user.hashed_password = hash_password(body.new_password)
    await db.commit()


# ---------------------------------------------------------------------------
# POST /users/me/avatar/presign — etapa 1: obter URL pré-assinada
# ---------------------------------------------------------------------------

@router.post("/me/avatar/presign", response_model=AvatarPresignResponse)
async def presign_avatar_upload(
    current_user: User = Depends(get_current_user),
):
    """
    Retorna uma URL pré-assinada para o mobile fazer PUT direto no MinIO.
    Depois do upload, chamar PATCH /users/me/avatar com o object_key.
    """
    object_key = f"avatars/{current_user.id}/{uuid.uuid4()}.jpg"
    upload_url = presign_upload(BUCKET_AVATARS, object_key, content_type="image/jpeg")
    return AvatarPresignResponse(upload_url=upload_url, object_key=object_key)


# ---------------------------------------------------------------------------
# PATCH /users/me/avatar — etapa 2: confirmar upload e salvar URL
# ---------------------------------------------------------------------------

@router.patch("/me/avatar", response_model=UserResponse)
async def confirm_avatar_upload(
    body: AvatarConfirmIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Confirma que o upload do avatar foi concluído.
    Salva a URL pública do objeto no perfil do usuário.
    O object_key deve ser o retornado por /me/avatar/presign.
    """
    from app.services.storage import get_minio_presign_client, _minio_public_secure
    from app.config import settings as cfg

    # Valida que o object_key pertence ao usuário (evita sobrescrever avatar alheio)
    expected_prefix = f"avatars/{current_user.id}/"
    if not body.object_key.startswith(expected_prefix):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="object_key inválido para este usuário",
        )

    # Monta URL pública permanente (sem expiração) — adequada para avatars
    # que são lidos frequentemente. Em produção, o bucket avatars deve ser
    # público-leitura no MinIO (policy: download).
    endpoint = cfg.MINIO_PUBLIC_ENDPOINT or cfg.MINIO_ENDPOINT
    secure = _minio_public_secure(cfg)
    scheme = "https" if secure else "http"
    public_url = f"{scheme}://{endpoint}/{BUCKET_AVATARS}/{body.object_key}"

    current_user.avatar_url = public_url
    await db.commit()
    await db.refresh(current_user)
    return current_user


# ---------------------------------------------------------------------------
# GET /users/me/plan
# ---------------------------------------------------------------------------

@router.get("/me/plan", response_model=PlanStatusResponse)
async def get_my_plan(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retorna o plano atual do usuário com limites e consumo em tempo real."""
    plants_count = (
        await db.execute(
            select(func.count(Plant.id)).where(
                Plant.user_id == current_user.id,
                Plant.is_active == True,
            )
        )
    ).scalar_one()

    grows_count = (
        await db.execute(
            select(func.count(Grow.id)).where(Grow.user_id == current_user.id)
        )
    ).scalar_one()

    status_data = get_plan_status(current_user, plants_count, grows_count)
    return PlanStatusResponse(**status_data)
