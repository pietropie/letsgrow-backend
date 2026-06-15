"""
Feature Flags — endpoints público e admin.

Público (JWT normal):
  GET /feature-flags            — retorna {key, enabled} de todas as flags

Admin (X-Admin-Token):
  GET /admin/feature-flags      — lista completa com metadados
  PATCH /admin/feature-flags/{key} — atualiza enabled, name e/ou description
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.models.feature_flag import FeatureFlag
from app.models.user import User
from app.services.auth import get_current_user

# Dois routers separados para permitir prefixos distintos em router.py
public_router = APIRouter()
admin_router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class FeatureFlagPublic(BaseModel):
    """Resposta para o app mobile — apenas o essencial."""
    key: str
    enabled: bool

    model_config = {"from_attributes": True}


class FeatureFlagDetail(BaseModel):
    """Resposta completa para o painel admin."""
    id: uuid.UUID
    key: str
    name: str
    description: str | None
    enabled: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FeatureFlagUpdateIn(BaseModel):
    enabled: bool | None = Field(default=None, description="Liga/desliga a flag")
    name: str | None = Field(default=None, max_length=200, description="Nome legível")
    description: str | None = Field(default=None, description="O que a flag controla")


# ---------------------------------------------------------------------------
# Endpoint público — GET /feature-flags
# ---------------------------------------------------------------------------

@public_router.get(
    "/feature-flags",
    response_model=list[FeatureFlagPublic],
    summary="Lista feature flags ativas/inativas (app mobile)",
)
async def list_feature_flags_public(
    _current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Retorna todas as feature flags com seu estado atual.

    O app mobile usa esta lista para decidir quais telas/features exibir.
    Requer autenticação JWT normal (qualquer usuário ativo).
    """
    result = await db.execute(select(FeatureFlag).order_by(FeatureFlag.key))
    flags = result.scalars().all()
    return [FeatureFlagPublic.model_validate(f) for f in flags]


# ---------------------------------------------------------------------------
# Endpoints admin — /admin/feature-flags
# ---------------------------------------------------------------------------

@admin_router.get(
    "/feature-flags",
    response_model=list[FeatureFlagDetail],
    summary="Lista completa de feature flags (admin)",
)
async def list_feature_flags_admin(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Lista todas as flags com todos os metadados."""
    result = await db.execute(select(FeatureFlag).order_by(FeatureFlag.key))
    flags = result.scalars().all()
    return [FeatureFlagDetail.model_validate(f) for f in flags]


@admin_router.patch(
    "/feature-flags/{key}",
    response_model=FeatureFlagDetail,
    summary="Atualiza uma feature flag (admin)",
)
async def update_feature_flag(
    key: str,
    body: FeatureFlagUpdateIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Atualiza parcialmente uma feature flag identificada pela sua chave.

    Todos os campos do body são opcionais — envia apenas o que deseja alterar.
    """
    result = await db.execute(select(FeatureFlag).where(FeatureFlag.key == key))
    flag = result.scalar_one_or_none()
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Feature flag '{key}' não encontrada",
        )

    if body.enabled is not None:
        flag.enabled = body.enabled
    if body.name is not None:
        flag.name = body.name.strip()
    if body.description is not None:
        flag.description = body.description.strip() or None

    await db.commit()
    await db.refresh(flag)
    return FeatureFlagDetail.model_validate(flag)
