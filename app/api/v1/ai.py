import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.knowledge import AIConversation
from app.models.plant import Plant
from app.models.user import User
from app.services.auth import get_current_user
from app.services.rag import chat
from app.services.subscription import check_ai_limit

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    grow_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    reply: str


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str | None
    grow_id: uuid.UUID | None
    plant_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    message_count: int

    model_config = {"from_attributes": True}


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    check_ai_limit(current_user)

    grow = None
    if body.grow_id:
        grow_result = await db.execute(
            select(Grow).where(Grow.id == body.grow_id, Grow.user_id == current_user.id)
        )
        grow = grow_result.scalar_one_or_none()
        if not grow:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow nao encontrado")

    plant = None
    if body.plant_id:
        plant_result = await db.execute(
            select(Plant).where(Plant.id == body.plant_id, Plant.user_id == current_user.id)
        )
        plant = plant_result.scalar_one_or_none()
        if not plant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planta nao encontrada")

    conversation = None
    if body.conversation_id:
        result = await db.execute(
            select(AIConversation).where(
                AIConversation.id == body.conversation_id,
                AIConversation.user_id == current_user.id,
            )
        )
        conversation = result.scalar_one_or_none()

    if not conversation:
        conversation = AIConversation(
            user_id=current_user.id,
            grow_id=body.grow_id,
            plant_id=body.plant_id,
            messages=[],
            title=body.message[:60],
        )
        db.add(conversation)
        await db.flush()

    reply = await chat(db, conversation, body.message, grow, plant)

    now_iso = datetime.now(timezone.utc).isoformat()
    conversation.messages = [
        *conversation.messages,
        {"role": "user", "content": body.message, "timestamp": now_iso},
        {"role": "assistant", "content": reply, "timestamp": now_iso},
    ]

    current_user.ai_queries_this_month += 1
    await db.commit()

    return ChatResponse(conversation_id=conversation.id, reply=reply)


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AIConversation)
        .where(AIConversation.user_id == current_user.id)
        .order_by(AIConversation.updated_at.desc())
        .limit(30)
    )
    conversations = result.scalars().all()
    return [
        ConversationSummary(
            id=c.id,
            title=c.title,
            grow_id=c.grow_id,
            plant_id=c.plant_id,
            created_at=c.created_at,
            updated_at=c.updated_at,
            message_count=len(c.messages),
        )
        for c in conversations
    ]


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AIConversation).where(
            AIConversation.id == conversation_id,
            AIConversation.user_id == current_user.id,
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversa nao encontrada")
    return conversation
