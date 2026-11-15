"""
Anexos do chat do Bobby (fotos e PDF): 30 dias de guarda e "Guardar no calendário".

Decisão do usuário (2026-11-15): o anexo da conversa fica guardado por 30 dias e
depois sai do armazenamento (a mensagem continua, sem o anexo). A foto que a
pessoa escolhe guardar vira um registro de foto da planta, com cópia própria fora
da retenção, e passa a valer como foto mais recente no cartão.

Na mensagem do usuário (AIConversation.messages):
- `photo_keys`: anexos ainda guardados;
- `expired_photo_keys`: anexos que já saíram (e `attachments_expired_at`);
- `saved_photo_events`: {chave do anexo: id do registro criado no diário}.

Backup: o media-backup (scripts/backup-media.sh) move para a lixeira do R2 o que
saiu do MinIO, e a lixeira apaga em 33 dias. Ele também para se mais de 20% dos
objetos de um bucket (mínimo 10) sumirem num dia. Por isso cada execução apaga no
máximo 10% dos objetos do bucket events (mínimo 10). Em regime, o que vence num dia
é o que chegou num único dia, 30 dias antes: no máximo ~1/30 dos anexos, bem abaixo
do teto. Assim o prazo de 30 dias vale sem mexer na trava do backup.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import GrowEvent
from app.models.knowledge import AIConversation
from app.models.plant import Plant
from app.services import storage
from app.services.storage import BUCKET_EVENTS

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
# Teto por execução: metade da trava do backup (20% do bucket, mínimo 10)
MAX_DELETE_SHARE = 0.10
MIN_DELETE_PER_RUN = 10


def delete_cap() -> int:
    total = storage.count_objects(BUCKET_EVENTS)
    return max(MIN_DELETE_PER_RUN, int(total * MAX_DELETE_SHARE))
SAVED_PHOTO_NOTE = "Foto da conversa com o Bobby"


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        value = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _kind(key: str) -> str:
    return "pdf" if (mimetypes.guess_type(key)[0] or "") == "application/pdf" else "image"


def expires_at(message: dict) -> datetime | None:
    sent = _parse(message.get("timestamp"))
    return sent + timedelta(days=RETENTION_DAYS) if sent else None


def attachments(message: dict) -> list[dict]:
    """Estado de cada anexo da mensagem, para o app mostrar a foto, o PDF ou "anexo expirado"."""
    expiry = expires_at(message)
    saved = message.get("saved_photo_events") or {}
    items = []
    for key, available in [(k, True) for k in message.get("photo_keys") or []] + \
                          [(k, False) for k in message.get("expired_photo_keys") or []]:
        url = None
        if available:
            try:
                url = storage.presign_download(BUCKET_EVENTS, key)
            except Exception:  # sem URL assinada, o app mostra só o estado
                logger.warning("Anexo %s sem URL assinada", key)
        kind = _kind(key)
        items.append({
            "key": key, "kind": kind, "available": available, "url": url,
            "expires_at": expiry.isoformat() if expiry else None,
            "can_save": available and kind == "image",
            "saved_event_id": saved.get(key),
        })
    return items


async def expire_attachments(db: AsyncSession, now: datetime | None = None) -> int:
    """Tira do armazenamento os anexos com mais de 30 dias. Devolve quantos saíram nesta execução."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    cap = await asyncio.to_thread(delete_cap)
    deleted = 0
    conversations = (await db.execute(select(AIConversation))).scalars().all()
    for conversation in conversations:
        if deleted >= cap:
            break
        messages = list(conversation.messages or [])
        changed = False
        for index, message in enumerate(messages):
            if deleted >= cap:
                break
            if not isinstance(message, dict) or message.get("role") != "user" or not message.get("photo_keys"):
                continue
            sent = _parse(message.get("timestamp"))
            if sent is None or sent > cutoff:
                continue
            room = cap - deleted
            keys = list(message["photo_keys"])
            leaving, staying = keys[:room], keys[room:]
            for key in leaving:
                await asyncio.to_thread(storage.delete_object, BUCKET_EVENTS, key)
            deleted += len(leaving)
            updated = {**message, "expired_photo_keys": [*(message.get("expired_photo_keys") or []), *leaving]}
            if staying:
                updated["photo_keys"] = staying
            else:
                updated.pop("photo_keys", None)
                updated["attachments_expired_at"] = now.isoformat()
            messages[index] = updated
            changed = True
        if changed:
            conversation.messages = messages  # lista nova: o JSON é regravado
            await db.commit()
    if deleted:
        logger.info("Anexos do chat: %d apagados (mais de %d dias)", deleted, RETENTION_DAYS)
    return deleted


class AttachmentError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def save_to_calendar(
    db: AsyncSession,
    user_id: uuid.UUID,
    conversation: AIConversation,
    message_id: str,
    photo_key: str,
    plant_id: uuid.UUID,
    event_date: datetime | None = None,
) -> GrowEvent:
    """Copia a foto anexada para um registro de foto da planta (fora da retenção do chat)."""
    messages = list(conversation.messages or [])
    index = next((i for i, m in enumerate(messages)
                  if isinstance(m, dict) and m.get("id") == message_id and m.get("role") == "user"), None)
    if index is None:
        raise AttachmentError(404, "Mensagem não encontrada")
    message = messages[index]
    if photo_key in (message.get("expired_photo_keys") or []):
        raise AttachmentError(410, "Este anexo expirou e não pode mais ser guardado.")
    if photo_key not in (message.get("photo_keys") or []):
        raise AttachmentError(404, "Anexo não encontrado nesta mensagem")
    if _kind(photo_key) != "image":
        raise AttachmentError(422, "Só fotos podem ir para o calendário.")
    plant = (await db.execute(select(Plant).where(Plant.id == plant_id, Plant.user_id == user_id))).scalar_one_or_none()
    if plant is None:
        raise AttachmentError(404, "Planta não encontrada")

    saved = dict(message.get("saved_photo_events") or {})
    if photo_key in saved:  # já guardada: devolve o mesmo registro
        existing = await db.get(GrowEvent, uuid.UUID(saved[photo_key]))
        if existing is not None:
            return existing

    try:
        data = await asyncio.to_thread(storage.get_bytes, BUCKET_EVENTS, photo_key)
    except Exception:
        raise AttachmentError(404, "Foto não encontrada. Ela pode ter expirado.")
    ext = photo_key.rsplit(".", 1)[-1].lower() if "." in photo_key else "jpg"
    new_key = f"{user_id}/{uuid.uuid4()}.{ext}"
    content_type = mimetypes.guess_type(photo_key)[0] or "image/jpeg"
    await asyncio.to_thread(storage.put_bytes, BUCKET_EVENTS, new_key, data, content_type)

    event = GrowEvent(
        id=uuid.uuid4(), plant_id=plant.id, event_type="note",
        event_date=event_date or _parse(message.get("timestamp")) or datetime.now(timezone.utc),
        notes=SAVED_PHOTO_NOTE, photo_keys=[new_key],
    )
    db.add(event)
    saved[photo_key] = str(event.id)
    messages[index] = {**message, "saved_photo_events": saved}
    conversation.messages = messages
    await db.commit()
    return event
