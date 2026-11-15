"""
Equipamentos e insumos: regras do catálogo e das indicações (UX item 26, PLAYSTORE.md §2.3).

- Cadastro: textos sem termos de cannabis, consumo, extração, bens digitais ou promessa
  de colheita; loja só generalista ou de jardinagem e agro; URL direta para o produto
  no domínio da loja, sem dado pessoal.
- Indicação: só por gatilho de contexto calculado aqui, com os dados da conta, e só
  onde o Admin ligou o ponto de exibição. O Bobby não usa nada disto.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_settings import AppSettings
from app.models.environment_reading import EnvironmentReading
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading
from app.models.shop import STORE_KINDS, ShopCategory, ShopClickDaily, ShopDismissal, ShopProduct, ShopStore

FIXED_NOTICE = ("A compra, a entrega e a garantia são da loja. O LetsGrow pode receber comissão; "
                "isso não muda o preço para você.")
LABEL_TEXTS = {
    "partner": "Publicidade · podemos receber comissão",
    "sponsored": "Patrocinado",
    "none": "Sem comissão",
}
MAX_PER_TRIGGER = 2
READING_WINDOW = timedelta(days=7)
REPEATED_READINGS = 3

# ── Textos do cadastro ──────────────────────────────────────────────────────

_BLOCKED_TEXT = re.compile(
    "|".join([
        # cannabis e gírias
        r"maconha", r"cannabis", r"\b420\b", r"\bweed\b", r"\berva\b", r"\bbuds?\b", r"\bganja\b", r"marijuana",
        r"\bthc\b", r"\bcbd\b", r"\bsementes?\b", r"\bclones?\b",
        # consumo e extração
        r"\bsedas?\b", r"piteira", r"\bbongs?\b", r"dichavador", r"\bgrinder", r"vaporizador", r"isqueiro",
        r"\brosin\b", r"bubble", r"peneira", r"\bbho\b", r"extra[çc][ãa]o", r"teste de thc",
        r"balan[çc]a (de )?(precis[ãa]o|bolso)", r"para (a )?flor\b",
        # bens digitais
        r"\bcursos?\b", r"e-?books?", r"consultoria", r"assinatura", r"\bapps?\b",
        # promessa de colheita e superlativo sem critério
        r"(aument|dobr|garant|maximiz|multiplic)\w*.{0,25}(colheita|rendimento|produ[çc][ãa]o)",
        r"\b(o|a) melhor\b", r"\boficial\b",
    ]),
    re.IGNORECASE,
)
_BLOCKED_STORE = re.compile(
    r"grow|head|seed|semente|\b420\b|cannabis|maconha|weed|tabacaria|smoke|hemp|c[âa]nhamo|bong|seda|cultivo-?indoor",
    re.IGNORECASE,
)
# Parâmetros que levariam dado pessoal na URL de saída (só a tag de afiliado pode ir)
_PERSONAL_PARAMS = re.compile(r"^(e-?mail|user(_?id)?|uid|cpf|phone|telefone|name|nome|grow|plant|token)$", re.IGNORECASE)


class ShopError(Exception):
    """Erro de regra. Com `field`, a rota responde 422 no formato do FastAPI ({loc, msg}) para o Admin
    mostrar a mensagem no campo."""

    def __init__(self, status: int, detail: str, field: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.field = field


def check_text(field: str, value: str | None) -> None:
    if value and (match := _BLOCKED_TEXT.search(value)):
        raise ShopError(422, f"O termo \"{match.group(0)}\" não é permitido nesta área.", field)


def normalize_domain(domain: str) -> str:
    value = domain.strip().lower()
    value = re.sub(r"^https?://", "", value).split("/")[0]
    value = value.removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", value):
        raise ShopError(422, "Domínio inválido. Use só o domínio, por exemplo loja.com.br.", "domain")
    return value


def check_store(name: str, domain: str, kind: str) -> str:
    if kind not in STORE_KINDS:
        raise ShopError(422, "Só lojas generalistas ou de jardinagem e agro.", "kind")
    if _BLOCKED_STORE.search(name) or _BLOCKED_STORE.search(domain):
        raise ShopError(422, "Lojas de cultivo de cannabis, growshops, headshops e bancos de sementes não são "
                             "permitidas.", "domain" if _BLOCKED_STORE.search(domain) else "name")
    check_text("name", name)
    return normalize_domain(domain)


def check_product_url(url: str, store: ShopStore) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme != "https":
        raise ShopError(422, "O link precisa começar com https://.", "url")
    if host != store.domain and not host.endswith("." + store.domain):
        raise ShopError(422, f"O link precisa ser da loja escolhida ({store.domain}).", "url")
    if parsed.path in ("", "/"):
        raise ShopError(422, "Use o link direto da página do produto, e não a página inicial da loja.", "url")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if _PERSONAL_PARAMS.match(key):
            raise ShopError(422, f"O link não pode levar dado pessoal (parâmetro \"{key}\"); só a tag de afiliado.", "url")
    if "{" in url or "}" in url:
        raise ShopError(422, "O link não pode ter campos variáveis; só a tag de afiliado fixa.", "url")
    return url.strip()


# ── Gatilhos de contexto ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Trigger:
    scope: str           # grow | plant
    description: str     # para o Admin escolher no formulário
    equipment: dict      # o que "Já tenho" grava no ambiente (vazio = só dispensa)


TRIGGERS: dict[str, Trigger] = {
    "grow_missing_light": Trigger("grow", "Ambiente fechado sem luz cadastrada", {}),
    "grow_missing_exhaust": Trigger("grow", "Ambiente fechado sem exaustor cadastrado", {}),
    "grow_missing_circulation": Trigger("grow", "Ambiente fechado sem ventilador de circulação", {}),
    "grow_manual_readings_only": Trigger("grow", "Leituras só manuais (sem sensor)", {"has_environment_sensors": True}),
    "grow_humidity_high": Trigger("grow", "Umidade acima da faixa em várias leituras, sem desumidificador",
                                  {"dehumidifier": True}),
    "grow_humidity_low": Trigger("grow", "Umidade abaixo da faixa em várias leituras, sem umidificador",
                                 {"humidifier": True}),
    "grow_temperature_high": Trigger("grow", "Temperatura acima da faixa em várias leituras, sem ar-condicionado",
                                     {"air_conditioning": True}),
    "grow_temperature_low": Trigger("grow", "Temperatura abaixo da faixa em várias leituras, sem aquecedor",
                                    {"heater": True}),
    "plant_phase_seedling": Trigger("plant", "Planta em muda", {}),
    "plant_phase_veg": Trigger("plant", "Planta em vegetativo", {}),
    "plant_phase_flower": Trigger("plant", "Planta em floração", {}),
}
_PHASE_NAMES = {"seedling": "muda", "veg": "vegetativo", "flower": "floração", "late_flower": "floração"}
_PHASE_TRIGGER = {"seedling": "plant_phase_seedling", "veg": "plant_phase_veg",
                  "flower": "plant_phase_flower", "late_flower": "plant_phase_flower"}


def _number(value: float) -> str:
    return (f"{value:.1f}".rstrip("0").rstrip(".")).replace(".", ",")


async def _recent_readings(db: AsyncSession, grow: Grow, now: datetime) -> list[tuple[float | None, float | None]]:
    """(temperatura, umidade) dos registros manuais e dos sensores do ambiente nos últimos 7 dias."""
    since = now - READING_WINDOW
    manual = (await db.execute(
        select(EnvironmentReading.temperature_c, EnvironmentReading.humidity_rh)
        .where(EnvironmentReading.grow_id == grow.id, EnvironmentReading.observed_at >= since)
    )).all()
    sensors = (await db.execute(
        select(SensorReading.temp_air, SensorReading.humidity_air)
        .join(SensorDevice, SensorDevice.id == SensorReading.device_id)
        .where(SensorDevice.grow_id == grow.id, SensorReading.recorded_at >= since)
    )).all()
    return [tuple(r) for r in [*manual, *sensors]]


async def grow_triggers(db: AsyncSession, grow: Grow, now: datetime) -> list[tuple[str, str]]:
    """(gatilho, motivo pronto) que valem para o ambiente agora, só com o que foi cadastrado e medido."""
    found: list[tuple[str, str]] = []
    name = grow.name
    if grow.grow_type != "outdoor":
        if grow.light_type is None and grow.lighting_watts is None:
            found.append(("grow_missing_light", f"{name} não tem luz cadastrada."))
        if grow.exhaust_type in (None, "none"):
            found.append(("grow_missing_exhaust", f"{name} não tem exaustor cadastrado."))
        if not grow.internal_circulation_fans and not grow.fans:
            found.append(("grow_missing_circulation", f"{name} não tem ventilador de circulação cadastrado."))
    has_sensor = (await db.execute(
        select(SensorDevice.id).where(SensorDevice.grow_id == grow.id).limit(1)
    )).scalar_one_or_none() is not None
    if not has_sensor and not grow.has_environment_sensors:
        found.append(("grow_manual_readings_only", f"As leituras de {name} são só manuais."))

    readings = await _recent_readings(db, grow, now)
    if grow.target_humidity_rh is not None:
        target = grow.target_humidity_rh
        high = [h for _, h in readings if h is not None and h > target + 5]
        low = [h for _, h in readings if h is not None and h < target - 5]
        if len(high) >= REPEATED_READINGS and not grow.dehumidifier:
            found.append(("grow_humidity_high", f"A umidade em {name} ficou acima da faixa ({_number(target)}%) "
                                                f"em {len(high)} leituras nos últimos 7 dias."))
        if len(low) >= REPEATED_READINGS and not grow.humidifier:
            found.append(("grow_humidity_low", f"A umidade em {name} ficou abaixo da faixa ({_number(target)}%) "
                                               f"em {len(low)} leituras nos últimos 7 dias."))
    if grow.target_temp_c is not None:
        target = grow.target_temp_c
        high = [t for t, _ in readings if t is not None and t > target + 3]
        low = [t for t, _ in readings if t is not None and t < target - 3]
        if len(high) >= REPEATED_READINGS and not grow.air_conditioning:
            found.append(("grow_temperature_high", f"A temperatura em {name} ficou acima da faixa "
                                                   f"({_number(target)} °C) em {len(high)} leituras nos últimos 7 dias."))
        if len(low) >= REPEATED_READINGS and not grow.heater:
            found.append(("grow_temperature_low", f"A temperatura em {name} ficou abaixo da faixa "
                                                  f"({_number(target)} °C) em {len(low)} leituras nos últimos 7 dias."))
    return found


def plant_triggers(plant: Plant) -> list[tuple[str, str]]:
    """A fase da planta. O motivo é neutro ("a sua planta"): o nome costuma ser o de uma genética (LOJA, 15/11)."""
    trigger = _PHASE_TRIGGER.get(plant.current_phase)
    if trigger is None:
        return []
    return [(trigger, f"A sua planta está em {_PHASE_NAMES[plant.current_phase]}.")]


# ── Indicações ──────────────────────────────────────────────────────────────

def product_out(product: ShopProduct, category: ShopCategory, store: ShopStore) -> dict:
    return {
        "id": product.id, "category": {"key": category.key, "name": category.name},
        "name": product.name, "specs": [s for s in (product.spec_1, product.spec_2) if s],
        "store": {"name": store.name, "domain": store.domain}, "url": product.url,
        "label": product.label, "label_text": LABEL_TEXTS[product.label], "reason": product.reason,
        "price_from": float(product.price_from) if product.price_from is not None else None,
        "price_checked_at": product.price_checked_at,
    }


async def visible_products(db: AsyncSession, settings: AppSettings, category: str | None = None) -> list[dict]:
    query = (
        select(ShopProduct, ShopCategory, ShopStore)
        .join(ShopCategory, ShopCategory.key == ShopProduct.category_key)
        .join(ShopStore, ShopStore.id == ShopProduct.store_id)
        .where(ShopProduct.is_active.is_(True), ShopCategory.is_active.is_(True), ShopStore.is_active.is_(True))
        .order_by(ShopCategory.sort_order, ShopProduct.created_at)
    )
    if category:
        query = query.where(ShopProduct.category_key == category)
    if not settings.shop_sponsored_enabled:
        query = query.where(ShopProduct.label != "sponsored")
    return [product_out(*row) for row in (await db.execute(query)).all()]


async def recommendations(
    db: AsyncSession, user_id: uuid.UUID, settings: AppSettings,
    grow_id: uuid.UUID | None = None, plant_id: uuid.UUID | None = None, now: datetime | None = None,
) -> list[dict]:
    """Produto, motivo e gatilho para o ambiente, a planta ou (sem parâmetro) a página inteira."""
    now = now or datetime.now(timezone.utc)
    if not settings.shop_enabled:
        return []
    if grow_id is not None:
        if not settings.shop_grow_block_enabled:
            return []
        grow = (await db.execute(select(Grow).where(Grow.id == grow_id, Grow.user_id == user_id))).scalar_one_or_none()
        if grow is None:
            raise ShopError(404, "Ambiente não encontrado")
        grows, plants = ([grow] if grow.status == "active" else []), []
    elif plant_id is not None:
        if not settings.shop_plant_block_enabled:
            return []
        plant = (await db.execute(select(Plant).where(Plant.id == plant_id, Plant.user_id == user_id))).scalar_one_or_none()
        if plant is None:
            raise ShopError(404, "Planta não encontrada")
        grows, plants = [], ([plant] if plant.is_active else [])
    else:
        if not settings.shop_page_enabled:
            return []
        grows = list((await db.execute(
            select(Grow).where(Grow.user_id == user_id, Grow.status == "active").order_by(Grow.created_at)
        )).scalars().all())
        plants = list((await db.execute(
            select(Plant).where(Plant.user_id == user_id, Plant.is_active.is_(True)).order_by(Plant.created_at)
        )).scalars().all())

    dismissed = {
        (d.trigger, d.grow_id, d.plant_id)
        for d in (await db.execute(select(ShopDismissal).where(ShopDismissal.user_id == user_id))).scalars().all()
    }
    contexts: list[tuple[str, str, uuid.UUID | None, uuid.UUID | None]] = []
    for grow in grows:
        contexts += [(t, r, grow.id, None) for t, r in await grow_triggers(db, grow, now) if (t, grow.id, None) not in dismissed]
    for plant in plants:
        contexts += [(t, r, None, plant.id) for t, r in plant_triggers(plant) if (t, None, plant.id) not in dismissed]
    if not contexts:
        return []

    catalog = (await db.execute(
        select(ShopProduct, ShopCategory, ShopStore)
        .join(ShopCategory, ShopCategory.key == ShopProduct.category_key)
        .join(ShopStore, ShopStore.id == ShopProduct.store_id)
        .where(ShopProduct.is_active.is_(True), ShopCategory.is_active.is_(True), ShopStore.is_active.is_(True))
        .order_by(ShopCategory.sort_order, ShopProduct.created_at)
    )).all()
    items = []
    for trigger, reason, g_id, p_id in contexts:
        matched = [row for row in catalog if trigger in (row[0].triggers or [])
                   and (row[0].label != "sponsored" or settings.shop_sponsored_enabled)][:MAX_PER_TRIGGER]
        items += [{"trigger": trigger, "reason": reason, "grow_id": g_id, "plant_id": p_id,
                   "product": product_out(*row)} for row in matched]
    return items


async def have_it(
    db: AsyncSession, user_id: uuid.UUID, trigger: str,
    grow_id: uuid.UUID | None, plant_id: uuid.UUID | None, product_id: uuid.UUID | None,
) -> dict:
    """"Já tenho": grava o equipamento no ambiente (quando o gatilho diz qual) e dispensa a indicação."""
    spec = TRIGGERS.get(trigger)
    if spec is None:
        raise ShopError(422, "Gatilho desconhecido")
    if spec.scope == "grow":
        if grow_id is None or plant_id is not None:
            raise ShopError(422, "Este gatilho é de ambiente: envie grow_id.")
        grow = (await db.execute(select(Grow).where(Grow.id == grow_id, Grow.user_id == user_id))).scalar_one_or_none()
        if grow is None:
            raise ShopError(404, "Ambiente não encontrado")
        for field, value in spec.equipment.items():
            setattr(grow, field, value)
    else:
        if plant_id is None or grow_id is not None:
            raise ShopError(422, "Este gatilho é de planta: envie plant_id.")
        plant = (await db.execute(select(Plant).where(Plant.id == plant_id, Plant.user_id == user_id))).scalar_one_or_none()
        if plant is None:
            raise ShopError(404, "Planta não encontrada")
    exists = (await db.execute(select(ShopDismissal.id).where(
        ShopDismissal.user_id == user_id, ShopDismissal.trigger == trigger,
        ShopDismissal.grow_id.is_(grow_id) if grow_id is None else ShopDismissal.grow_id == grow_id,
        ShopDismissal.plant_id.is_(plant_id) if plant_id is None else ShopDismissal.plant_id == plant_id,
    ))).scalar_one_or_none()
    if exists is None:
        db.add(ShopDismissal(id=uuid.uuid4(), user_id=user_id, trigger=trigger, grow_id=grow_id,
                             plant_id=plant_id, product_id=product_id))
    try:
        await db.commit()
    except IntegrityError:  # dois toques ao mesmo tempo
        await db.rollback()
    return {"trigger": trigger, "grow_id": grow_id, "plant_id": plant_id, "equipment": spec.equipment}


async def count_click(db: AsyncSession, product_id: uuid.UUID, today: date | None = None) -> str:
    """Soma 1 ao dia do produto (sem usuário) e devolve o link de saída."""
    product = await db.get(ShopProduct, product_id)
    if product is None or not product.is_active:
        raise ShopError(404, "Produto não encontrado")
    today = today or datetime.now(timezone.utc).date()
    row = await db.get(ShopClickDaily, (product_id, today))
    if row is None:
        db.add(ShopClickDaily(product_id=product_id, day=today, clicks=1))
    else:
        row.clicks += 1
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        row = await db.get(ShopClickDaily, (product_id, today))
        row.clicks += 1
        await db.commit()
    return product.url
