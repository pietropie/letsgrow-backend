"""
Registro manual do ambiente — temperatura, umidade e nota sem exigir planta.

POST   /grows/{grow_id}/readings
GET    /grows/{grow_id}/readings
GET    /grows/{grow_id}/readings/{reading_id}
PATCH  /grows/{grow_id}/readings/{reading_id}
DELETE /grows/{grow_id}/readings/{reading_id}

As leituras entram no resolvedor de medicoes do ambiente (grow_env) como origem
"manual". Registrar nao consome creditos.
"""
import math
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.environment_reading import EnvironmentReading
from app.models.grow import Grow
from app.models.user import User
from app.services.auth import get_current_user

router = APIRouter()

# Tolerancia para relogio do aparelho levemente adiantado.
_FUTURE_SKEW = timedelta(minutes=5)


def _check_observed_at(value: datetime | None) -> datetime | None:
    if value is None:
        return value
    if value.tzinfo is None:
        raise ValueError("observed_at precisa de fuso horário")
    if value > datetime.now(timezone.utc) + _FUTURE_SKEW:
        raise ValueError("observed_at não pode estar no futuro")
    return value


def _check_value(value: float | None) -> float | None:
    if value is not None and not math.isfinite(value):
        raise ValueError("valor inválido")
    return value


def _clean_notes(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class _ReadingFields(BaseModel):
    temperature_c: float | None = Field(default=None, ge=-20, le=60)
    humidity_rh: float | None = Field(default=None, ge=0, le=100)
    notes: str | None = Field(default=None, max_length=2000)

    model_config = {"extra": "forbid"}

    @field_validator("observed_at", check_fields=False)
    @classmethod
    def _observed(cls, value):
        return _check_observed_at(value)

    @field_validator("temperature_c", "humidity_rh")
    @classmethod
    def _values(cls, value):
        return _check_value(value)

    @field_validator("notes")
    @classmethod
    def _notes(cls, value):
        return _clean_notes(value)


class EnvironmentReadingCreate(_ReadingFields):
    observed_at: datetime

    @model_validator(mode="after")
    def _not_empty(self):
        if self.temperature_c is None and self.humidity_rh is None and not self.notes:
            raise ValueError("Informe temperatura, umidade ou uma nota")
        return self


class EnvironmentReadingUpdate(_ReadingFields):
    observed_at: datetime | None = None


class EnvironmentReadingResponse(BaseModel):
    id: uuid.UUID
    grow_id: uuid.UUID
    observed_at: datetime
    temperature_c: float | None
    humidity_rh: float | None
    notes: str | None
    # manual = registrado no app | exhaust_power = registro automatico do dimmer (somente leitura)
    kind: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class EnvironmentReadingPage(BaseModel):
    items: list[EnvironmentReadingResponse]
    total: int


async def _own_grow(db: AsyncSession, grow_id: uuid.UUID, user_id: uuid.UUID) -> Grow:
    grow = (
        await db.execute(select(Grow).where(Grow.id == grow_id, Grow.user_id == user_id))
    ).scalar_one_or_none()
    if grow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")
    return grow


async def _own_reading(
    db: AsyncSession, grow_id: uuid.UUID, reading_id: uuid.UUID, user_id: uuid.UUID
) -> EnvironmentReading:
    reading = (
        await db.execute(
            select(EnvironmentReading).where(
                EnvironmentReading.id == reading_id,
                EnvironmentReading.grow_id == grow_id,
                EnvironmentReading.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if reading is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Registro não encontrado")
    return reading


def _require_manual(reading: EnvironmentReading) -> None:
    if reading.kind != "manual":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Registro automático da potência do exaustor: não pode ser editado nem apagado. "
                   "Para mudar, ajuste a potência no painel do ambiente.",
        )


@router.post(
    "/{grow_id}/readings",
    response_model=EnvironmentReadingResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_reading(
    grow_id: uuid.UUID,
    body: EnvironmentReadingCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    grow = await _own_grow(db, grow_id, current_user.id)
    if grow.status == "abandoned":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Ambiente arquivado")
    now = datetime.now(timezone.utc)
    reading = EnvironmentReading(
        id=uuid.uuid4(),
        grow_id=grow.id,
        user_id=current_user.id,
        kind="manual",
        created_at=now,
        updated_at=now,
        **body.model_dump(),
    )
    db.add(reading)
    await db.commit()
    await db.refresh(reading)
    return reading


@router.get("/{grow_id}/readings", response_model=EnvironmentReadingPage)
async def list_readings(
    grow_id: uuid.UUID,
    start: datetime | None = Query(default=None, description="Inclusivo, com fuso"),
    end: datetime | None = Query(default=None, description="Exclusivo, com fuso"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _own_grow(db, grow_id, current_user.id)
    for value in (start, end):
        if value is not None and value.tzinfo is None:
            raise HTTPException(status_code=422, detail="start/end precisam de fuso horário")
    conditions = [EnvironmentReading.grow_id == grow_id, EnvironmentReading.user_id == current_user.id]
    if start is not None:
        conditions.append(EnvironmentReading.observed_at >= start)
    if end is not None:
        conditions.append(EnvironmentReading.observed_at < end)
    total = (await db.execute(select(func.count(EnvironmentReading.id)).where(*conditions))).scalar() or 0
    rows = (
        await db.execute(
            select(EnvironmentReading)
            .where(*conditions)
            .order_by(EnvironmentReading.observed_at.desc(), EnvironmentReading.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return EnvironmentReadingPage(items=list(rows), total=total)


@router.get("/{grow_id}/readings/{reading_id}", response_model=EnvironmentReadingResponse)
async def get_reading(
    grow_id: uuid.UUID,
    reading_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _own_reading(db, grow_id, reading_id, current_user.id)


@router.patch("/{grow_id}/readings/{reading_id}", response_model=EnvironmentReadingResponse)
async def update_reading(
    grow_id: uuid.UUID,
    reading_id: uuid.UUID,
    body: EnvironmentReadingUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    reading = await _own_reading(db, grow_id, reading_id, current_user.id)
    _require_manual(reading)
    changes = body.model_dump(exclude_unset=True)
    if "observed_at" in changes and changes["observed_at"] is None:
        raise HTTPException(status_code=422, detail="observed_at não pode ser removido")
    for field, value in changes.items():
        setattr(reading, field, value)
    if reading.temperature_c is None and reading.humidity_rh is None and not reading.notes:
        raise HTTPException(status_code=422, detail="Informe temperatura, umidade ou uma nota")
    reading.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(reading)
    return reading


@router.delete("/{grow_id}/readings/{reading_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reading(
    grow_id: uuid.UUID,
    reading_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    reading = await _own_reading(db, grow_id, reading_id, current_user.id)
    _require_manual(reading)
    await db.delete(reading)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
