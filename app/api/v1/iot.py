"""
IoT device management endpoints.

Routes
------
GET    /iot/devices                 List all devices owned by the current user
                                    (both paired and pending/unassigned).
POST   /iot/devices                 Manually register a new device.
GET    /iot/devices/{device_id}     Retrieve a single device.
PATCH  /iot/devices/{device_id}     Assign / rename a device (confirm pairing).
DELETE /iot/devices/{device_id}     Remove a device.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.sensor import SensorDevice
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.sensor import DevicePatch, DeviceRegister, DeviceResponse
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
        hub_result = await db.execute(
            select(SensorDevice)
            .join(Plant, SensorDevice.plant_id == Plant.id)
            .where(
                SensorDevice.esp32_mac == device.hub_mac,
                Plant.user_id == current_user.id,
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

def _device_to_response(device: SensorDevice, grow_id: uuid.UUID | None = None) -> DeviceResponse:
    """Build DeviceResponse. grow_id stored on the device, with plant fallback."""
    return DeviceResponse(
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
    paired_devices = [_device_to_response(d) for d in paired_rows]

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
        pending_devices = [_device_to_response(d, None) for d in pending_rows]

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
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")

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
    return _device_to_response(device)


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
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")
        device.grow_id = body.grow_id
        device.is_paired = True

    if body.name is not None:
        device.name = body.name

    if body.module_type is not None:
        device.module_type = body.module_type

    if body.sensors_config is not None:
        device.sensors_config = body.sensors_config

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
    await db.delete(device)
    await db.commit()
    return MessageResponse(message="Dispositivo removido")
