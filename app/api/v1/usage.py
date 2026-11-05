"""GET /usage — consultas e créditos do mês para o mostrador do Início (UX item 16)."""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.services.auth import get_current_user
from app.services.usage import get_usage

router = APIRouter()


class UsagePeriod(BaseModel):
    start: datetime
    renews_at: datetime


class QueryUsage(BaseModel):
    used: int
    total: int
    remaining: int


class CreditExpiry(BaseModel):
    at: datetime
    credits: int


class CreditUsage(BaseModel):
    plan_total: int
    plan_used: int
    extra_balance: int
    used_in_period: int
    remaining: int
    next_expiry: CreditExpiry | None


class UsageOut(BaseModel):
    mode: Literal["queries", "credits", "both", "unlimited"]
    period: UsagePeriod
    queries: QueryUsage | None
    credits: CreditUsage | None
    near_limit: bool
    exhausted: bool
    warning_threshold: float


@router.get("", response_model=UsageOut)
async def read_usage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_usage(db, current_user)
