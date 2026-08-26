"""
Endpoints do Bob 2.0: alertas criticos e acknowledge.

GET  /plants/bob-alerts                         – alertas ativos (critical + anomaly), sem cache
POST /plants/bob-alerts/{alert_id}/acknowledge  – silencia alerta por 24h

Registrado em router.py com prefix="/plants" ANTES do plants.router para evitar
que GET /plants/{plant_id} capture /plants/bob-alerts por correspondencia de parametro.
"""

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.bob import BobAnomaly, BobRecommendation
from app.models.event import GrowEvent
from app.models.plant import Plant
from app.models.user import User
from app.schemas.bob import BobAlertResponse, BobAlertsResponse
from app.services.alert_state import (
    acknowledge_alert,
    filter_unacknowledged,
    make_alert_id,
)
from app.services.anomaly_detector import format_anomaly_message
from app.services.auth import get_current_user
from app.services.bob_accountability import reschedule_recommendation
from app.services.bob_env import load_latest_sensor_env
from app.services.bob_rules import compute_bob_steps

logger = logging.getLogger(__name__)

router = APIRouter()

# Fuso horario Brasil (UTC-3)
_BRASIL_TZ = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------------------
# GET /plants/bob-alerts
# ---------------------------------------------------------------------------


@router.get("/bob-alerts", response_model=BobAlertsResponse)
async def get_bob_alerts(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> BobAlertsResponse:
    """
    Retorna alertas criticos e anomalias do Bob para o usuario.

    - Sem cache (sempre computa em tempo real)
    - Filtra tier=='critical' ou tier=='anomaly' apenas
    - Inclui alert_id deterministico para dedup no mobile
    - Filtra alertas ja reconhecidos (acknowledged) via Redis
    - Inclui anomalias da tabela bob_anomalies ativas
    """
    today: date = datetime.now(_BRASIL_TZ).date()
    user_id: uuid.UUID = current_user.id

    # 1. Buscar plantas ativas do usuario (limit 10, order by created_at desc)
    plants_result = await db.execute(
        select(Plant)
        .where(Plant.user_id == user_id, Plant.is_active == True)  # noqa: E712
        .order_by(Plant.created_at.desc())
        .limit(10)
    )
    plants = plants_result.scalars().all()

    # Mapa plant_id -> plant_name ja disponivel a partir das plantas carregadas
    plant_name_map: dict[uuid.UUID, str] = {
        p.id: (p.strain_name or "Planta sem nome") for p in plants
    }

    step_alerts: list[BobAlertResponse] = []

    for plant in plants:
        # 2. Carregar ultimos 15 eventos da planta (ordem DESC — igual ao bob_rules)
        events_result = await db.execute(
            select(GrowEvent)
            .where(GrowEvent.plant_id == plant.id)
            .order_by(GrowEvent.event_date.desc())
            .limit(15)
        )
        events = events_result.scalars().all()

        latest_temp, latest_humidity = await load_latest_sensor_env(db, plant.id)
        steps = await compute_bob_steps(
            plant,
            events,
            today,
            latest_temp=latest_temp,
            latest_humidity=latest_humidity,
        )

        # 4. Filtrar apenas critical e anomaly; gerar alert_id deterministico
        plant_name = plant.strain_name or "Planta sem nome"
        for step in steps:
            if step.tier not in ("critical", "anomaly"):
                continue
            alert_id = make_alert_id(step.plant_id, step.action, today)
            step_alerts.append(
                BobAlertResponse(
                    alert_id=alert_id,
                    plant_id=str(step.plant_id),
                    plant_name=plant_name,
                    tier=step.tier,
                    action=step.action,
                    message=step.message,
                    reason=step.reason,
                )
            )

    # 5. Filtrar alertas ja reconhecidos (batch check no Redis)
    all_step_ids = [a.alert_id for a in step_alerts]
    unacked_ids: set[str] = set(await filter_unacknowledged(user_id, all_step_ids))
    unacked_step_alerts = [a for a in step_alerts if a.alert_id in unacked_ids]

    # 6. Buscar BobAnomaly ativos (resolved_at IS NULL AND expires_at > now())
    now_utc = datetime.now(timezone.utc)
    anomalies_result = await db.execute(
        select(BobAnomaly).where(
            BobAnomaly.user_id == user_id,
            BobAnomaly.resolved_at.is_(None),
            BobAnomaly.expires_at > now_utc,
        )
    )
    anomalies = anomalies_result.scalars().all()

    # Resolver plant_name para anomalias com plant_id fora das 10 plantas ja carregadas
    missing_plant_ids = {
        a.plant_id
        for a in anomalies
        if a.plant_id is not None and a.plant_id not in plant_name_map
    }
    if missing_plant_ids:
        extra_result = await db.execute(
            select(Plant.id, Plant.strain_name).where(Plant.id.in_(missing_plant_ids))
        )
        for row in extra_result.all():
            plant_name_map[row[0]] = row[1] or "Planta sem nome"

    anomaly_alerts: list[BobAlertResponse] = [
        BobAlertResponse(
            alert_id=f"anomaly:{anomaly.id}",
            plant_id=str(anomaly.plant_id) if anomaly.plant_id else "",
            plant_name=(
                plant_name_map.get(anomaly.plant_id, "Planta sem nome")
                if anomaly.plant_id
                else "Sem planta"
            ),
            tier="anomaly",
            action=anomaly.anomaly_type,
            message=format_anomaly_message(anomaly.anomaly_type, anomaly.details),
            reason=f"BobAnomaly id={anomaly.id} type={anomaly.anomaly_type}",
        )
        for anomaly in anomalies
    ]

    # 7. Combinar steps nao-acknowledged + anomalias ativas
    all_alerts = unacked_step_alerts + anomaly_alerts

    # 8. Retornar resposta
    return BobAlertsResponse(
        alerts=all_alerts,
        total=len(all_alerts),
        has_critical=any(a.tier == "critical" for a in all_alerts),
    )


# ---------------------------------------------------------------------------
# POST /plants/bob-alerts/{alert_id}/acknowledge
# ---------------------------------------------------------------------------


@router.post("/bob-alerts/{alert_id}/acknowledge", status_code=204)
async def acknowledge_bob_alert(
    alert_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Silencia um alerta por 24h. Chamado quando o usuario toca em 'ok' no card."""
    await acknowledge_alert(current_user.id, alert_id, silence_hours=24)


# ---------------------------------------------------------------------------
# POST /plants/bob-recommendations/{rec_id}/fulfill
# ---------------------------------------------------------------------------


class FulfillRequest(BaseModel):
    fulfilled: bool = True
    fulfilled_at: str | None = None  # ISO 8601, ex: "2026-07-13T10:00:00Z"
    reason: str | None = None        # opcional — "nao consegui" / reagendar


@router.post("/bob-recommendations/{rec_id}/fulfill", status_code=204)
async def fulfill_bob_recommendation(
    rec_id: uuid.UUID,
    body: FulfillRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Fecha ou reagenda uma BobRecommendation.

    Body JSON:
      { "fulfilled": true, "fulfilled_at": "2026-07-13T10:00:00Z" }
      { "fulfilled": false, "reason": "estava viajando" }

    fulfilled=true  — marca como feita (data informada pelo usuario, nunca inferida).
    fulfilled=false — mantem pendente e empurra o prazo +48h.
    """
    result = await db.execute(
        select(BobRecommendation).where(
            BobRecommendation.id == rec_id,
            BobRecommendation.user_id == current_user.id,
        )
    )
    rec = result.scalar_one_or_none()
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recomendacao nao encontrada",
        )

    if body.fulfilled:
        rec.followed_up = True
        if body.fulfilled_at:
            try:
                rec.fulfilled_at = datetime.fromisoformat(
                    body.fulfilled_at.replace("Z", "+00:00")
                )
            except ValueError:
                logger.warning(
                    "fulfilled_at invalido ('%s') — usando now()", body.fulfilled_at
                )
                rec.fulfilled_at = datetime.now(timezone.utc)
        else:
            rec.fulfilled_at = datetime.now(timezone.utc)
    else:
        await reschedule_recommendation(rec, hours=48)
        if body.reason:
            logger.info(
                "Bob rec %s reagendada (%s): %s",
                rec.id, rec.action, body.reason[:200],
            )

    await db.commit()
