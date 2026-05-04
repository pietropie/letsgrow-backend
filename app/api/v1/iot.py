import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.sensor import SensorDevice
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.sensor import DeviceRegister, DeviceResponse
from app.services.auth import get_current_user

router = APIRouter()


@router.get("/devices", response_model=list[DeviceResponse])
async def list_devices(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SensorDevice)
        .join(Grow, SensorDevice.grow_id == Grow.id)
        .where(Grow.user_id == current_user.id)
        .order_by(SensorDevice.created_at.desc())
    )
    return result.scalars().all()


@router.post("/devices", response_model=DeviceResponse, status_code=status.HTTP_201_CREATED)
async def register_device(
    body: DeviceRegister,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify grow belongs to user
    grow_result = await db.execute(
        select(Grow).where(Grow.id == body.grow_id, Grow.user_id == current_user.id)
    )
    if not grow_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")

    # Check MAC uniqueness
    mac_result = await db.execute(
        select(SensorDevice).where(SensorDevice.esp32_mac == body.esp32_mac)
    )
    if mac_result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Dispositivo já registrado")

    device = SensorDevice(**body.model_dump())
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return device


@router.get("/devices/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SensorDevice)
        .join(Grow, SensorDevice.grow_id == Grow.id)
        .where(SensorDevice.id == device_id, Grow.user_id == current_user.id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispositivo não encontrado")
    return device


@router.delete("/devices/{device_id}", response_model=MessageResponse)
async def delete_device(
    device_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(SensorDevice)
        .join(Grow, SensorDevice.grow_id == Grow.id)
        .where(SensorDevice.id == device_id, Grow.user_id == current_user.id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispositivo não encontrado")

    await db.delete(device)
    await db.commit()
    return MessageResponse(message="Dispositivo removido")
