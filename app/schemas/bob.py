"""
Schemas Pydantic para o endpoint /plants/bob-alerts.
"""

from typing import Literal

from pydantic import BaseModel


class BobAlertResponse(BaseModel):
    alert_id: str           # determinístico: "{plant_id}:{action}:{date}"
    plant_id: str           # UUID como string
    plant_name: str
    tier: Literal["critical", "anomaly", "attention", "info"]
    action: str
    message: str
    reason: str


class BobAlertsResponse(BaseModel):
    alerts: list[BobAlertResponse]
    total: int
    has_critical: bool
