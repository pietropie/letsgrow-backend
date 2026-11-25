"""
Tela de Sensores do ambiente (UX item 28, SN1–SN4): Hub, cartões dos satélites com a
leitura avaliada contra a faixa ideal, e o mini-histórico agregado.

Um aparelho pode virar mais de um cartão: o Hub também mede (ar, luz, solo e solução),
e um satélite antigo sem tipo gravado vira os cartões dos tipos que aparecem na leitura.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.grow import Grow, GrowIdealRange
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorModel, SensorReading
from app.services import ideal_ranges as ranges
from app.services import relays as relay_svc
from app.services import sensor_catalog as catalog

STALE_AFTER = timedelta(minutes=30)
STATE_ORDER = {"out": 0, "near": 1, "ok": 2, "none": 3}

# Janela do mini-histórico por tipo (UX: 24 h, 3 ou 7 dias, conforme o tipo)
WINDOWS = {"24h": (timedelta(hours=24), 24), "3d": (timedelta(days=3), 18), "7d": (timedelta(days=7), 14)}
KIND_WINDOW = {"air": "24h", "light": "24h", "soil": "3d", "solution": "7d", "co2": "24h", "ph": "7d", "root": "24h"}
PRIMARY_METRIC = {"air": "temp_air", "light": "lux", "soil": "soil_moisture_pct", "solution": "ppm",
                  "co2": "co2_ppm", "ph": "ph_solution", "root": "temp_root"}


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def custom_ranges(db: AsyncSession, grow_id: uuid.UUID) -> dict[str, tuple[float, float]]:
    rows = (await db.execute(select(GrowIdealRange).where(GrowIdealRange.grow_id == grow_id))).scalars().all()
    return {r.metric: (r.min_value, r.max_value) for r in rows}


async def grow_phase(db: AsyncSession, grow_id: uuid.UUID) -> tuple[str, str]:
    phases = (await db.execute(
        select(Plant.current_phase).where(Plant.grow_id == grow_id, Plant.is_active.is_(True))
    )).scalars().all()
    return ranges.grow_phase(list(phases))


def range_out(band: ranges.Effective | None) -> dict | None:
    if band is None:
        return None
    return {"min": band.min, "max": band.max, "source": band.source, "phase": band.phase,
            "needs_review": band.needs_review, "reference": band.reference}


async def photo_urls(db: AsyncSession) -> dict[str, str]:
    rows = (await db.execute(select(SensorModel.key, SensorModel.photo_url))).all()
    return {key: url for key, url in rows if url}


def photo_for(device: SensorDevice, photos: dict[str, str]) -> str | None:
    """Foto do modelo; sem ela, a do module_type; sem as duas, None (o app usa o ícone)."""
    key = catalog.model_key(device)
    if key in photos:
        return photos[key]
    raw = (device.sensors_config or {}).get("module_type") if isinstance(device.sensors_config, dict) else None
    if device.module_type == "hub":
        raw = "hub"
    return photos.get(raw) if isinstance(raw, str) else None


def light_on_now(grow: Grow, tz_name: str, now: datetime) -> bool | None:
    if grow.photoperiod_light_minutes and grow.photoperiod_light_minutes >= 1440:
        return True
    schedule = relay_svc.light_schedule(grow)
    if schedule is None:
        return None
    lit, _ = relay_svc.schedule_now(schedule[0], schedule[1], tz_name, now)
    return lit


# ---------------------------------------------------------------------------
# Histórico agregado
# ---------------------------------------------------------------------------

@dataclass
class Bucket:
    start: datetime
    values: list[float]


def aggregate(readings: list[SensorReading], metric: str, window: str, now: datetime,
              band: ranges.Effective | None) -> dict:
    span, count = WINDOWS[window]
    size = span / count
    begin = now - span
    buckets = [Bucket(begin + size * i, []) for i in range(count)]
    for reading in readings:
        at = aware(reading.recorded_at)
        value = catalog.metric_value(reading, metric)
        if value is None or at is None or at < begin or at > now:
            continue
        index = min(int((at - begin) / size), count - 1)
        buckets[index].values.append(float(value))
    out = []
    every: list[float] = []
    for bucket in buckets:
        if bucket.values:
            avg = sum(bucket.values) / len(bucket.values)
            every.extend(bucket.values)
            out.append({"start": bucket.start, "avg": round(avg, 2), "min": round(min(bucket.values), 2),
                        "max": round(max(bucket.values), 2), "n": len(bucket.values),
                        "state": ranges.range_state(avg, band)})
        else:
            out.append({"start": bucket.start, "avg": None, "min": None, "max": None, "n": 0, "state": "none"})
    return {
        "metric": metric,
        "window": window,
        "bucket_minutes": int(size.total_seconds() // 60),
        "buckets": out,
        "min": round(min(every), 2) if every else None,
        "max": round(max(every), 2) if every else None,
    }


async def readings_since(db: AsyncSession, device_id: uuid.UUID, since: datetime) -> list[SensorReading]:
    return (await db.execute(
        select(SensorReading)
        .where(SensorReading.device_id == device_id, SensorReading.recorded_at >= since)
        .order_by(SensorReading.recorded_at.asc())
    )).scalars().all()


def out_since(readings: list[SensorReading], metric: str, band: ranges.Effective | None) -> datetime | None:
    """Início do trecho atual fora da faixa ("Abaixo da faixa há 5 horas")."""
    since = None
    for reading in reversed(readings):
        value = catalog.metric_value(reading, metric)
        if value is None:
            continue
        if ranges.range_state(value, band) != "out":
            break
        since = aware(reading.recorded_at)
    return since


# ---------------------------------------------------------------------------
# Painel
# ---------------------------------------------------------------------------

def _kind_fields(kind: str) -> dict:
    meta = catalog.KINDS[kind]
    return {"sensor_kind": kind, "kind_label": meta.label, "tag": meta.tag,
            "description": meta.description, "icon": meta.icon}


async def build_panel(db: AsyncSession, grow: Grow) -> dict:
    now = datetime.now(timezone.utc)
    devices = (await db.execute(
        select(SensorDevice)
        .outerjoin(Plant, SensorDevice.plant_id == Plant.id)
        .where((SensorDevice.grow_id == grow.id) | (Plant.grow_id == grow.id))
        .order_by(SensorDevice.created_at)
    )).scalars().unique().all()
    hubs = [d for d in devices if d.module_type == "hub"]
    satellites = [d for d in devices if d.module_type != "hub" and d.is_paired]
    plants = {p.id: p for p in (await db.execute(select(Plant).where(Plant.grow_id == grow.id))).scalars().all()}
    custom = await custom_ranges(db, grow.id)
    phase, basis = await grow_phase(db, grow.id)
    photos = await photo_urls(db)
    tz_name = await relay_svc.user_tz(db, grow.user_id)
    light_on = light_on_now(grow, tz_name, now)

    hub = hubs[0] if hubs else None
    hub_online = relay_svc.hub_online(hub, now) if hub else False
    sat_online = [s for s in satellites if s.is_online and s.last_seen_at and now - aware(s.last_seen_at) <= STALE_AFTER]

    cards = []
    for device in ([hub] if hub else []) + satellites:
        latest = (await db.execute(
            select(SensorReading).where(SensorReading.device_id == device.id)
            .order_by(SensorReading.recorded_at.desc()).limit(1)
        )).scalar_one_or_none()
        kind = catalog.device_kind(device)
        if device.module_type == "hub" or kind is None:
            kinds = catalog.kinds_in_reading(latest)
            if device.module_type != "hub" and not kinds:
                kinds = [None]
        else:
            kinds = [kind]
        plant = plants.get(device.plant_id) if device.plant_id else None
        plant_phase = plant.current_phase if plant and plant.current_phase not in (None, "unknown") else None
        history_cache: dict[str, list[SensorReading]] = {}
        for card_kind in kinds:
            cards.append(await _card(db, device, card_kind, latest, plant, plant_phase or phase, custom, photos,
                                     hub_online, light_on, now, history_cache))

    cards.sort(key=lambda c: (STATE_ORDER[c["worst_state"]], c["name"].lower()))
    hub_out = None
    if hub:
        hub_out = {
            "id": hub.id, "name": hub.name, **_kind_fields("hub"),
            "photo_url": photo_for(hub, photos),
            "online": hub_online, "last_seen_at": aware(hub.last_seen_at),
            "satellites_total": len(satellites), "satellites_online": len(sat_online),
            "clock": hub.clock_source,
        }
    return {
        "grow_id": grow.id,
        "phase": phase,
        "phase_basis": basis,
        "light_on": light_on,
        "hub": hub_out,
        "cards": cards,
        "out_of_range_count": sum(1 for c in cards if c["worst_state"] == "out"),
        "generated_at": now,
    }


async def _card(db, device, kind, latest, plant, phase, custom, photos, hub_online, light_on, now, cache) -> dict:
    meta = catalog.KINDS.get(kind) if kind else None
    last_at = aware(latest.recorded_at) if latest else None
    if latest is None:
        status = "waiting"
    elif device.module_type != "hub" and not hub_online:
        status = "stale"
    elif device.module_type == "hub" and not hub_online:
        status = "stale"
    elif now - last_at > STALE_AFTER:
        status = "no_signal"
    else:
        status = "live"

    metrics = []
    for metric in (meta.metrics if meta else ()):
        value = catalog.metric_value(latest, metric.key)
        band = ranges.effective(metric.key, phase, custom) if metric.key in ranges.RANGE_METRICS else None
        if metric.key == "lux" and light_on is False:
            band = None  # luz apagada pela programação: sem faixa
        state = ranges.range_state(value, band) if status in ("live", "no_signal") else "none"
        since = None
        if state == "out":
            if "7d" not in cache:
                cache["7d"] = await readings_since(db, device.id, now - timedelta(days=7))
            since = out_since(cache["7d"], metric.key, band)
        metrics.append({
            "metric": metric.key, "label": metric.label, "unit": metric.unit, "decimals": metric.decimals,
            "computed": metric.computed, "note": metric.note,
            "value": round(value, metric.decimals) if isinstance(value, float) else value,
            "range": range_out(band), "state": state, "out_of_range_since": since,
        })

    history = None
    if kind in PRIMARY_METRIC:
        window = KIND_WINDOW[kind]
        span = WINDOWS[window][0]
        key = "7d" if "7d" in cache else window
        if key not in cache:
            cache[key] = await readings_since(db, device.id, now - span)
        primary = PRIMARY_METRIC[kind]
        band = ranges.effective(primary, phase, custom) if primary in ranges.RANGE_METRICS else None
        history = aggregate(cache[key], primary, window, now, band)

    states = [m["state"] for m in metrics if m["state"] != "none"]
    worst = min(states, key=lambda s: STATE_ORDER[s]) if states else "none"
    placement_label = None
    if plant is not None:
        plant_name = plant.display_name or plant.strain_name or "Planta"
        placement_label = f"{plant.pot_label} · {plant_name}" if plant.pot_label else plant_name
    return {
        "device_id": device.id,
        "card_id": f"{device.id}:{kind or 'unknown'}",
        "name": device.name,
        "source": "hub" if device.module_type == "hub" else "satellite",
        **(_kind_fields(kind) if meta else {"sensor_kind": None, "kind_label": None, "tag": None,
                                             "description": None, "icon": "sensor"}),
        "photo_url": photo_for(device, photos),
        "placement": {"plant_id": plant.id if plant else None, "label": placement_label},
        "status": status,
        "is_online": device.is_online,
        "last_reading_at": last_at,
        "metrics": metrics,
        "worst_state": worst,
        "history": history,
    }
