"""
Chat do Bobby.

POST   /ai/chat                               – resposta inteira (compativel com o app atual)
POST   /ai/chat/stream                        – mesma resposta em SSE: status, delta, sources, done
POST   /ai/chat/{message_id}/regenerate       – gera de novo a ultima resposta (cobra como mensagem nova)
GET    /ai/conversations?limit&offset&q       – conversas do usuario (X-Total-Count no cabecalho)
GET    /ai/conversations/{id}                 – conversa com as mensagens (cada uma com `id`)
PATCH  /ai/conversations/{id}                 – renomear
DELETE /ai/conversations/{id}                 – apagar

Todas as rotas que chamam a IA seguem a mesma regra: limite mensal de consultas e, se a
mensagem tiver custo em creditos, cotacao confirmada -> reserva -> captura na entrega ou
estorno em falha (app/api/v1/credits.start_paid_task).
"""
import asyncio
import base64
import json
import logging
import mimetypes
import unicodedata
import uuid
from datetime import date, datetime, timezone

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.credits import start_paid_task
from app.database import AsyncSessionLocal, get_db
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
from app.services.credits import capture, release
from app.services.guard import guard_user_message
from app.services.rag import prepare_chat, run_chat, stream_chat
from app.services import storage
from app.services.storage import BUCKET_EVENTS
from app.services.upload_access import require_owned_upload

router = APIRouter()
logger = logging.getLogger(__name__)

MAX_PHOTOS = 4
_PREVIOUS_VERSIONS = 3


class ChatRequest(BaseModel):
    message: str
    grow_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    # base64 data URIs, e.g. "data:image/jpeg;base64,..."  (max 4 imagens)
    images: list[str] | None = None
    # Fotos ja enviadas por POST /uploads/presign (bucket events): preferivel ao base64
    photo_keys: list[str] | None = Field(default=None, max_length=MAX_PHOTOS)
    # Cotacao confirmada (POST /credits/quote) quando a mensagem tem custo em creditos
    quote_id: uuid.UUID | None = None


class ChatSource(BaseModel):
    """Algo que entrou de fato no prompt da resposta."""
    kind: str                      # event | environment_reading | knowledge | photo
    id: str
    title: str
    date: str | None = None        # ISO 8601; nulo para trechos da base e fotos
    scope: str                     # message | focus | knowledge | account
    plant_id: str | None = None
    grow_id: str | None = None


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    message_id: str | None = None           # id da resposta (feedback, gerar de novo)
    user_message_id: str | None = None
    sources: list[ChatSource] = []
    recommendation_id: str | None = None   # UUID da BobRecommendation (quando acionavel)
    action: str | None = None              # ex: "watering", "topping", "flush"
    plant_id: str | None = None            # UUID da planta como string
    plant_name: str | None = None


class RegenerateRequest(BaseModel):
    quote_id: uuid.UUID | None = None


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str | None
    grow_id: uuid.UUID | None
    plant_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    message_count: int

    model_config = {"from_attributes": True}


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)

    model_config = {"extra": "forbid"}

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Informe um título")
        return value


# ── Etapas compartilhadas ──────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _guarded(db: AsyncSession, user: User, message: str) -> str:
    """Protecao contra prompt injection — valida e sanitiza antes de tocar o LLM."""
    config = (await db.execute(select(AIConfig).order_by(AIConfig.created_at.asc()).limit(1))).scalar_one_or_none()
    return guard_user_message(message, user_id=str(user.id), injection_message=config.injection_message if config else None)


async def _scope(db: AsyncSession, user_id: uuid.UUID, grow_id, plant_id) -> tuple[Grow | None, Plant | None]:
    grow = plant = None
    if grow_id:
        grow = (await db.execute(select(Grow).where(Grow.id == grow_id, Grow.user_id == user_id))).scalar_one_or_none()
        if not grow:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambiente não encontrado")
    if plant_id:
        plant = (await db.execute(select(Plant).where(Plant.id == plant_id, Plant.user_id == user_id))).scalar_one_or_none()
        if not plant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planta não encontrada")
    return grow, plant


async def _photos(user: User, images: list[str] | None, photo_keys: list[str] | None) -> tuple[list[str], list[str | None]]:
    """Fotos da mensagem: uploads do proprio usuario (lidos do armazenamento) ou base64 legado."""
    if images and photo_keys:
        raise HTTPException(status_code=422, detail="Envie as fotos por upload (photo_keys) ou em base64 (images), não os dois.")
    if photo_keys:
        uris: list[str] = []
        for key in photo_keys[:MAX_PHOTOS]:
            require_owned_upload(BUCKET_EVENTS, key, user.id)
            try:
                data = await asyncio.to_thread(storage.get_bytes, BUCKET_EVENTS, key)
            except Exception:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Foto não encontrada. Envie de novo.")
            mime = mimetypes.guess_type(key)[0] or "image/jpeg"
            uris.append(f"data:{mime};base64,{base64.b64encode(data).decode()}")
        return uris, list(photo_keys[:MAX_PHOTOS])
    inline = [img for img in (images or []) if img.startswith("data:image/")][:MAX_PHOTOS]
    return inline, [None] * len(inline)


async def _release(db: AsyncSession, reservation_id: uuid.UUID | None, reason: str) -> None:
    """Descarta o que nao foi gravado e devolve a reserva (falha tecnica ou cancelamento): nada e cobrado."""
    await db.rollback()
    if reservation_id is None:
        return
    quote = await db.get(CreditQuote, reservation_id)
    if quote is not None:
        await release(db, quote, reason)


async def _recommendation(db: AsyncSession, user: User, plant: Plant | None) -> tuple[str | None, str | None, str | None, str | None]:
    """Recomendacao acionavel (dedup por user+plant+action), como antes."""
    if plant is None:
        return None, None, None, None
    try:
        events = (
            await db.execute(
                select(GrowEvent).where(GrowEvent.plant_id == plant.id).order_by(GrowEvent.event_date.desc()).limit(15)
            )
        ).scalars().all()
        latest_temp, latest_humidity = await load_latest_env(db, plant.id)
        steps = await compute_bob_steps(plant, events, date.today(), latest_temp=latest_temp, latest_humidity=latest_humidity)
        async with db.begin_nested():
            rec = await upsert_actionable_recommendations(db, user.id, plant, steps)
            if rec is not None:
                await db.flush()
                return str(rec.id), rec.action, str(plant.id), rec.plant_name
    except Exception as rec_err:
        logger.warning("Falha ao processar BobRecommendation (chat nao afetado): %s", rec_err)
    return None, None, None, None


def _user_message(text: str, photo_keys: list[str | None], image_count: int) -> dict:
    message = {"id": str(uuid.uuid4()), "role": "user", "content": text, "timestamp": _now_iso()}
    keys = [k for k in photo_keys if k]
    if keys:
        message["photo_keys"] = keys
    elif image_count:
        message["images"] = image_count  # base64 nao e guardado: so a quantidade
    return message


def _assistant_message(reply: str, sources: list[dict]) -> dict:
    return {"id": str(uuid.uuid4()), "role": "assistant", "content": reply, "timestamp": _now_iso(), "sources": sources}


async def _deliver(db: AsyncSession, user: User, conversation: AIConversation, messages: list[dict], reservation) -> None:
    """Grava a entrega: mensagens, contador de consultas e captura dos creditos, num commit so."""
    conversation.messages = messages
    user.ai_queries_this_month += 1
    if reservation is not None:
        await capture(db, reservation, task_ref=str(conversation.id))
    await db.commit()


async def _own_conversation(db: AsyncSession, conversation_id, user_id) -> AIConversation:
    conversation = (
        await db.execute(
            select(AIConversation).where(AIConversation.id == conversation_id, AIConversation.user_id == user_id)
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversa não encontrada")
    return conversation


# ── POST /ai/chat ──────────────────────────────────────────────────────────────


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    message = await _guarded(db, current_user, body.message)
    grow, plant = await _scope(db, current_user.id, body.grow_id, body.plant_id)
    images, keys = await _photos(current_user, body.images, body.photo_keys)

    # Limite mensal de consultas e, se a mensagem tiver custo, reserva dos creditos.
    # Nada acima consome saldo; a reserva e gravada antes de chamar a IA.
    reservation = await start_paid_task(db, current_user, "chat_message", body.quote_id)
    reservation_id = reservation.id if reservation is not None else None

    conversation = None
    if body.conversation_id:
        conversation = (
            await db.execute(
                select(AIConversation).where(
                    AIConversation.id == body.conversation_id, AIConversation.user_id == current_user.id
                )
            )
        ).scalar_one_or_none()
    if not conversation:
        conversation = AIConversation(
            user_id=current_user.id, grow_id=body.grow_id, plant_id=body.plant_id, messages=[], title=message[:60],
        )
        db.add(conversation)
        await db.flush()

    try:
        prepared = await prepare_chat(db, current_user.id, conversation.messages or [], message, grow, plant, images, keys)
        reply = await run_chat(db, prepared)
    except Exception:
        # Falha tecnica sem entrega: descarta a conversa nao gravada e devolve a reserva.
        await _release(db, reservation_id, "Falha técnica")
        raise

    rec_id, rec_action, rec_plant_id, rec_plant_name = await _recommendation(db, current_user, plant)
    user_msg = _user_message(message, keys, len(images))
    assistant_msg = _assistant_message(reply, prepared.sources)
    await _deliver(db, current_user, conversation, [*(conversation.messages or []), user_msg, assistant_msg], reservation)

    return ChatResponse(
        conversation_id=conversation.id, reply=reply, message_id=assistant_msg["id"], user_message_id=user_msg["id"],
        sources=prepared.sources, recommendation_id=rec_id, action=rec_action, plant_id=rec_plant_id,
        plant_name=rec_plant_name,
    )


# ── POST /ai/chat/{message_id}/regenerate ──────────────────────────────────────


async def _find_message(db: AsyncSession, user_id: uuid.UUID, message_id: str) -> tuple[AIConversation, int]:
    rows = (
        await db.execute(
            select(AIConversation).where(AIConversation.user_id == user_id).order_by(AIConversation.updated_at.desc())
        )
    ).scalars().all()
    for conversation in rows:
        for index, message in enumerate(conversation.messages or []):
            if isinstance(message, dict) and message.get("id") == message_id:
                return conversation, index
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mensagem não encontrada")


@router.post("/chat/{message_id}/regenerate", response_model=ChatResponse)
async def regenerate_endpoint(
    message_id: str,
    body: RegenerateRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Gera de novo a ULTIMA resposta da conversa, com a mesma pergunta e o mesmo contexto.

    Conta como mensagem nova: limite mensal e, com custo ativo, cotacao/reserva/captura. A resposta
    anterior fica em `previous` (ate 3 versoes) e a nova ganha outro `id`.
    """
    conversation, index = await _find_message(db, current_user.id, message_id)
    messages = list(conversation.messages or [])
    target = messages[index]
    if target.get("role") != "assistant" or index != len(messages) - 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Só a última resposta pode ser gerada de novo.")
    question = messages[index - 1] if index > 0 else None
    if not question or question.get("role") != "user":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Não há pergunta para esta resposta.")
    if question.get("images") and not question.get("photo_keys"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A foto enviada nesta pergunta não fica guardada. Envie a pergunta de novo com a foto.",
        )

    grow = await db.get(Grow, conversation.grow_id) if conversation.grow_id else None
    plant = await db.get(Plant, conversation.plant_id) if conversation.plant_id else None
    images, keys = await _photos(current_user, None, question.get("photo_keys"))

    reservation = await start_paid_task(db, current_user, "chat_message", body.quote_id if body else None)
    reservation_id = reservation.id if reservation is not None else None
    try:
        prepared = await prepare_chat(db, current_user.id, messages[: index - 1], question["content"], grow, plant, images, keys)
        reply = await run_chat(db, prepared)
    except Exception:
        await _release(db, reservation_id, "Falha técnica")
        raise

    new_msg = _assistant_message(reply, prepared.sources)
    previous = [*target.get("previous", []), {"id": target["id"], "content": target["content"], "timestamp": target.get("timestamp")}]
    new_msg["previous"] = previous[-_PREVIOUS_VERSIONS:]
    new_msg["regenerated_from"] = target["id"]
    await _deliver(db, current_user, conversation, [*messages[:index], new_msg], reservation)

    return ChatResponse(
        conversation_id=conversation.id, reply=reply, message_id=new_msg["id"], user_message_id=question.get("id"),
        sources=prepared.sources,
    )


# ── POST /ai/chat/stream (SSE) ─────────────────────────────────────────────────


def stream_session():
    """Sessao propria do streaming: a sessao da requisicao fecha antes do fim da resposta."""
    return AsyncSessionLocal()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


class _Cancelled(Exception):
    pass


async def stream_events(
    request: Request,
    *,
    user_id: uuid.UUID,
    message: str,
    conversation_id: uuid.UUID | None,
    grow_id: uuid.UUID | None,
    plant_id: uuid.UUID | None,
    images: list[str],
    keys: list[str | None],
    reservation_id: uuid.UUID | None,
):
    """Eventos SSE da resposta. Cancelada antes do `done`: nada e salvo, contado ou cobrado."""
    async with stream_session() as db:
        try:
            user = await db.get(User, user_id)
            conversation = await db.get(AIConversation, conversation_id) if conversation_id else None
            grow = await db.get(Grow, grow_id) if grow_id else None
            plant = await db.get(Plant, plant_id) if plant_id else None
            prepared = await prepare_chat(
                db, user_id, (conversation.messages if conversation else []) or [], message, grow, plant, images, keys,
            )
            subject = f" da {plant.display_name or plant.strain_name}" if plant else (f" de {grow.name}" if grow else "")
            yield _sse("status", {
                "stage": "context", "records": prepared.record_count,
                "text": f"lendo {prepared.record_count} {'registro' if prepared.record_count == 1 else 'registros'}{subject}",
            })
            parts: list[str] = []
            async for piece in stream_chat(db, prepared):
                if await request.is_disconnected():
                    raise _Cancelled()
                parts.append(piece)
                yield _sse("delta", {"text": piece})
            if await request.is_disconnected():
                raise _Cancelled()
        except (_Cancelled, asyncio.CancelledError, GeneratorExit):
            with anyio.CancelScope(shield=True):
                await _release(db, reservation_id, "Cancelada pelo usuário")
            logger.info("Bobby: resposta cancelada pelo cliente (user=%s)", user_id)
            return
        except Exception as err:
            logger.error("Bobby: falha no streaming: %s", err, exc_info=True)
            with anyio.CancelScope(shield=True):
                await _release(db, reservation_id, "Falha técnica")
            yield _sse("error", {"message": "A resposta não chegou. Sua pergunta foi preservada para tentar de novo."})
            return

        # Resposta completa: grava e cobra antes dos ultimos eventos (fechar agora nao perde nada).
        reply = "".join(parts)
        with anyio.CancelScope(shield=True):
            if conversation is None:
                conversation = AIConversation(
                    user_id=user_id, grow_id=grow_id, plant_id=plant_id, messages=[], title=message[:60],
                )
                db.add(conversation)
                await db.flush()
            reservation = await db.get(CreditQuote, reservation_id) if reservation_id else None
            rec_id, rec_action, rec_plant_id, rec_plant_name = await _recommendation(db, user, plant)
            user_msg = _user_message(message, keys, len(images))
            assistant_msg = _assistant_message(reply, prepared.sources)
            await _deliver(db, user, conversation, [*(conversation.messages or []), user_msg, assistant_msg], reservation)
        yield _sse("sources", {"sources": prepared.sources})
        yield _sse("done", {
            "conversation_id": str(conversation.id), "message_id": assistant_msg["id"], "user_message_id": user_msg["id"],
            "recommendation_id": rec_id, "action": rec_action, "plant_id": rec_plant_id, "plant_name": rec_plant_name,
        })


@router.post("/chat/stream")
async def chat_stream_endpoint(
    body: ChatRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mesma regra do POST /ai/chat, em Server-Sent Events.

    Erros de validacao, limite ou creditos chegam como HTTP normal (antes do streaming).
    Eventos: `status` {stage, records, text}, `delta` {text}, `sources` {sources[]},
    `done` {conversation_id, message_id, user_message_id, ...} ou `error` {message}.
    Se o cliente fechar a conexao antes do `done`, a reserva e devolvida e nada e gravado.
    """
    message = await _guarded(db, current_user, body.message)
    await _scope(db, current_user.id, body.grow_id, body.plant_id)
    images, keys = await _photos(current_user, body.images, body.photo_keys)
    if body.conversation_id:
        await _own_conversation(db, body.conversation_id, current_user.id)
    reservation = await start_paid_task(db, current_user, "chat_message", body.quote_id)
    return StreamingResponse(
        stream_events(
            request, user_id=current_user.id, message=message, conversation_id=body.conversation_id,
            grow_id=body.grow_id, plant_id=body.plant_id, images=images, keys=keys,
            reservation_id=reservation.id if reservation is not None else None,
        ),
        media_type="text/event-stream",
        # Sem cache nem buffer em proxies (Traefik/nginx), para os pedacos chegarem na hora.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ── Conversas ──────────────────────────────────────────────────────────────────


def _fold(text: str) -> str:
    """Busca sem diferenciar maiusculas e acentos."""
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    response: Response,
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=100, description="Busca no título e no texto das mensagens"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mais recentes primeiro. O total (para "carregar mais") vem no cabeçalho X-Total-Count."""
    stmt = select(AIConversation).where(AIConversation.user_id == current_user.id).order_by(AIConversation.updated_at.desc())
    term = _fold(q.strip()) if q and q.strip() else None
    if term is None:
        total = (
            await db.execute(select(func.count(AIConversation.id)).where(AIConversation.user_id == current_user.id))
        ).scalar() or 0
        conversations = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    else:
        # Mensagens ficam em JSON (com escapes unicode no banco): o filtro e feito aqui, por usuario.
        matches = [
            c for c in (await db.execute(stmt)).scalars().all()
            if term in _fold(c.title or "")
            or any(term in _fold(str(m.get("content", ""))) for m in (c.messages or []) if isinstance(m, dict))
        ]
        total = len(matches)
        conversations = matches[offset:offset + limit]
    response.headers["X-Total-Count"] = str(total)
    return [
        ConversationSummary(
            id=c.id, title=c.title, grow_id=c.grow_id, plant_id=c.plant_id,
            created_at=c.created_at, updated_at=c.updated_at, message_count=len(c.messages or []),
        )
        for c in conversations
    ]


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _own_conversation(db, conversation_id, current_user.id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationSummary)
async def rename_conversation(
    conversation_id: uuid.UUID,
    body: ConversationRename,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conversation = await _own_conversation(db, conversation_id, current_user.id)
    conversation.title = body.title
    await db.commit()
    return ConversationSummary(
        id=conversation.id, title=conversation.title, grow_id=conversation.grow_id, plant_id=conversation.plant_id,
        created_at=conversation.created_at, updated_at=conversation.updated_at,
        message_count=len(conversation.messages or []),
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Apaga a conversa e as avaliacoes das respostas dela. A memoria do Bobby nao muda."""
    conversation = await _own_conversation(db, conversation_id, current_user.id)
    await db.delete(conversation)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
