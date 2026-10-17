"""
Schemas Pydantic para o endpoint /plants/bob-alerts.
"""

from typing import Literal

from pydantic import BaseModel


class BobEvidence(BaseModel):
    """Registro usado por uma observacao do Bobby."""
    kind: Literal["event", "environment_reading", "sensor_reading", "plant"]
    id: str
    observed_at: str | None = None      # ISO 8601; nulo para datas cadastradas na planta
    source: Literal["manual", "sensor"] | None = None
    plant_id: str | None = None
    grow_id: str | None = None
    values: dict = {}


class BobAlertResponse(BaseModel):
    alert_id: str           # determinístico: "{plant_id}:{action}:{date}"
    plant_id: str           # UUID como string
    plant_name: str
    tier: Literal["critical", "anomaly", "attention", "info"]
    action: str
    message: str
    reason: str
    # Registros citados pela regra. Vazio = a regra nao consegue citar a base.
    evidence: list[BobEvidence] = []


class BobAlertsResponse(BaseModel):
    alerts: list[BobAlertResponse]
    total: int
    has_critical: bool
