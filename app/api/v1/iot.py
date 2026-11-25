"""
IoT device management endpoints.

Routes
------
GET    /iot/devices                 List all devices owned by the current user
                                    (both paired and pending/unassigned).
POST   /iot/devices                 Manually register a new device.
GET    /iot/devices/{device_id}     Retrieve a single device.
PATCH  /iot/devices/{device_id}     Assign / rename a device (confirm pairing).
                                    Confirming a pending satellite also publishes
                                    pair_confirm to its Hub.
DELETE /iot/devices/{device_id}     Remove a device.
POST   /iot/devices/{hub_id}/pairing  Open the Hub's satellite pairing window (pair_open).
POST   /iot/hubs/claim-codes        Claim code for a new Hub (BLE onboarding).
POST   /iot/hubs/activate           Hub trades the claim code for its MQTT password (no login).
GET    /iot/grows/{grow_id}/sensors Sensors screen: Hub, cards with ideal ranges and mini history.
Relays (app/api/v1/relays.py): /iot/devices/{hub_id}/relays..., /iot/relay-commands/{req_id}.

Contract: side-session note hub-onboarding-ble-contrato.md.
"""
import hashlib
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.sensor import HubClaimCode, SensorDevice
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.sensor import (
    DevicePatch,
    DeviceRegister,
    DeviceResponse,
    HubActivateRequest,
    HubActivateResponse,
    HubClaimCodeCreate,
    HubClaimCodeResponse,
    HubPairingOpen,
    HubPairingResponse,
)
from app.services import mqtt_broker, rate_limit, sensor_catalog
from app.services import relays as relay_svc
from app.services import sensor_panel
from app.schemas.sensor_panel import SensorPanelOut
from app.services.auth import get_current_user
from app.services.subscription import check_sensor_access

router = APIRouter()


# ---------------------------------------------------------------------------
# Helper: resolve ownership for a device
# ---------------------------------------------------------------------------

async def _get_owned_device(
    device_id: uuid.UUID,
    current_user: User,
    db: AsyncSession,
) -> SensorDevice:
    """
    Return the SensorDevice if it belongs to the current user.

    Ownership rules:
    - Paired device   -> plant_id IS NOT NULL and Plant.user_id == current_user.id
    - Pending device  -> is_paired=False, plant_id IS NULL.
                         We verify ownership via hub_mac: the hub device must be
                         owned by the user.  If no hub is known (standalone
                         discovery edge-case) we still surface it so the user
                         can claim it.
    """
    # Try the paired path first (fast-path for normal devices)
    result = await db.execute(
        select(SensorDevice)
        .outerjoin(Plant, SensorDevice.plant_id == Plant.id)
        .where(
            SensorDevice.id == device_id,
            or_(
                Plant.user_id == current_user.id,
                SensorDevice.grow_id.in_(
                    select(Grow.id).where(Grow.user_id == current_user.id)
                ),
                SensorDevice.is_paired.is_(False),
            ),
        )
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Dispositivo nao encontrado",
        )

    # Extra ownership check for pending satellites: confirm hub is owned by user
    if not device.is_paired and device.hub_mac:
        # Hub ativado pelo app fica ligado ao grow (sem planta); o legado, à planta
        hub_result = await db.execute(
            select(SensorDevice)
            .outerjoin(Plant, SensorDevice.plant_id == Plant.id)
            .outerjoin(Grow, SensorDevice.grow_id == Grow.id)
            .where(
                SensorDevice.esp32_mac == device.hub_mac,
                or_(Plant.user_id == current_user.id, Grow.user_id == current_user.id),
            )
        )
        if not hub_result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Dispositivo nao encontrado",
            )

    return device


# ---------------------------------------------------------------------------
# GET /iot/devices
# ---------------------------------------------------------------------------

def _device_to_response(
    device: SensorDevice, grow_id: uuid.UUID | None = None, photos: dict[str, str] | None = None
) -> DeviceResponse:
    """Build DeviceResponse. grow_id stored on the device, with plant fallback."""
    kind = sensor_catalog.device_kind(device)
    meta = sensor_catalog.KINDS.get(kind) if kind else None
    return DeviceResponse(
        sensor_kind=kind,
        kind_label=meta.label if meta else None,
        description=meta.description if meta else None,
        icon=meta.icon if meta else None,
        photo_url=sensor_panel.photo_for(device, photos or {}),
        mains_voltage=device.mains_voltage,
        id=device.id,
        plant_id=device.plant_id,
        grow_id=grow_id if grow_id is not None else device.grow_id,
        name=device.name,
        esp32_mac=device.esp32_mac,
        firmware_version=device.firmware_version,
        sensors_config=device.sensors_config,
        module_type=device.module_type,
        hub_mac=device.hub_mac,
        is_paired=device.is_paired,
        is_online=device.is_online,
        last_seen_at=device.last_seen_at,
        created_at=device.created_at,
    )


@router.get("/devices", response_model=list[DeviceResponse])
async def list_devices(
    grow_id: uuid.UUID | None = None,
    paired_only: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    List all sensor devices visible to the authenticated user.

    Query params:
    - grow_id     Filter devices whose plant belongs to this grow.
    - paired_only If true, return only devices already assigned to a plant.

    Pending satellites (is_paired=False) are included by default so the mobile
    app can display a "new device detected" flow.  They are identified via the
    hub ownership chain.

    grow_id is resolved server-side via plant.grow_id and included in each
    DeviceResponse so the mobile can filter by grow without a second query.
    """
    paired_q = (
        select(SensorDevice)
        .outerjoin(Plant, SensorDevice.plant_id == Plant.id)
        .outerjoin(Grow, SensorDevice.grow_id == Grow.id)
        .where(
            or_(
                Plant.user_id == current_user.id,
                Grow.user_id == current_user.id,
            )
        )
    )
    if grow_id:
        paired_q = paired_q.where(
            or_(SensorDevice.grow_id == grow_id, Plant.grow_id == grow_id)
        )
    if paired_only:
        paired_q = paired_q.where(SensorDevice.is_paired.is_(True))

    paired_rows = (await db.execute(paired_q.order_by(SensorDevice.created_at.desc()))).scalars().all()
    photos = await sensor_panel.photo_urls(db)
    paired_devices = [_device_to_response(d, photos=photos) for d in paired_rows]

    if paired_only:
        return paired_devices

    # Hub MACs owned by this user (for pending satellite discovery)
    hub_macs = [d.esp32_mac for d in paired_rows if d.module_type == "hub"]

    # Pending satellites: not yet paired, hub_mac links to one of the user's hubs
    pending_devices: list[DeviceResponse] = []
    if hub_macs:
        pending_rows = (
            await db.execute(
                select(SensorDevice)
                .where(
                    SensorDevice.is_paired.is_(False),
                    SensorDevice.hub_mac.in_(hub_macs),
                )
                .order_by(SensorDevice.created_at.desc())
            )
        ).scalars().all()
        # Pending devices don't have a plant yet → grow_id is None
        pending_devices = [_device_to_response(d, None, photos) for d in pending_rows]

    return paired_devices + pending_devices


# ---------------------------------------------------------------------------
# POST /iot/devices
# ---------------------------------------------------------------------------

@router.post("/devices", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def register_device(
    body: DeviceRegister,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually register a new device (hub or standalone)."""
    check_sensor_access(current_user)

    resolved_grow_id = body.grow_id
    if body.plant_id:
        plant_result = await db.execute(
            select(Plant).where(
                Plant.id == body.plant_id,
                Plant.user_id == current_user.id,
            )
        )
        plant = plant_result.scalar_one_or_none()
        if not plant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Planta nao encontrada",
            )
        if resolved_grow_id is None:
            resolved_grow_id = plant.grow_id

    if resolved_grow_id:
        grow_result = await db.execute(
            select(Grow).where(Grow.id == resolved_grow_id, Grow.user_id == current_user.id)
        )
        if not grow_result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambiente não encontrado")

    mac_result = await db.execute(
        select(SensorDevice).where(SensorDevice.esp32_mac == body.esp32_mac)
    )
    if mac_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Dispositivo ja registrado",
        )

    device = SensorDevice(
        plant_id=body.plant_id,
        grow_id=resolved_grow_id,
        name=body.name,
        esp32_mac=body.esp32_mac,
        sensors_config=body.sensors_config,
        module_type=body.module_type or "standalone",
        hub_mac=body.hub_mac,
        # Manually registered devices are considered paired immediately
        is_paired=True,
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return _device_to_response(device)


# ---------------------------------------------------------------------------
# GET /iot/devices/{device_id}
# ---------------------------------------------------------------------------

@router.get("/devices/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_owned_device(device_id, current_user, db)
    return _device_to_response(device, photos=await sensor_panel.photo_urls(db))


# ---------------------------------------------------------------------------
# PATCH /iot/devices/{device_id}
# ---------------------------------------------------------------------------

@router.patch("/devices/{device_id}", response_model=DeviceResponse)
async def patch_device(
    device_id: uuid.UUID,
    body: DevicePatch,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Assign a detected satellite to a plant and/or rename it.

    Typical pairing flow:
      1. Hub detects satellite -> publishes discovery -> backend creates pending device.
      2. Mobile app calls GET /iot/devices to list pending devices.
      3. User picks a plant and sends:
            PATCH /iot/devices/{id}
            {"plant_id": "<uuid>", "name": "Sensor solo vaso 1"}
      4. Backend links device to plant and marks is_paired=True.
    """
    device = await _get_owned_device(device_id, current_user, db)
    was_pending = not device.is_paired

    if body.plant_id is not None:
        plant_result = await db.execute(
            select(Plant).where(
                Plant.id == body.plant_id,
                Plant.user_id == current_user.id,
            )
        )
        plant = plant_result.scalar_one_or_none()
        if not plant:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Planta nao encontrada",
            )
        device.plant_id = body.plant_id
        device.grow_id = plant.grow_id
        device.is_paired = True

    if body.grow_id is not None:
        grow_result = await db.execute(
            select(Grow).where(Grow.id == body.grow_id, Grow.user_id == current_user.id)
        )
        if not grow_result.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambiente não encontrado")
        device.grow_id = body.grow_id
        device.is_paired = True

    if body.name is not None:
        device.name = body.name

    if body.module_type is not None:
        device.module_type = body.module_type

    if body.sensors_config is not None:
        device.sensors_config = body.sensors_config

    if "sensor_kind" in body.model_fields_set:
        device.sensor_kind = body.sensor_kind

    if "mains_voltage" in body.model_fields_set:
        if device.module_type != "hub":
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="mains_voltage_hub_only")
        device.mains_voltage = body.mains_voltage

    # Satélite pendente confirmado: avisa o Hub (pair_confirm) antes de gravar,
    # para o app poder tentar de novo se o broker estiver fora do ar.
    if was_pending and device.is_paired and device.module_type != "hub" and device.hub_mac:
        hub = await _hub_by_mac(device.hub_mac, db)
        hub_mac = mqtt_broker.normalize_mac(hub.esp32_mac) if hub is not None else None
        if hub is not None and hub.grow_id is not None and hub_mac:
            command = {"cmd": "pair_confirm", "mac": mqtt_broker.normalize_mac(device.esp32_mac) or device.esp32_mac}
            try:
                await mqtt_broker.publish_hub_command(hub.grow_id, hub_mac, command)
            except mqtt_broker.BrokerUnavailable:
                await db.rollback()
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="broker_unavailable")

    await db.commit()
    await db.refresh(device)
    return _device_to_response(device)


# ---------------------------------------------------------------------------
# DELETE /iot/devices/{device_id}
# ---------------------------------------------------------------------------

@router.delete("/devices/{device_id}", response_model=MessageResponse)
async def delete_device(
    device_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    device = await _get_owned_device(device_id, current_user, db)
    if device.module_type == "hub":
        await relay_svc.detach_hub(db, device, device.grow_id)
    await db.delete(device)
    await db.commit()
    return MessageResponse(message="Dispositivo removido")


# ---------------------------------------------------------------------------
# Hub: comandos MQTT e onboarding por BLE
# ---------------------------------------------------------------------------

CLAIM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CLAIM_CODE_LENGTH = 8
CLAIM_CODE_TTL = timedelta(minutes=15)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _new_claim_code() -> str:
    return "".join(secrets.choice(CLAIM_CODE_ALPHABET) for _ in range(CLAIM_CODE_LENGTH))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _hub_by_mac(hub_mac: str, db: AsyncSession) -> SensorDevice | None:
    """Hub pelo MAC, aceitando o formato legado com ':'."""
    mac = mqtt_broker.normalize_mac(hub_mac)
    candidates = {hub_mac, hub_mac.upper()}
    if mac:
        candidates |= {mac, ":".join(mac[i:i + 2] for i in range(0, 12, 2))}
    return (
        await db.execute(
            select(SensorDevice).where(SensorDevice.esp32_mac.in_(candidates)).limit(1)
        )
    ).scalar_one_or_none()


@router.post("/devices/{hub_id}/pairing", response_model=HubPairingResponse)
async def open_hub_pairing(
    hub_id: uuid.UUID,
    body: HubPairingOpen | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Abre a janela de pareamento de satélites do Hub (publica pair_open no cmd dele)."""
    ttl_s = (body or HubPairingOpen()).ttl_s
    hub = (
        await db.execute(
            select(SensorDevice)
            .join(Grow, SensorDevice.grow_id == Grow.id)
            .where(SensorDevice.id == hub_id, Grow.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    if hub is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispositivo nao encontrado")
    mac = mqtt_broker.normalize_mac(hub.esp32_mac)
    if hub.module_type != "hub" or mac is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Dispositivo nao e um Hub")
    try:
        await mqtt_broker.publish_hub_command(hub.grow_id, mac, {"cmd": "pair_open", "ttl_s": ttl_s})
    except mqtt_broker.BrokerUnavailable:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="broker_unavailable")
    return HubPairingResponse(
        hub_id=hub.id,
        ttl_s=ttl_s,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_s),
    )


@router.post("/hubs/claim-codes", response_model=HubClaimCodeResponse, status_code=status.HTTP_201_CREATED)
async def create_hub_claim_code(
    body: HubClaimCodeCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Código de vínculo (8 chars, 15 min, uso único) que o app manda ao Hub por BLE."""
    check_sensor_access(current_user)
    grow = (
        await db.execute(select(Grow).where(Grow.id == body.grow_id, Grow.user_id == current_user.id))
    ).scalar_one_or_none()
    if grow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambiente não encontrado")

    expires_at = datetime.now(timezone.utc) + CLAIM_CODE_TTL
    for _ in range(5):  # 32^8 combinações: colisão é quase impossível, mas o hash é único
        code = _new_claim_code()
        code_hash = _hash_code(code)
        taken = (
            await db.execute(select(HubClaimCode.id).where(HubClaimCode.code_hash == code_hash))
        ).scalar_one_or_none()
        if taken is None:
            break
    else:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Tente de novo")

    db.add(HubClaimCode(code_hash=code_hash, user_id=current_user.id, grow_id=grow.id, expires_at=expires_at))
    await db.commit()
    return HubClaimCodeResponse(code=code, expires_at=expires_at)


async def _activation_wait(ip: str, mac: str | None) -> int | None:
    """Conta a tentativa de ativação por IP e por MAC; devolve os segundos de espera se estourou."""
    if not settings.RATE_LIMIT_ENABLED:
        return None
    now = time.time()
    checks = [("ip", ip, rate_limit.parse_rule(settings.RATE_LIMIT_HUB_ACTIVATE_IP))]
    if mac:
        checks.append(("mac", mac, rate_limit.parse_rule(settings.RATE_LIMIT_HUB_ACTIVATE_MAC)))
    wait = None
    for kind, subject, rule in checks:
        window = int(now // rule.seconds)
        count = await rate_limit._hit(f"rl:hub_activate:{kind}:{subject}:{rule.seconds}:{window}", rule.seconds, now)
        if count > rule.count:
            wait = max(wait or 0, rule.seconds - int(now % rule.seconds))
    return wait


def _error(code: int, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=code, content={"detail": detail}, headers=headers)


@router.post("/hubs/activate", response_model=HubActivateResponse)
async def activate_hub(
    body: HubActivateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Chamado pelo Hub (HTTPS, sem login): troca o código de vínculo pela senha MQTT.

    Erros: 400 bad_request | code_invalid, 410 code_expired, 429 too_many_attempts,
    503 broker_unavailable (o código não é consumido; o Hub pode tentar de novo).
    """
    mac = mqtt_broker.normalize_mac(body.mac)
    headers = request.headers
    ip = rate_limit.client_ip(
        request.client.host if request.client else None,
        headers.get("x-forwarded-for"),
        headers.get("x-letsgrow-bff-key"),
        headers.get("x-letsgrow-client-ip"),
    )
    wait = await _activation_wait(ip, mac)
    if wait is not None:
        return _error(429, "too_many_attempts", {"Retry-After": str(wait)})

    code = (body.code or "").strip().upper()
    if mac is None or len(code) != CLAIM_CODE_LENGTH:
        return _error(400, "bad_request")
    if any(ch not in CLAIM_CODE_ALPHABET for ch in code):
        return _error(400, "code_invalid")

    claim = (
        await db.execute(
            select(HubClaimCode).where(HubClaimCode.code_hash == _hash_code(code)).with_for_update()
        )
    ).scalar_one_or_none()
    if claim is None or claim.used_at is not None:
        return _error(400, "code_invalid")
    now = datetime.now(timezone.utc)
    if _aware(claim.expires_at) <= now:
        return _error(410, "code_expired")

    device = await _hub_by_mac(mac, db)
    if device is None:
        device = SensorDevice(name=f"Hub {mac[-4:]}", esp32_mac=mac, sensors_config={})
        db.add(device)
    elif device.grow_id != claim.grow_id:
        # Hub mudou de grow/dono: a planta antiga e os relés do ambiente antigo não valem mais
        device.plant_id = None
        await relay_svc.detach_hub(db, device, device.grow_id)
    device.esp32_mac = mac
    device.grow_id = claim.grow_id
    device.module_type = "hub"
    device.hub_mac = None
    device.is_paired = True
    if body.fw:
        device.firmware_version = body.fw[:20]

    password = mqtt_broker.generate_mqtt_password()
    try:
        await mqtt_broker.upsert_hub_client(mac, claim.grow_id, password)
    except mqtt_broker.BrokerUnavailable:
        await db.rollback()
        return _error(503, "broker_unavailable")

    claim.used_at = now
    claim.used_by_mac = mac
    await db.commit()
    return HubActivateResponse(
        grow_id=claim.grow_id,
        mqtt_password=password,
        mqtt_host=settings.MQTT_PUBLIC_HOST,
        mqtt_port=settings.MQTT_PUBLIC_PORT,
    )


# ---------------------------------------------------------------------------
# Tela de Sensores do ambiente (UX item 28)
# ---------------------------------------------------------------------------

@router.get("/grows/{grow_id}/sensors", response_model=SensorPanelOut)
async def grow_sensors(
    grow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hub e um cartão por tipo medido, com a leitura avaliada contra a faixa ideal
    (fora da faixa primeiro), "fora da faixa desde" e o mini-histórico do tipo."""
    grow = (
        await db.execute(select(Grow).where(Grow.id == grow_id, Grow.user_id == current_user.id))
    ).scalar_one_or_none()
    if grow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambiente não encontrado")
    return await sensor_panel.build_panel(db, grow)


# Relés do Hub (/iot/devices/{hub_id}/relays..., /iot/relay-commands/{req_id})
from app.api.v1 import relays as _relays  # noqa: E402

router.include_router(_relays.router)
