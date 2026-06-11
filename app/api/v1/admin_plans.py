"""
Endpoints admin para gerenciamento de planos (plan_configs).

GET  /admin/plans            -- lista todos os planos com config atual
PATCH /admin/plans/{key}     -- atualiza um plano (limites, preco, badge, etc)
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.services.plan_config_service import get_all_plan_configs, update_plan_config

router = APIRouter()


class PlanConfigResponse(BaseModel):
    plan_key: str
    label: str
    price_brl: float
    price_display: str
    period_display: str
    badge_text: str | None
    max_plants: int
    max_grows: int
    max_pots_per_grow: int
    ai_queries_per_month: int | None
    sensors_allowed: bool
    updated_by: str | None


class PlanConfigUpdate(BaseModel):
    label: str | None = None
    price_brl: float | None = Field(default=None, ge=0)
    price_display: str | None = None
    period_display: str | None = None
    badge_text: str | None = None
    max_plants: int | None = Field(default=None, ge=1)
    max_grows: int | None = Field(default=None, ge=1)
    max_pots_per_grow: int | None = Field(default=None, ge=1)
    # None = ilimitado; use 0 para explicitamente remover limite (sera salvo como null)
    ai_queries_per_month: int | None = Field(default=..., ge=1)
    sensors_allowed: bool | None = None
    updated_by: str | None = None

    model_config = {"extra": "forbid"}


@router.get("/plans", response_model=list[PlanConfigResponse])
async def list_plan_configs(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Lista todos os planos com configuracao atual (do banco)."""
    configs = await get_all_plan_configs(db)
    order = ["free", "jardineiro", "cultivador", "grower_pro"]
    sorted_configs = sorted(configs.values(), key=lambda c: order.index(c["plan_key"]) if c["plan_key"] in order else 99)
    return [PlanConfigResponse(**c) for c in sorted_configs]


@router.patch("/plans/{plan_key}", response_model=PlanConfigResponse)
async def update_plan(
    plan_key: str,
    body: PlanConfigUpdate,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Atualiza configuracao de um plano. Apenas os campos enviados sao alterados."""
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    result = await update_plan_config(db, plan_key, updates)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Plano '{plan_key}' nao encontrado.")
    return PlanConfigResponse(**result)
