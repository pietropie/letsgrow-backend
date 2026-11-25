"""
Relés do Hub pelo app (UX item 28b/28c; contrato da API em docs/API-SENSORES-RELES.md).

Routes (montadas em /iot pelo app/api/v1/iot.py)
------
GET  /iot/devices/{hub_id}/relays               As 4 posições, equipamentos do ambiente e avisos de carga
PUT  /iot/devices/{hub_id}/relays/{n}           Vínculo com o equipamento, modo, programação e umidade
POST /iot/devices/{hub_id}/relays/{n}/command   Liga, desliga ou encerra a pausa: 202 {req_id}
GET  /iot/relay-commands/{req_id}               Situação do comando (o app consulta a cada ~1 s)

A chave do app mostra só o estado confirmado pelo Hub. O comando responde 202 e a
confirmação chega pelo listener MQTT (relay/ack), que pode estar em outro processo.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.sensor import HubRelay, RelayCommand, SensorDevice
from app.models.user import User
from app.schemas.relay import (
    HubRelaysOut,
    RelayCommandAccepted,
    RelayCommandIn,
    RelayCommandOut,
    RelayPut,
)
from app.services import mqtt_broker
from app.services import relays as svc
from app.services.auth import get_current_user

router = APIRouter()


async def _owned_hub(hub_id: uuid.UUID, user: User, db: AsyncSession) -> tuple[SensorDevice, Grow]:
    row = (await db.execute(
        select(SensorDevice, Grow)
        .join(Grow, SensorDevice.grow_id == Grow.id)
        .where(SensorDevice.id == hub_id, Grow.user_id == user.id)
    )).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispositivo nao encontrado")
    hub, grow = row
    if hub.module_type != "hub" or svc.hub_mac(hub) is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_a_hub")
    return hub, grow


def _position(n: int) -> int:
    if n not in svc.POSITIONS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Posição não existe (1 a 4)")
    return n


async def relays_view(db: AsyncSession, hub: SensorDevice, grow: Grow) -> dict:
    relays = await svc.hub_relays(db, hub)
    equip = svc.equipment_by_key(grow)
    tz_name = await svc.user_tz(db, grow.user_id)
    now = datetime.now(timezone.utc)
    taken = (await db.execute(
        select(HubRelay.equip_key, HubRelay.n, HubRelay.hub_device_id)
        .where(HubRelay.grow_id == grow.id, HubRelay.equip_key.is_not(None))
    )).all()
    where = {key: (n, hub_id) for key, n, hub_id in taken}

    states = []
    for relay in relays:
        item = equip.get(relay.equip_key or "")
        schedule = svc.relay_schedule(relay, grow)
        lit = next_change = None
        mode = svc.effective_mode(relay, grow)
        if mode == "schedule" and schedule:
            lit, next_change = svc.schedule_now(schedule[0], schedule[1], tz_name, now)
        humidity = None
        if relay.hum_on_above is not None and relay.hum_off_below is not None:
            humidity = {"on_above": relay.hum_on_above, "off_below": relay.hum_off_below,
                        "min_on_s": relay.min_on_s, "min_off_s": relay.min_off_s}
        states.append({
            "n": relay.n,
            "equip_kind": relay.equip_kind,
            "equip_key": relay.equip_key,
            "equip_name": item.name if item else None,
            "kind_label": svc.KIND_LABEL.get(relay.equip_kind or ""),
            "watts": item.watts if item else None,
            "mode": relay.mode,
            "effective_mode": mode,
            "schedule": {"on": schedule[0], "off": schedule[1]} if schedule else None,
            "humidity": humidity,
            "failsafe": relay.failsafe,
            "state": relay.state,
            "override": relay.override,
            "source": relay.state_source,
            "ts": svc._aware(relay.state_at),
            "confirmed": relay.state_confirmed,
            "schedule_lit": lit,
            "next_change_at": next_change,
        })
    voltage = hub.mains_voltage if hub.mains_voltage in (127, 220) else svc.DEFAULT_VOLTAGE
    return {
        "hub_id": hub.id,
        "grow_id": grow.id,
        "online": svc.hub_online(hub, now),
        "last_seen_at": svc._aware(hub.last_seen_at),
        "synced": hub.relay_rev == 0 or hub.relay_synced_rev == hub.relay_rev,
        "rev": hub.relay_rev,
        "synced_rev": hub.relay_synced_rev,
        "sync_error": hub.relay_sync_error,
        "clock": hub.clock_source,
        "mains_voltage": voltage,
        "mains_voltage_set": hub.mains_voltage in (127, 220),
        "output_limit_w": svc.OUTPUT_LIMIT_W,
        "total_limit_w": svc.total_limit(voltage),
        "relays": states,
        "equipment": [
            {"kind": e.kind, "key": e.key, "name": e.name, "kind_label": svc.KIND_LABEL[e.kind], "watts": e.watts,
             "n": where.get(e.key, (None, None))[0], "hub_id": where.get(e.key, (None, None))[1]}
            for e in equip.values()
        ],
        "warnings": svc.load_warnings(relays, equip, hub.mains_voltage),
    }


@router.get("/devices/{hub_id}/relays", response_model=HubRelaysOut)
async def get_relays(
    hub_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    hub, grow = await _owned_hub(hub_id, current_user, db)
    view = await relays_view(db, hub, grow)
    await db.commit()  # posições criadas na primeira leitura
    return view


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


@router.put("/devices/{hub_id}/relays/{n}", response_model=HubRelaysOut)
async def put_relay(
    hub_id: uuid.UUID,
    n: int,
    body: RelayPut,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Grava a posição e publica a configuração (retida) com o rev novo. Com o Hub
    offline também grava: ele recebe a configuração quando conectar."""
    _position(n)
    hub, grow = await _owned_hub(hub_id, current_user, db)
    relays = await svc.hub_relays(db, hub)
    relay = relays[n - 1]
    fields = body.model_fields_set
    equip = svc.equipment_by_key(grow)
    now = datetime.now(timezone.utc)
    other_hubs: set[uuid.UUID] = set()

    if fields & {"equip_kind", "equip_key"}:
        new_key = None
        if body.equip_kind is not None or body.equip_key is not None:
            new_key = body.equip_key or (body.equip_kind if body.equip_kind in ("light", "exhaust") else None)
            item = equip.get(new_key or "")
            if item is None or (body.equip_kind is not None and item.kind != body.equip_kind):
                raise _unprocessable("equipment_not_found")
        if new_key != relay.equip_key:
            if new_key is not None:
                other = (await db.execute(
                    select(HubRelay).where(HubRelay.grow_id == grow.id, HubRelay.equip_key == new_key,
                                           HubRelay.id != relay.id)
                )).scalar_one_or_none()
                if other is not None:
                    if not body.move:
                        return JSONResponse(status_code=409, content={
                            "detail": "equipment_in_use", "n": other.n, "hub_id": str(other.hub_device_id)})
                    other.equip_kind = other.equip_key = None
                    other.mode = "manual"
                    svc.mark_rebind(other, now)
                    if other.hub_device_id != hub.id:
                        other_hubs.add(other.hub_device_id)
                    await db.flush()
            relay.equip_kind = equip[new_key].kind if new_key else None
            relay.equip_key = new_key
            svc.mark_rebind(relay, now)
            if new_key is None or (relay.equip_kind == "light" and relay.mode == "humidity"):
                relay.mode = "manual"

    if body.schedule is not None:
        relay.schedule_on, relay.schedule_off = body.schedule.on, body.schedule.off
    if body.humidity is not None:
        relay.hum_on_above, relay.hum_off_below = body.humidity.on_above, body.humidity.off_below
        relay.min_on_s, relay.min_off_s = body.humidity.min_on_s, body.humidity.min_off_s
    if "failsafe" in fields:
        relay.failsafe = body.failsafe

    if body.mode is not None:
        if body.mode != "manual" and relay.equip_key is None:
            raise _unprocessable("relay_free")
        if body.mode == "humidity":
            if relay.equip_kind == "light":
                raise _unprocessable("mode_not_allowed")
            if relay.hum_on_above is None or relay.hum_off_below is None:
                raise _unprocessable("humidity_required")
        if body.mode == "schedule":
            if relay.equip_kind == "light" and svc.light_schedule(grow) is None:
                raise _unprocessable("photoperiod_required")
            if relay.equip_kind != "light" and not (relay.schedule_on and relay.schedule_off):
                raise _unprocessable("schedule_required")
        relay.mode = body.mode

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="equipment_in_use")

    try:
        await svc.publish_config(db, hub, grow, relays)
        for other_id in other_hubs:
            other_hub = await db.get(SensorDevice, other_id)
            if other_hub is not None:
                await svc.publish_config(db, other_hub, grow)
    except mqtt_broker.BrokerUnavailable:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="broker_unavailable")

    await db.commit()
    hub, grow = await _owned_hub(hub_id, current_user, db)
    return await relays_view(db, hub, grow)


@router.post("/devices/{hub_id}/relays/{n}/command", response_model=RelayCommandAccepted,
             status_code=status.HTTP_202_ACCEPTED)
async def relay_command(
    hub_id: uuid.UUID,
    n: int,
    body: RelayCommandIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Publica relay_set / relay_resume com req_id. A chave só muda quando o Hub
    confirmar (GET /iot/relay-commands/{req_id}); 10 s sem resposta = timeout."""
    _position(n)
    hub, _grow = await _owned_hub(hub_id, current_user, db)
    relay = (await svc.hub_relays(db, hub))[n - 1]
    if relay.equip_key is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="relay_free")
    if not svc.hub_online(hub):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="hub_offline")

    command = RelayCommand(
        req_id=uuid.uuid4(), hub_device_id=hub.id, user_id=current_user.id, n=n,
        action=body.action, requested_state=body.state if body.action == "set" else None,
        duration_s=body.duration_s if body.action == "set" else 0, status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(command)
    await db.flush()
    payload = {"cmd": "relay_resume", "req_id": str(command.req_id), "relay": n}
    if body.action == "set":
        payload = {"cmd": "relay_set", "req_id": str(command.req_id), "relay": n,
                   "state": body.state, "duration_s": body.duration_s}
    try:
        await mqtt_broker.publish_hub_command(hub.grow_id, svc.hub_mac(hub), payload)
    except mqtt_broker.BrokerUnavailable:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="broker_unavailable")
    await db.commit()
    return RelayCommandAccepted(req_id=command.req_id)


@router.get("/relay-commands/{req_id}", response_model=RelayCommandOut)
async def get_relay_command(
    req_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    command = (await db.execute(
        select(RelayCommand)
        .join(SensorDevice, RelayCommand.hub_device_id == SensorDevice.id)
        .join(Grow, SensorDevice.grow_id == Grow.id)
        .where(RelayCommand.req_id == req_id, Grow.user_id == current_user.id)
    )).scalar_one_or_none()
    if command is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comando não encontrado")
    return svc.command_view(command)
