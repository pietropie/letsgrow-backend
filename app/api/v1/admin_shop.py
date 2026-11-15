"""Admin: Equipamentos e insumos (UX item 26c/26d, tela AD1). Todas as rotas pedem X-Admin-Token."""
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.models.shop import SHOP_CATEGORIES, ShopCategory, ShopClickDaily, ShopProduct, ShopStore
from app.services import shop
from app.services.app_settings import get_app_settings

router = APIRouter(dependencies=[Depends(require_admin_token)])


def _error(exc: shop.ShopError) -> HTTPException:
    if exc.field:  # formato padrão do FastAPI: o Admin mostra a mensagem no campo
        return HTTPException(status_code=exc.status, detail=[{"loc": ["body", exc.field], "msg": exc.detail,
                                                              "type": "value_error"}])
    return HTTPException(status_code=exc.status, detail=exc.detail)


# ── Pontos de exibição ─────────────────────────────────────────────────────

class ShopSettingsOut(BaseModel):
    enabled: bool
    page: bool
    environment_block: bool
    phase_block: bool
    account_entry: bool
    bobby: bool = False          # sempre falso: o Bobby nunca indica produto (PLAYSTORE.md §2.3)
    sponsored: bool              # ligar muda o "Contém anúncios" da Play para Sim


class ShopSettingsPatch(BaseModel):
    enabled: bool | None = None
    page: bool | None = None
    environment_block: bool | None = None
    phase_block: bool | None = None
    account_entry: bool | None = None
    bobby: bool | None = None
    sponsored: bool | None = None
    updated_by: str | None = Field(default=None, max_length=255)

    model_config = {"extra": "forbid"}


_SWITCH_COLUMNS = {
    "enabled": "shop_enabled", "page": "shop_page_enabled", "environment_block": "shop_grow_block_enabled",
    "phase_block": "shop_plant_block_enabled", "account_entry": "shop_account_entry_enabled",
    "sponsored": "shop_sponsored_enabled",
}


def settings_out(row) -> ShopSettingsOut:
    return ShopSettingsOut(**{field: getattr(row, column) for field, column in _SWITCH_COLUMNS.items()})


@router.get("/shop/settings", response_model=ShopSettingsOut)
async def get_shop_settings(db: AsyncSession = Depends(get_db)):
    return settings_out(await get_app_settings(db))


@router.patch("/shop/settings", response_model=ShopSettingsOut)
async def patch_shop_settings(body: ShopSettingsPatch, db: AsyncSession = Depends(get_db)):
    if body.bobby:
        raise _error(shop.ShopError(422, "O Bobby não indica produto (condição da LOJA).", "bobby"))
    row = await get_app_settings(db)
    for field, value in body.model_dump(exclude_unset=True, exclude={"bobby", "updated_by"}).items():
        if value is not None:
            setattr(row, _SWITCH_COLUMNS[field], value)
    if body.updated_by:
        row.updated_by = body.updated_by
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return settings_out(row)


# ── Categorias ─────────────────────────────────────────────────────────────

class CategoryOut(BaseModel):
    key: str
    name: str
    sort_order: int
    is_active: bool
    product_count: int


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    is_active: bool | None = None

    model_config = {"extra": "forbid"}


async def _categories(db: AsyncSession) -> list[CategoryOut]:
    counts = dict((await db.execute(
        select(ShopProduct.category_key, func.count(ShopProduct.id)).group_by(ShopProduct.category_key)
    )).all())
    rows = (await db.execute(select(ShopCategory).order_by(ShopCategory.sort_order, ShopCategory.key))).scalars().all()
    return [CategoryOut(key=c.key, name=c.name, sort_order=c.sort_order, is_active=c.is_active,
                        product_count=counts.get(c.key, 0)) for c in rows]


@router.get("/shop/categories", response_model=list[CategoryOut])
async def list_categories(db: AsyncSession = Depends(get_db)):
    return await _categories(db)


@router.patch("/shop/categories/{key}", response_model=CategoryOut)
async def patch_category(key: str, body: CategoryPatch, db: AsyncSession = Depends(get_db)):
    category = await db.get(ShopCategory, key)
    if category is None or key not in SHOP_CATEGORIES:
        raise HTTPException(status_code=404, detail="Categoria não encontrada")
    try:
        shop.check_text("name", body.name)
    except shop.ShopError as exc:
        raise _error(exc)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(category, field, value)
    await db.commit()
    return next(c for c in await _categories(db) if c.key == key)


# ── Lojas permitidas ───────────────────────────────────────────────────────

class StoreOut(BaseModel):
    id: uuid.UUID
    name: str
    domain: str
    kind: Literal["generalist", "garden_agro"]
    is_active: bool
    product_count: int = 0


class StoreIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    domain: str = Field(min_length=3, max_length=120)
    kind: Literal["generalist", "garden_agro"]
    is_active: bool = True

    model_config = {"extra": "forbid"}


class StorePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    domain: str | None = Field(default=None, min_length=3, max_length=120)
    kind: Literal["generalist", "garden_agro"] | None = None
    is_active: bool | None = None

    model_config = {"extra": "forbid"}


async def _store_out(db: AsyncSession, store: ShopStore) -> StoreOut:
    count = (await db.execute(select(func.count(ShopProduct.id)).where(ShopProduct.store_id == store.id))).scalar_one()
    return StoreOut(id=store.id, name=store.name, domain=store.domain, kind=store.kind,
                    is_active=store.is_active, product_count=count)


async def _unique_domain(db: AsyncSession, domain: str, store_id: uuid.UUID | None = None) -> None:
    query = select(ShopStore.id).where(ShopStore.domain == domain)
    if store_id is not None:
        query = query.where(ShopStore.id != store_id)
    if (await db.execute(query)).scalar_one_or_none() is not None:
        raise shop.ShopError(422, "Já existe uma loja com este domínio.", "domain")


@router.get("/shop/stores", response_model=list[StoreOut])
async def list_stores(db: AsyncSession = Depends(get_db)):
    stores = (await db.execute(select(ShopStore).order_by(ShopStore.name))).scalars().all()
    return [await _store_out(db, s) for s in stores]


@router.post("/shop/stores", response_model=StoreOut, status_code=status.HTTP_201_CREATED)
async def create_store(body: StoreIn, db: AsyncSession = Depends(get_db)):
    try:
        domain = shop.check_store(body.name, body.domain, body.kind)
        await _unique_domain(db, domain)
    except shop.ShopError as exc:
        raise _error(exc)
    store = ShopStore(id=uuid.uuid4(), name=body.name.strip(), domain=domain, kind=body.kind, is_active=body.is_active)
    db.add(store)
    await db.commit()
    return await _store_out(db, store)


@router.patch("/shop/stores/{store_id}", response_model=StoreOut)
async def patch_store(store_id: uuid.UUID, body: StorePatch, db: AsyncSession = Depends(get_db)):
    store = await db.get(ShopStore, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Loja não encontrada")
    data = body.model_dump(exclude_unset=True)
    try:
        domain = shop.check_store(data.get("name") or store.name, data.get("domain") or store.domain,
                                  data.get("kind") or store.kind)
        await _unique_domain(db, domain, store.id)
        if domain != store.domain:
            # Os produtos já cadastrados precisam continuar no domínio da loja
            products = (await db.execute(select(ShopProduct).where(ShopProduct.store_id == store.id))).scalars().all()
            probe = ShopStore(name=store.name, domain=domain, kind=store.kind)
            for product in products:
                shop.check_product_url(product.url, probe)
    except shop.ShopError as exc:
        raise _error(exc)
    store.domain = domain
    for field in ("name", "kind", "is_active"):
        if data.get(field) is not None:
            setattr(store, field, data[field].strip() if field == "name" else data[field])
    await db.commit()
    return await _store_out(db, store)


@router.delete("/shop/stores/{store_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_store(store_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    store = await db.get(ShopStore, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Loja não encontrada")
    if (await _store_out(db, store)).product_count:
        raise HTTPException(status_code=409, detail="A loja tem produtos. Apague ou mova os produtos, ou desative a loja.")
    await db.delete(store)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Gatilhos ───────────────────────────────────────────────────────────────

class TriggerOut(BaseModel):
    key: str
    label: str
    scope: Literal["grow", "plant"]


@router.get("/shop/triggers", response_model=list[TriggerOut])
async def list_triggers():
    return [TriggerOut(key=key, label=t.description, scope=t.scope) for key, t in shop.TRIGGERS.items()]


# ── Produtos ───────────────────────────────────────────────────────────────

class ProductOut(BaseModel):
    id: uuid.UUID
    name: str
    category_key: str
    spec_1: str | None
    spec_2: str | None
    store_id: uuid.UUID
    store_name: str
    url: str
    label: Literal["partner", "sponsored", "none"]
    triggers: list[str]
    reason: str | None
    is_active: bool
    price_from: float | None
    price_checked_at: date | None
    created_at: datetime
    updated_at: datetime


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category_key: str
    spec_1: str | None = Field(default=None, max_length=120)
    spec_2: str | None = Field(default=None, max_length=120)
    store_id: uuid.UUID
    url: str = Field(min_length=10, max_length=1000)
    label: Literal["partner", "sponsored", "none"] = "partner"
    triggers: list[str] = []
    reason: str | None = Field(default=None, max_length=300)
    is_active: bool = True

    model_config = {"extra": "forbid"}  # price_from/price_checked_at ficam reservados


class ProductPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    category_key: str | None = None
    spec_1: str | None = Field(default=None, max_length=120)
    spec_2: str | None = Field(default=None, max_length=120)
    store_id: uuid.UUID | None = None
    url: str | None = Field(default=None, min_length=10, max_length=1000)
    label: Literal["partner", "sponsored", "none"] | None = None
    triggers: list[str] | None = None
    reason: str | None = Field(default=None, max_length=300)
    is_active: bool | None = None

    model_config = {"extra": "forbid"}


async def _product_out(db: AsyncSession, product: ShopProduct) -> ProductOut:
    store = await db.get(ShopStore, product.store_id)
    return ProductOut(
        id=product.id, name=product.name, category_key=product.category_key, spec_1=product.spec_1,
        spec_2=product.spec_2, store_id=product.store_id, store_name=store.name if store else "", url=product.url,
        label=product.label, triggers=list(product.triggers or []), reason=product.reason, is_active=product.is_active,
        price_from=float(product.price_from) if product.price_from is not None else None,
        price_checked_at=product.price_checked_at, created_at=product.created_at, updated_at=product.updated_at,
    )


async def _validate_product(db: AsyncSession, data: dict) -> dict:
    """Valida o produto inteiro (o PATCH chega aqui já combinado com o que estava gravado)."""
    for field in ("name", "spec_1", "spec_2", "reason"):
        shop.check_text(field, data.get(field))
    if data["category_key"] not in SHOP_CATEGORIES or await db.get(ShopCategory, data["category_key"]) is None:
        raise shop.ShopError(422, "Categoria fora da lista permitida.", "category_key")
    store = await db.get(ShopStore, data["store_id"])
    if store is None:
        raise shop.ShopError(422, "Loja não cadastrada na lista permitida.", "store_id")
    unknown = [t for t in data.get("triggers") or [] if t not in shop.TRIGGERS]
    if unknown:
        raise shop.ShopError(422, f"Gatilho desconhecido: {', '.join(unknown)}.", "triggers")
    data["url"] = shop.check_product_url(data["url"], store)
    data["triggers"] = list(dict.fromkeys(data.get("triggers") or []))
    return data


@router.get("/shop/products", response_model=list[ProductOut])
async def list_products(
    category: str | None = Query(default=None),
    active: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    query = select(ShopProduct).order_by(ShopProduct.category_key, ShopProduct.created_at)
    if category:
        query = query.where(ShopProduct.category_key == category)
    if active is not None:
        query = query.where(ShopProduct.is_active.is_(active))
    return [await _product_out(db, p) for p in (await db.execute(query)).scalars().all()]


@router.post("/shop/products", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
async def create_product(body: ProductIn, db: AsyncSession = Depends(get_db)):
    try:
        data = await _validate_product(db, body.model_dump())
    except shop.ShopError as exc:
        raise _error(exc)
    product = ShopProduct(id=uuid.uuid4(), **data)
    db.add(product)
    await db.commit()
    await db.refresh(product)
    return await _product_out(db, product)


@router.patch("/shop/products/{product_id}", response_model=ProductOut)
async def patch_product(product_id: uuid.UUID, body: ProductPatch, db: AsyncSession = Depends(get_db)):
    product = await db.get(ShopProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    current = {field: getattr(product, field) for field in ProductIn.model_fields}
    changes = body.model_dump(exclude_unset=True)
    for field in ("name", "category_key", "store_id", "url", "label", "triggers", "is_active"):
        if field in changes and changes[field] is None:
            changes.pop(field)  # obrigatórios: nulo não apaga
    try:
        data = await _validate_product(db, {**current, **changes})
    except shop.ShopError as exc:
        raise _error(exc)
    for field, value in data.items():
        setattr(product, field, value)
    await db.commit()
    await db.refresh(product)
    return await _product_out(db, product)


@router.delete("/shop/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    product = await db.get(ShopProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Produto não encontrado")
    await db.delete(product)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Toques agregados ───────────────────────────────────────────────────────

class ClickOut(BaseModel):
    product_id: uuid.UUID
    product_name: str
    day: date
    count: int


@router.get("/shop/clicks", response_model=list[ClickOut])
async def list_clicks(days: int = Query(default=30, ge=1, le=365), db: AsyncSession = Depends(get_db)):
    """Contagem por produto e dia (sem usuário)."""
    since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    rows = (await db.execute(
        select(ShopClickDaily, ShopProduct.name)
        .join(ShopProduct, ShopProduct.id == ShopClickDaily.product_id)
        .where(ShopClickDaily.day >= since)
        .order_by(ShopClickDaily.day.desc(), ShopProduct.name)
    )).all()
    return [ClickOut(product_id=c.product_id, product_name=name, day=c.day, count=c.clicks) for c, name in rows]
