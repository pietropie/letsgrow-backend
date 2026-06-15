"""
Endpoints administrativos -- clientes & assinaturas.

Protegidos pelo mesmo X-Admin-Token de app/api/v1/admin.py.
GET /admin/plans foi movido para admin_plans.py (suporta edicao em runtime).
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.models.grow import Grow
from app.models.knowledge import AIConversation
from app.models.plant import Plant
from app.models.user import User

router = APIRouter()

PLAN_CHOICES = ("free", "jardineiro", "cultivador", "grower_pro", "grower", "pro")


class UserListItem(BaseModel):
    id: uuid.UUID
    email: str
    username: str
    full_name: str | None
    plan: str
    plan_expires_at: datetime | None
    is_active: bool
    is_verified: bool
    is_dev_mode: bool
    oauth_provider: str | None
    ai_queries_this_month: int
    created_at: datetime

    model_config = {"from_attributes": True}


class UserListResponse(BaseModel):
    items: list[UserListItem]
    total: int
    limit: int
    offset: int


class UserDetail(UserListItem):
    grows_count: int
    plants_count: int
    conversations_count: int
    ai_queries_reset_at: datetime


class UserUpdateIn(BaseModel):
    plan: str | None = Field(default=None, description="free | jardineiro | cultivador | grower_pro")
    plan_expires_at: datetime | None = Field(
        default=None,
        description="Use null para manter; envie uma data para definir"
    )
    clear_plan_expires_at: bool = Field(
        default=False,
        description="Se true, zera plan_expires_at (plano sem validade)"
    )
    is_active: bool | None = None


@router.get("/users", response_model=UserListResponse)
async def list_users(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
    search: str | None = Query(default=None, description="Filtra por e-mail, username ou nome"),
    plan: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(User)
    count_stmt = select(func.count()).select_from(User)

    if search:
        needle = f"%{search.strip().lower()}%"
        cond = (
            func.lower(User.email).like(needle)
            | func.lower(User.username).like(needle)
            | func.lower(func.coalesce(User.full_name, "")).like(needle)
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    if plan:
        stmt = stmt.where(User.plan == plan.strip().lower())
        count_stmt = count_stmt.where(User.plan == plan.strip().lower())

    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
        count_stmt = count_stmt.where(User.is_active == is_active)

    total = (await db.execute(count_stmt)).scalar_one()
    stmt = stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
    users = (await db.execute(stmt)).scalars().all()

    return UserListResponse(
        items=[UserListItem.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/users/{user_id}", response_model=UserDetail)
async def get_user_detail(
    user_id: uuid.UUID,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario nao encontrado")

    grows_count = (
        await db.execute(select(func.count()).select_from(Grow).where(Grow.user_id == user_id))
    ).scalar_one()
    plants_count = (
        await db.execute(select(func.count()).select_from(Plant).where(Plant.user_id == user_id))
    ).scalar_one()
    conversations_count = (
        await db.execute(
            select(func.count()).select_from(AIConversation).where(AIConversation.user_id == user_id)
        )
    ).scalar_one()

    data = UserListItem.model_validate(user).model_dump()
    return UserDetail(
        **data,
        grows_count=grows_count,
        plants_count=plants_count,
        conversations_count=conversations_count,
        ai_queries_reset_at=user.ai_queries_reset_at,
    )


@router.patch("/users/{user_id}", response_model=UserDetail)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdateIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario nao encontrado")

    if body.plan is not None:
        plan = body.plan.strip().lower()
        if plan not in PLAN_CHOICES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"plan invalido -- use um de: {', '.join(PLAN_CHOICES)}",
            )
        user.plan = plan

    if body.clear_plan_expires_at:
        user.plan_expires_at = None
    elif body.plan_expires_at is not None:
        user.plan_expires_at = body.plan_expires_at

    if body.is_active is not None:
        user.is_active = body.is_active

    await db.commit()
    await db.refresh(user)

    return await get_user_detail(user_id, None, db)  # type: ignore[arg-type]


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Remove permanentemente o usuario e todos os seus dados."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario nao encontrado")

    await db.delete(user)
    await db.commit()
