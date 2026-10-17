"""
Bobby: correcao de contexto e memoria editavel pelo usuario.

POST   /bob/feedback            – "Ajude a corrigir o contexto" (sem IA, sem creditos)
GET    /bob/memories            – preferencias guardadas
POST   /bob/memories            – nova preferencia ("Informada por voce")
PATCH  /bob/memories/{id}       – editar
DELETE /bob/memories/{id}       – esquecer (apaga de fato)

As preferencias entram no prompt do chat (app/rag/prompts.build_system_prompt).
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.ai_config import AIConfig
from app.models.bob import BobFeedback, BobMemory
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.user import User
from app.services.auth import get_current_user
from app.services.guard import guard_user_message

router = APIRouter()

MAX_MEMORIES = 20


def _strip_required(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Texto vazio")
    return value


class FeedbackCreate(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    alert_id: str | None = Field(default=None, max_length=200)
    plant_id: uuid.UUID | None = None
    grow_id: uuid.UUID | None = None
    evidence_ids: list[str] | None = Field(default=None, max_length=20)

    model_config = {"extra": "forbid"}

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        return _strip_required(value)


class FeedbackCreated(BaseModel):
    id: uuid.UUID
    created_at: datetime


class MemoryWrite(BaseModel):
    content: str = Field(min_length=1, max_length=300)

    model_config = {"extra": "forbid"}

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        return _strip_required(value)


class MemoryResponse(BaseModel):
    id: uuid.UUID
    content: str
    source: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


@router.post("/feedback", response_model=FeedbackCreated, status_code=status.HTTP_201_CREATED)
async def create_feedback(
    body: FeedbackCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.plant_id is not None:
        owned = (
            await db.execute(select(Plant.id).where(Plant.id == body.plant_id, Plant.user_id == current_user.id))
        ).scalar_one_or_none()
        if owned is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planta não encontrada")
    if body.grow_id is not None:
        owned = (
            await db.execute(select(Grow.id).where(Grow.id == body.grow_id, Grow.user_id == current_user.id))
        ).scalar_one_or_none()
        if owned is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")

    feedback = BobFeedback(
        id=uuid.uuid4(),
        user_id=current_user.id,
        alert_id=body.alert_id,
        plant_id=body.plant_id,
        grow_id=body.grow_id,
        evidence_ids=[item[:100] for item in body.evidence_ids] if body.evidence_ids else None,
        message=body.message,
        status="new",
        created_at=datetime.now(timezone.utc),
    )
    db.add(feedback)
    await db.commit()
    return FeedbackCreated(id=feedback.id, created_at=feedback.created_at)


async def _guarded(db: AsyncSession, user: User, content: str) -> str:
    """Preferencias vao para o prompt: passam pela mesma protecao das mensagens."""
    config = (await db.execute(select(AIConfig).order_by(AIConfig.created_at.asc()).limit(1))).scalar_one_or_none()
    return guard_user_message(
        content,
        user_id=str(user.id),
        injection_message=config.injection_message if config else None,
    )[:300]


async def _own_memory(db: AsyncSession, memory_id: uuid.UUID, user_id: uuid.UUID) -> BobMemory:
    memory = (
        await db.execute(select(BobMemory).where(BobMemory.id == memory_id, BobMemory.user_id == user_id))
    ).scalar_one_or_none()
    if memory is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preferência não encontrada")
    return memory


@router.get("/memories", response_model=list[MemoryResponse])
async def list_memories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(BobMemory)
            .where(BobMemory.user_id == current_user.id)
            .order_by(BobMemory.created_at.asc(), BobMemory.id.asc())
        )
    ).scalars().all()
    return list(rows)


@router.post("/memories", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def create_memory(
    body: MemoryWrite,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count = (
        await db.execute(select(func.count(BobMemory.id)).where(BobMemory.user_id == current_user.id))
    ).scalar() or 0
    if count >= MAX_MEMORIES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Limite de {MAX_MEMORIES} preferências atingido. Remova uma antes de adicionar outra.",
        )
    now = datetime.now(timezone.utc)
    memory = BobMemory(
        id=uuid.uuid4(),
        user_id=current_user.id,
        content=await _guarded(db, current_user, body.content),
        source="user",
        created_at=now,
        updated_at=now,
    )
    db.add(memory)
    await db.commit()
    await db.refresh(memory)
    return memory


@router.patch("/memories/{memory_id}", response_model=MemoryResponse)
async def update_memory(
    memory_id: uuid.UUID,
    body: MemoryWrite,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    memory = await _own_memory(db, memory_id, current_user.id)
    memory.content = await _guarded(db, current_user, body.content)
    memory.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(memory)
    return memory


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_memory(
    memory_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    memory = await _own_memory(db, memory_id, current_user.id)
    await db.delete(memory)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
