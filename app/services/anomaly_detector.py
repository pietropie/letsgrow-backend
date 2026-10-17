"""Detecção assíncrona de anomalias agronômicas ao salvar eventos."""
import uuid
from datetime import date, datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bob import BobAnomaly
from app.models.event import GrowEvent
from app.models.plant import Plant
from app.services.bob_rules import classify_substrate

# Tipos de evento considerados rega — espelha bob_rules._WATERING_TYPES
_WATERING_TYPES: frozenset[str] = frozenset({"rega", "nutrients", "watering", "feeding"})

# Tipos de evento que indicam fase de floração
_FLOWER_EVENT_TYPES: frozenset[str] = frozenset({"flush", "harvest"})

# Tipos de evento que indicam fase vegetativa
_VEG_EVENT_TYPES: frozenset[str] = frozenset({"topping"})

_ANOMALY_TTL_HOURS = 48

# Voz do Bob para o card /bob-alerts — nunca expor o slug interno.
ANOMALY_MESSAGES: dict[str, str] = {
    "watering_gap_solo": (
        "Isso nao parece certo. Voce regou e nao registrou? "
        "Me confirma a data pra eu nao te encher de alerta a toa."
    ),
    "watering_gap_coco": (
        "Coco fica seco em 1-2 dias. Voce ta regando sem registrar?"
    ),
    "ec_out_of_range": (
        "Esse EC parece muito alto. Calibrou o medidor?"
    ),
    "ph_out_of_range": (
        "Esse pH ta fora da faixa possivel. Calibrou o medidor?"
    ),
    "phase_inconsistent": (
        "Esse evento parece nao bater com a fase atual. Tudo certo?"
    ),
}


def format_anomaly_message(anomaly_type: str, details: dict | None = None) -> str:
    """Texto humano para uma BobAnomaly. details pode enriquecer o caso."""
    base = ANOMALY_MESSAGES.get(
        anomaly_type,
        "Algo no registro nao bate. Da uma olhada e me conta o que aconteceu.",
    )
    if not details:
        return base
    days = details.get("days_since")
    if days is not None and anomaly_type.startswith("watering_gap"):
        return f"{int(days)}d sem rega no registro. {base}"
    ph = details.get("ph_value")
    if ph is not None and anomaly_type == "ph_out_of_range":
        return f"pH {ph} registrado. {base}"
    ec = details.get("ec_value")
    if ec is not None and anomaly_type == "ec_out_of_range":
        return f"EC {ec} mS/cm. {base}"
    return base


def _to_date(d: date | datetime) -> date:
    """Normaliza datetime → date; date passa sem alteração."""
    return d.date() if isinstance(d, datetime) else d  # type: ignore[return-value]


async def _anomaly_already_active(
    db: AsyncSession,
    plant_id: uuid.UUID,
    anomaly_type: str,
) -> bool:
    """Retorna True se já existe anomalia ativa (não expirada, não resolvida) do mesmo tipo."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(BobAnomaly).where(
            BobAnomaly.plant_id == plant_id,
            BobAnomaly.anomaly_type == anomaly_type,
            BobAnomaly.resolved_at.is_(None),
            BobAnomaly.expires_at > now,
        ).limit(1)
    )
    return result.scalar_one_or_none() is not None


async def _create_anomaly(
    db: AsyncSession,
    user_id: uuid.UUID,
    plant_id: uuid.UUID,
    anomaly_type: str,
    details: dict | None,
) -> BobAnomaly:
    """Persiste uma nova BobAnomaly e retorna a instância adicionada à sessão."""
    now = datetime.now(timezone.utc)
    anomaly = BobAnomaly(
        user_id=user_id,
        plant_id=plant_id,
        anomaly_type=anomaly_type,
        detected_at=now,
        expires_at=now + timedelta(hours=_ANOMALY_TTL_HOURS),
        details=details,
    )
    db.add(anomaly)
    return anomaly


async def check_and_save_anomaly(
    db: AsyncSession,
    plant: Plant,
    recent_events: list[GrowEvent],
    user_id: uuid.UUID,
) -> BobAnomaly | None:
    """Verifica anomalias agronômicas e persiste a primeira encontrada.

    Parâmetros
    ----------
    db            : sessão AsyncSession (sem auto-commit).
    plant         : instância Plant já carregada.
    recent_events : até 15 GrowEvent mais recentes ordenados DESC por event_date.
    user_id       : UUID do usuário autenticado.

    Retorno
    -------
    BobAnomaly adicionada à sessão (ainda não commitada), ou None se nenhuma
    anomalia foi detectada ou se já existe uma ativa do mesmo tipo.
    """
    today = date.today()
    plant_id = plant.id

    # ── 1. Rega impossível (solo) ─────────────────────────────────────────────
    substrate_kind = classify_substrate(plant.substrate)
    watering_evs = [ev for ev in recent_events if ev.event_type in _WATERING_TYPES]

    if watering_evs:
        last_watering_date = _to_date(watering_evs[0].event_date)
        days_since = (today - last_watering_date).days

        if substrate_kind == "solo" and days_since > 10:
            anomaly_type = "watering_gap_solo"
            if not await _anomaly_already_active(db, plant_id, anomaly_type):
                return await _create_anomaly(
                    db, user_id, plant_id, anomaly_type,
                    {"days_since": days_since, "substrate": plant.substrate,
                     "event_id": str(watering_evs[0].id)},
                )

        # ── 2. Rega impossível (coco) ─────────────────────────────────────────
        elif substrate_kind == "coco" and days_since > 5:
            anomaly_type = "watering_gap_coco"
            if not await _anomaly_already_active(db, plant_id, anomaly_type):
                return await _create_anomaly(
                    db, user_id, plant_id, anomaly_type,
                    {"days_since": days_since, "substrate": plant.substrate,
                     "event_id": str(watering_evs[0].id)},
                )

    # ── 3. EC fora do range ───────────────────────────────────────────────────
    if recent_events:
        latest = recent_events[0]
        if latest.ec_out is not None and latest.ec_out > 4.0:
            anomaly_type = "ec_out_of_range"
            if not await _anomaly_already_active(db, plant_id, anomaly_type):
                return await _create_anomaly(
                    db, user_id, plant_id, anomaly_type,
                    {"ec_value": latest.ec_out, "event_id": str(latest.id)},
                )

        # ── 4. pH fora do range ───────────────────────────────────────────────
        if latest.ph_in is not None and (latest.ph_in < 4.5 or latest.ph_in > 8.0):
            anomaly_type = "ph_out_of_range"
            if not await _anomaly_already_active(db, plant_id, anomaly_type):
                return await _create_anomaly(
                    db, user_id, plant_id, anomaly_type,
                    {"ph_value": latest.ph_in, "event_id": str(latest.id)},
                )

        # ── 5. Fase inconsistente ─────────────────────────────────────────────
        phase = (plant.current_phase or "").lower()
        event_type = latest.event_type

        is_flower_event_in_veg = event_type in _FLOWER_EVENT_TYPES and phase == "veg"
        is_veg_event_in_flower = event_type in _VEG_EVENT_TYPES and phase == "flower"

        if is_flower_event_in_veg or is_veg_event_in_flower:
            anomaly_type = "phase_inconsistent"
            if not await _anomaly_already_active(db, plant_id, anomaly_type):
                return await _create_anomaly(
                    db, user_id, plant_id, anomaly_type,
                    {"event_type": event_type, "plant_phase": plant.current_phase,
                     "event_id": str(latest.id)},
                )

    return None
