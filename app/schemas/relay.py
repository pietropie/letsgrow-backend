import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

EquipKind = Literal["light", "exhaust", "fan"]
RelayMode = Literal["manual", "schedule", "humidity"]
HHMM = r"^([01]\d|2[0-3]):([0-5]\d)$"


class RelaySchedule(BaseModel):
    on: str = Field(pattern=HHMM)
    # "24:00" = fim do dia (o firmware aceita no off)
    off: str = Field(pattern=r"^(([01]\d|2[0-3]):([0-5]\d)|24:00)$")

    @model_validator(mode="after")
    def _different(self):
        if self.on == self.off:
            raise ValueError("on e off precisam ser horários diferentes")
        return self


class RelayHumidity(BaseModel):
    on_above: float = Field(ge=0, le=100)
    off_below: float = Field(ge=0, le=100)
    min_on_s: int = Field(default=60, ge=30, le=3600)
    min_off_s: int = Field(default=60, ge=30, le=3600)

    @model_validator(mode="after")
    def _hysteresis(self):
        if not self.off_below < self.on_above:
            raise ValueError("off_below precisa ser menor que on_above")
        return self


class RelayPut(BaseModel):
    """PUT /iot/devices/{hub_id}/relays/{n}. Campo ausente não muda; `equip_kind: null`
    libera a posição. Mover um equipamento que está em outra posição exige `move: true`
    (sem ele: 409 equipment_in_use)."""

    equip_kind: EquipKind | None = None
    equip_key: str | None = Field(default=None, max_length=40)
    mode: RelayMode | None = None
    schedule: RelaySchedule | None = None
    humidity: RelayHumidity | None = None
    failsafe: Literal["on", "off"] | None = None
    move: bool = False


class RelayCommandIn(BaseModel):
    """`{"state": 1}` liga, `{"state": 0}` desliga, `{"action": "resume"}` encerra a pausa."""

    state: Literal[0, 1] | None = None
    action: Literal["set", "resume"] = "set"
    duration_s: int = Field(default=0, ge=0, le=86_400)

    @model_validator(mode="after")
    def _shape(self):
        if self.action == "set" and self.state is None:
            raise ValueError("state é obrigatório (0 ou 1)")
        return self


class RelayEquipment(BaseModel):
    kind: EquipKind
    key: str
    name: str
    kind_label: str
    watts: int | None
    n: int | None  # posição em que já está (em qualquer Hub do ambiente)
    hub_id: uuid.UUID | None


class RelayStateOut(BaseModel):
    n: int
    equip_kind: EquipKind | None
    equip_key: str | None
    equip_name: str | None
    kind_label: str | None
    watts: int | None
    mode: RelayMode
    # Modo que o Hub está rodando de fato (programação sem horário vira manual)
    effective_mode: RelayMode
    schedule: RelaySchedule | None
    humidity: RelayHumidity | None
    failsafe: Literal["on", "off"] | None
    state: Literal[0, 1] | None
    override: bool
    source: str | None
    ts: datetime | None
    confirmed: bool
    # Programação: se diz ligado agora e quando muda (UTC)
    schedule_lit: bool | None
    next_change_at: datetime | None


class RelayWarning(BaseModel):
    code: Literal["over_output_limit", "over_total_limit", "missing_watts"]
    n: int | None


class HubRelaysOut(BaseModel):
    hub_id: uuid.UUID
    grow_id: uuid.UUID
    online: bool
    last_seen_at: datetime | None
    synced: bool
    rev: int
    synced_rev: int | None
    sync_error: str | None
    clock: str | None
    mains_voltage: Literal[127, 220]
    mains_voltage_set: bool
    output_limit_w: int
    total_limit_w: int
    relays: list[RelayStateOut]
    equipment: list[RelayEquipment]
    warnings: list[RelayWarning]


class RelayCommandAccepted(BaseModel):
    req_id: uuid.UUID
    status: Literal["pending"] = "pending"


class RelayCommandOut(BaseModel):
    req_id: uuid.UUID
    status: Literal["pending", "confirmed", "failed", "timeout"]
    relay: int
    action: Literal["set", "resume"]
    requested_state: Literal[0, 1] | None
    state: Literal[0, 1] | None
    override: bool | None
    err: str | None
    created_at: datetime
    resolved_at: datetime | None
