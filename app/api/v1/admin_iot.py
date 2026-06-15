"""
Endpoints admin para gerenciamento e simulacao de dispositivos IoT.

Routes
------
POST /admin/iot/simulate-discovery   Simula a descoberta de um satellite pelo hub,
                                     criando um SensorDevice pendente sem hardware fisico.
GET  /admin/iot/devices              Lista todos os SensorDevices do sistema (todos os grows),
                                     com paginacao simples.

Protecao: header X-Admin-Token (mesmo mecanismo de app/api/v1/admin.py).
"""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.sensor import SensorDevice

router = APIRouter()

# ---------------------------------------------------------------------------
# Schemas locais
# ---------------------------------------------------------------------------

_MODULE_SUBTYPES = Literal["solo", "ambiente", "ppfd", "nutricao", "automacao"]


class SimulateDiscoveryRequest(BaseModel):
    grow_id: uuid.UUID | None = Field(
        default=None,
        description="UUID do grow onde o dispositivo sera criado. Null = sem grow vinculado.",
    )
    hub_mac: str = Field(
        description="MAC do hub pai (12 chars hex sem separadores, ex: 112233445566).",
        min_length=12,
        max_length=12,
    )
    satellite_mac: str = Field(
        description="MAC do satellite a simular (12 chars hex sem separadores, ex: AABBCCDDEEFF).",
        min_length=12,
        max_length=12,
    )
    module_type: Literal["satellite", "hub", "standalone"] = Field(
        default="satellite",
        description="Tipo do modulo a registrar.",
    )
    module_subtype: _MODULE_SUBTYPES | None = Field(
        default=None,
        description="Subtipo do modulo — determina o nome gerado automaticamente.",
    )


class AdminDeviceResponse(BaseModel):
    """DeviceResponse estendido com grow_id (resolvido via planta)."""

    id: uuid.UUID
    esp32_mac: str
    name: str
    module_type: str | None
    hub_mac: str | None
    is_paired: bool
    is_online: bool
    plant_id: uuid.UUID | None
    # grow_id nao existe na tabela sensor_devices; e resolvido via JOIN com plants
    grow_id: uuid.UUID | None

    model_config = {"from_attributes": False}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_device_name(module_subtype: str | None, mac: str) -> str:
    """
    Gera o nome do dispositivo a partir do subtipo e do MAC.

    Exemplos:
      subtype=None        -> "Satellite AABBCCDDEEFF"
      subtype="solo"      -> "solo AABBCCDDEEFF"
    """
    prefix = module_subtype.capitalize() if module_subtype else "Satellite"
    return f"{prefix} {mac.upper()}"


async def _resolve_grow_id(
    device: SensorDevice,
    db: AsyncSession,
) -> uuid.UUID | None:
    """Retorna o grow_id da planta vinculada ao device, ou None."""
    if not device.plant_id:
        return None
    result = await db.execute(
        select(Plant.grow_id).where(Plant.id == device.plant_id)
    )
    row = result.scalar_one_or_none()
    return row  # pode ser None se a planta nao tiver grow


def _to_admin_response(device: SensorDevice, grow_id: uuid.UUID | None) -> AdminDeviceResponse:
    return AdminDeviceResponse(
        id=device.id,
        esp32_mac=device.esp32_mac,
        name=device.name,
        module_type=device.module_type,
        hub_mac=device.hub_mac,
        is_paired=device.is_paired,
        is_online=device.is_online,
        plant_id=device.plant_id,
        grow_id=grow_id,
    )


# ---------------------------------------------------------------------------
# POST /admin/iot/simulate-discovery
# ---------------------------------------------------------------------------

@router.post(
    "/iot/simulate-discovery",
    response_model=AdminDeviceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Simula descoberta de dispositivo IoT (sem hardware fisico)",
)
async def simulate_discovery(
    body: SimulateDiscoveryRequest,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Cria um SensorDevice no estado 'descoberto mas nao pareado', exatamente
    como o hub faria ao detectar um satellite via ESP-NOW.

    Comportamento:
    - Se grow_id for informado, valida que o grow existe.
    - Se um device com o mesmo satellite_mac ja existir, retorna o existente
      (operacao idempotente).
    - O device e criado com is_paired=False para simular o fluxo de pairing
      real pelo app mobile.
    """
    # 1. Valida grow_id (opcional)
    if body.grow_id is not None:
        grow_result = await db.execute(
            select(Grow).where(Grow.id == body.grow_id)
        )
        if not grow_result.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Grow '{body.grow_id}' nao encontrado.",
            )

    # 2. Idempotencia: retorna existente se MAC ja cadastrado
    existing_result = await db.execute(
        select(SensorDevice).where(
            SensorDevice.esp32_mac == body.satellite_mac.upper()
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing:
        grow_id = await _resolve_grow_id(existing, db)
        return _to_admin_response(existing, grow_id)

    # 3. Cria o device pendente
    device_name = _build_device_name(body.module_subtype, body.satellite_mac)

    device = SensorDevice(
        esp32_mac=body.satellite_mac.upper(),
        name=device_name,
        module_type=body.module_type,
        hub_mac=body.hub_mac.upper(),
        is_paired=False,
        sensors_config={},
        # plant_id fica None: o grower vai parear pelo app mobile (PATCH /iot/devices/{id})
        plant_id=None,
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)

    # grow_id e None porque plant_id e None no momento da criacao
    return _to_admin_response(device, None)


# ---------------------------------------------------------------------------
# GET /admin/iot/devices
# ---------------------------------------------------------------------------

@router.get(
    "/iot/devices",
    response_model=list[AdminDeviceResponse],
    summary="Lista todos os SensorDevices do sistema (admin)",
)
async def list_all_devices(
    skip: int = Query(default=0, ge=0, description="Numero de registros a pular."),
    limit: int = Query(default=50, ge=1, le=500, description="Maximo de registros a retornar."),
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Retorna todos os dispositivos IoT registrados no sistema, independente de grow
    ou usuario. Inclui dispositivos pendentes (is_paired=False) e pareados.

    Paginacao: skip + limit.
    grow_id e resolvido via JOIN com a tabela plants.
    """
    # Busca devices com o grow_id da planta vinculada via LEFT JOIN
    stmt = (
        select(SensorDevice, Plant.grow_id)
        .outerjoin(Plant, SensorDevice.plant_id == Plant.id)
        .order_by(SensorDevice.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.all()

    return [
        _to_admin_response(device, grow_id)
        for device, grow_id in rows
    ]
