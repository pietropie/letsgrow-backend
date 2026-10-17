import logging
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.credits import start_paid_task
from app.database import get_db
from app.models.ai_config import AIConfig
from app.models.credits import CreditQuote
from app.models.event import GrowEvent
from app.models.grow import Grow
from app.models.knowledge import AIConversation
from app.models.plant import Plant
from app.models.user import User
from app.services.auth import get_current_user
from app.services.bob_accountability import upsert_actionable_recommendations
from app.services.bob_env import load_latest_env
from app.services.bob_rules import compute_bob_steps
from app.services.guard import guard_user_message
from app.services.rag import chat
from app.services.credits import capture, release

router = APIRouter()
logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str
    grow_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    # base64 data URIs, e.g. "data:image/jpeg;base64,..."  (max 4 imagens)
    images: list[str] | None = None
    # Cotacao confirmada (POST /credits/quote) quando a mensagem tem custo em creditos
    quote_id: uuid.UUID | None = None


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    recommendation_id: str | None = None   # UUID da BobRecommendation (quando acionavel)
    action: str | None = None              # ex: "watering", "topping", "flush"
    plant_id: str | None = None            # UUID da planta como string
    plant_name: str | None = None


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
    # Carrega a mensagem de bloqueio personalizada do admin (se configurada)
    _config_result = await db.execute(select(AIConfig).order_by(AIConfig.created_at.asc()).limit(1))
    _ai_config = _config_result.scalar_one_or_none()
    _injection_msg = _ai_config.injection_message if _ai_config else None

    # Proteção contra prompt injection — valida e sanitiza antes de tocar o LLM
    body.message = guard_user_message(
        body.message,
        user_id=str(current_user.id),
        injection_message=_injection_msg,
    )

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

    # Limite mensal de consultas e, se a mensagem tiver custo, reserva dos creditos.
    # Nada acima consome saldo; a reserva e gravada antes de chamar a IA.
    reservation = await start_paid_task(db, current_user, "chat_message", body.quote_id)
    reservation_id = reservation.id if reservation is not None else None

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

    try:
        reply = await chat(db, conversation, body.message, grow, plant, body.images or [])
    except Exception:
        if reservation_id is not None:
            # Falha tecnica sem entrega: descarta a conversa nao gravada e devolve a reserva.
            await db.rollback()
            quote = await db.get(CreditQuote, reservation_id)
            if quote is not None:
                await release(db, quote, "Falha técnica")
        raise

    # ── Recomendacao acionavel (dedup por user+plant+action) ──────────────────
    rec_id: str | None = None
    rec_action: str | None = None
    rec_plant_id: str | None = None
    rec_plant_name: str | None = None

    if plant is not None:
        try:
            today = date.today()
            events_result = await db.execute(
                select(GrowEvent)
                .where(GrowEvent.plant_id == plant.id)
                .order_by(GrowEvent.event_date.desc())
                .limit(15)
            )
            plant_events = events_result.scalars().all()
            latest_temp, latest_humidity = await load_latest_env(db, plant.id)
            steps = await compute_bob_steps(
                plant,
                plant_events,
                today,
                latest_temp=latest_temp,
                latest_humidity=latest_humidity,
            )

            async with db.begin_nested():
                rec = await upsert_actionable_recommendations(
                    db, current_user.id, plant, steps
                )
                if rec is not None:
                    await db.flush()
                    rec_id = str(rec.id)
                    rec_action = rec.action
                    rec_plant_id = str(plant.id)
                    rec_plant_name = rec.plant_name
        except Exception as rec_err:
            logger.warning("Falha ao processar BobRecommendation (chat nao afetado): %s", rec_err)

    now_iso = datetime.now(timezone.utc).isoformat()
    conversation.messages = [
        *conversation.messages,
        {"role": "user", "content": body.message, "timestamp": now_iso},
        {"role": "assistant", "content": reply, "timestamp": now_iso},
    ]

    current_user.ai_queries_this_month += 1
    if reservation is not None:
        await capture(db, reservation, task_ref=str(conversation.id))
    await db.commit()

    return ChatResponse(
        conversation_id=conversation.id,
        reply=reply,
        recommendation_id=rec_id,
        action=rec_action,
        plant_id=rec_plant_id,
        plant_name=rec_plant_name,
    )


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
