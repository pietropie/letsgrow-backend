"""
Endpoint publico de configuracao de planos.
Usado pelo app mobile para exibir precos e limites sem autenticacao.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.plan_config_service import get_all_plan_configs

router = APIRouter()


class PublicPlanConfig(BaseModel):
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


@router.get("/config", response_model=list[PublicPlanConfig])
async def get_plans_config(db: AsyncSession = Depends(get_db)):
    """Retorna a configuracao publica de todos os planos (precos e limites)."""
    configs = await get_all_plan_configs(db)
    order = ["free", "jardineiro", "cultivador", "grower_pro"]
    sorted_configs = sorted(configs.values(), key=lambda c: order.index(c["plan_key"]) if c["plan_key"] in order else 99)
    return [PublicPlanConfig(**c) for c in sorted_configs]
