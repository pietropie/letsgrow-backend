"""Equipamentos e insumos no app e no web (UX item 26, PLAYSTORE.md §2.3).

Tudo pede login e a confirmação de 18+ (a área não aparece antes dela) e respeita
os interruptores do Admin. O Bobby não usa estas rotas.
"""
import uuid
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.shop import ShopCategory
from app.models.user import User
from app.services import shop
from app.services.app_settings import get_app_settings
from app.services.auth import get_current_user

router = APIRouter()


def adult_user(current_user: User = Depends(get_current_user)) -> User:
    if getattr(current_user, "age_confirmed_at", None) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "age_confirmation_required", "message": "Confirme que você tem 18 anos ou mais."},
        )
    return current_user


def _error(exc: shop.ShopError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


class ShopSettingsPublic(BaseModel):
    enabled: bool
    page: bool
    environment_block: bool
    phase_block: bool
    account_entry: bool
    notice: str


class CategoryOut(BaseModel):
    key: str
    name: str


class ProductCategory(BaseModel):
    key: str
    name: str


class ProductStore(BaseModel):
    name: str
    domain: str


class ProductOut(BaseModel):
    id: uuid.UUID
    category: ProductCategory
    name: str
    specs: list[str]
    store: ProductStore
    url: str
    label: Literal["partner", "sponsored", "none"]
    label_text: str
    reason: str | None
    price_from: float | None
    price_checked_at: date | None


class RecommendationOut(BaseModel):
    trigger: str
    reason: str
    grow_id: uuid.UUID | None
    plant_id: uuid.UUID | None
    product: ProductOut


class HaveIn(BaseModel):
    trigger: str
    grow_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None

    model_config = {"extra": "forbid"}


class HaveOut(BaseModel):
    trigger: str
    grow_id: uuid.UUID | None
    plant_id: uuid.UUID | None
    equipment: dict


class ClickOut(BaseModel):
    url: str


@router.get("/settings", response_model=ShopSettingsPublic)
async def shop_settings(_: User = Depends(adult_user), db: AsyncSession = Depends(get_db)):
    """O app e o web leem antes de mostrar qualquer bloco ou entrada da área."""
    row = await get_app_settings(db)
    return ShopSettingsPublic(
        enabled=row.shop_enabled, page=row.shop_enabled and row.shop_page_enabled,
        environment_block=row.shop_enabled and row.shop_grow_block_enabled,
        phase_block=row.shop_enabled and row.shop_plant_block_enabled,
        account_entry=row.shop_enabled and row.shop_account_entry_enabled,
        notice=shop.FIXED_NOTICE,
    )


@router.get("/categories", response_model=list[CategoryOut])
async def shop_categories(_: User = Depends(adult_user), db: AsyncSession = Depends(get_db)):
    row = await get_app_settings(db)
    if not (row.shop_enabled and row.shop_page_enabled):
        return []
    categories = (await db.execute(
        select(ShopCategory).where(ShopCategory.is_active.is_(True)).order_by(ShopCategory.sort_order)
    )).scalars().all()
    return [CategoryOut(key=c.key, name=c.name) for c in categories]


@router.get("/products", response_model=list[ProductOut])
async def shop_products(
    category: str | None = Query(default=None),
    _: User = Depends(adult_user),
    db: AsyncSession = Depends(get_db),
):
    row = await get_app_settings(db)
    if not (row.shop_enabled and row.shop_page_enabled):
        return []
    return await shop.visible_products(db, row, category)


@router.get("/recommendations", response_model=list[RecommendationOut])
async def shop_recommendations(
    grow_id: uuid.UUID | None = Query(default=None),
    plant_id: uuid.UUID | None = Query(default=None),
    current_user: User = Depends(adult_user),
    db: AsyncSession = Depends(get_db),
):
    """Sem parâmetro: a página inteira. Com grow_id ou plant_id: o bloco daquele contexto."""
    if grow_id and plant_id:
        raise HTTPException(status_code=422, detail="Envie grow_id ou plant_id, não os dois.")
    try:
        return await shop.recommendations(db, current_user.id, await get_app_settings(db), grow_id, plant_id)
    except shop.ShopError as exc:
        raise _error(exc)


@router.post("/recommendations/have", response_model=HaveOut)
async def shop_have_it(body: HaveIn, current_user: User = Depends(adult_user), db: AsyncSession = Depends(get_db)):
    """"Já tenho": grava o equipamento (quando o gatilho diz qual) e dispensa a indicação."""
    try:
        return await shop.have_it(db, current_user.id, body.trigger, body.grow_id, body.plant_id, body.product_id)
    except shop.ShopError as exc:
        raise _error(exc)


@router.post("/products/{product_id}/click", response_model=ClickOut)
async def shop_click(product_id: uuid.UUID, _: User = Depends(adult_user), db: AsyncSession = Depends(get_db)):
    """Conta o toque em "Ver na loja" (só por produto e dia, sem usuário) e devolve o link para abrir fora do app."""
    try:
        return ClickOut(url=await shop.count_click(db, product_id))
    except shop.ShopError as exc:
        raise _error(exc)
