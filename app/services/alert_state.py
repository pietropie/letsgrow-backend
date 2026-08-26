"""
Dedup e acknowledge de alertas críticos do Bob.

Cada alerta é identificado por (user_id, alert_id).
alert_id é derivado de (plant_id, action, date) para ser determinístico:
  alert_id = f"{plant_id}:{action}:{date_str}"  # ex: "abc123:watering:2026-07-13"

Quando o usuário reconhece um alerta, ele é silenciado por silence_hours (padrão 24h).
Alertas não reconhecidos repetem a cada fetch.

Estado de UI/cache — Redis é suficiente aqui (diferente de RecommendationLog que é
estado de negócio e vai em Postgres).
"""

import uuid
from datetime import date

from app.services.redis_client import _get_redis


def make_alert_id(
    plant_id: "str | uuid.UUID",
    action: str,
    today: "date | None" = None,
) -> str:
    """Gera um alert_id determinístico para (planta, ação, dia).

    Exemplos:
        make_alert_id("abc123", "watering")          -> "abc123:watering:2026-07-13"
        make_alert_id(some_uuid, "harvest", d)       -> "<uuid>:harvest:2026-07-13"
    """
    d = today or date.today()
    return f"{plant_id}:{action}:{d.isoformat()}"


async def acknowledge_alert(
    user_id: uuid.UUID,
    alert_id: str,
    silence_hours: int = 24,
) -> None:
    """Silencia um alerta por silence_hours.

    O mobile chama isso quando o usuário toca em 'ok' / descarta o alerta.
    Após silence_hours, a chave expira e o alerta volta a aparecer caso
    ainda seja relevante.
    """
    key = f"bob_alert_ack:{user_id}:{alert_id}"
    r = _get_redis()
    await r.set(key, "1", ex=silence_hours * 3600)


async def is_acknowledged(user_id: uuid.UUID, alert_id: str) -> bool:
    """Retorna True se o alerta foi reconhecido e ainda está no período de silêncio.

    Falha silenciosa: em caso de erro no Redis retorna False para não suprimir
    alertas indevidamente.
    """
    key = f"bob_alert_ack:{user_id}:{alert_id}"
    try:
        r = _get_redis()
        return bool(await r.exists(key))
    except Exception:
        # Preferível mostrar alerta duplicado a suprimir um alerta crítico.
        return False


async def filter_unacknowledged(
    user_id: uuid.UUID,
    alert_ids: list[str],
) -> list[str]:
    """Retorna apenas os alert_ids que NÃO foram reconhecidos (batch check via mget).

    Falha silenciosa: em caso de erro no Redis retorna todos os IDs para garantir
    que nenhum alerta crítico seja suprimido silenciosamente.
    """
    if not alert_ids:
        return []
    try:
        r = _get_redis()
        keys = [f"bob_alert_ack:{user_id}:{aid}" for aid in alert_ids]
        # mget retorna lista com string ou None (decode_responses=True no cliente)
        results = await r.mget(*keys)
        return [aid for aid, val in zip(alert_ids, results) if val is None]
    except Exception:
        # Falha silenciosa — retorna todos se Redis falhar.
        return alert_ids
