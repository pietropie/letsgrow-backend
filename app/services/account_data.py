"""
LGPD: copia dos dados (export assincrono) e exclusao de conta com carencia.

Copia: ZIP no bucket privado DATA_EXPORT_BUCKET com JSON de todos os dados do
usuario e as fotos/avatar dele. O e-mail avisa sem link; o download e feito no
app autenticado (URL assinada curta).

Exclusao: agendada para agora + carencia (app_settings). Ao vencer, apaga os
objetos do usuario no MinIO, os dispositivos de sensor ligados aos ambientes
dele e o usuario (o restante sai por cascata). Compras de creditos ficam sem
vinculo pessoal (user_id nulo).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import uuid
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.account import AccountDeletionRequest, DataExportRequest, SupportMessage
from app.models.bob import BobAnomaly, BobFeedback, BobMemory, BobRecommendation
from app.models.credits import CreditLedgerEntry, CreditPurchase, CreditQuote
from app.models.environment_reading import EnvironmentReading
from app.models.event import GrowEvent
from app.models.grow import Grow
from app.models.knowledge import AIConversation
from app.models.notification import NotificationPreference
from app.models.plant import Plant
from app.models.pot import Pot
from app.models.sensor import SensorDevice, SensorReading
from app.models.user import User

logger = logging.getLogger(__name__)

EXPORT_COOLDOWN = timedelta(hours=24)
STUCK_EXPORT = timedelta(minutes=30)
_USER_FIELDS_EXCLUDED = {"hashed_password", "expo_push_token", "oauth_id"}


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _jsonable(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _row(obj, exclude: set[str] = frozenset()) -> dict:
    return {
        column.key: _jsonable(getattr(obj, column.key))
        for column in obj.__mapper__.column_attrs
        if column.key not in exclude
    }


async def _rows(db: AsyncSession, statement) -> list[dict]:
    return [_row(obj) for obj in (await db.execute(statement)).scalars().all()]


async def collect_user_data(db: AsyncSession, user: User) -> dict[str, object]:
    """Todos os registros do usuario, por arquivo do ZIP."""
    uid = user.id
    grow_ids = select(Grow.id).where(Grow.user_id == uid)
    plant_ids = select(Plant.id).where(Plant.user_id == uid)
    device_ids = select(SensorDevice.id).where(
        or_(SensorDevice.grow_id.in_(grow_ids), SensorDevice.plant_id.in_(plant_ids))
    )
    return {
        "perfil.json": _row(user, _USER_FIELDS_EXCLUDED),
        "ambientes.json": await _rows(db, select(Grow).where(Grow.user_id == uid)),
        "vasos.json": await _rows(db, select(Pot).where(Pot.grow_id.in_(grow_ids))),
        "plantas.json": await _rows(db, select(Plant).where(Plant.user_id == uid)),
        "diario.json": await _rows(db, select(GrowEvent).where(GrowEvent.plant_id.in_(plant_ids)).order_by(GrowEvent.event_date)),
        "registros_do_ambiente.json": await _rows(db, select(EnvironmentReading).where(EnvironmentReading.user_id == uid).order_by(EnvironmentReading.observed_at)),
        "sensores.json": await _rows(db, select(SensorDevice).where(SensorDevice.id.in_(device_ids))),
        "leituras_de_sensores.json": await _rows(db, select(SensorReading).where(SensorReading.device_id.in_(device_ids)).order_by(SensorReading.recorded_at)),
        "conversas_bobby.json": await _rows(db, select(AIConversation).where(AIConversation.user_id == uid).order_by(AIConversation.created_at)),
        "memoria_bobby.json": await _rows(db, select(BobMemory).where(BobMemory.user_id == uid)),
        "correcoes_bobby.json": await _rows(db, select(BobFeedback).where(BobFeedback.user_id == uid)),
        "recomendacoes_bobby.json": await _rows(db, select(BobRecommendation).where(BobRecommendation.user_id == uid)),
        "observacoes_bobby.json": await _rows(db, select(BobAnomaly).where(BobAnomaly.user_id == uid)),
        "preferencias_de_notificacao.json": await _rows(db, select(NotificationPreference).where(NotificationPreference.user_id == uid)),
        "creditos_extrato.json": await _rows(db, select(CreditLedgerEntry).where(CreditLedgerEntry.user_id == uid).order_by(CreditLedgerEntry.created_at)),
        "creditos_tarefas.json": await _rows(db, select(CreditQuote).where(CreditQuote.user_id == uid).order_by(CreditQuote.created_at)),
        "creditos_compras.json": await _rows(db, select(CreditPurchase).where(CreditPurchase.user_id == uid)),
        "suporte.json": await _rows(db, select(SupportMessage).where(SupportMessage.user_id == uid)),
    }


def build_zip(data: dict[str, object], files: list[tuple[str, bytes]], generated_at: datetime) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("LEIA-ME.txt", (
            "Cópia dos seus dados no Let's Grow\n"
            f"Gerada em {generated_at.isoformat()}\n\n"
            "Cada arquivo .json contém um tipo de registro da sua conta. A pasta arquivos/ "
            "contém suas fotos e seu avatar como estavam armazenados.\n"
        ))
        for name, content in data.items():
            archive.writestr(name, json.dumps(content, ensure_ascii=False, indent=2, default=str))
        for name, content in files:
            archive.writestr(f"arquivos/{name}", content)
    return buffer.getvalue()


def _user_files(user_id: uuid.UUID) -> list[tuple[str, bytes]]:
    from app.services.storage import get_bytes, user_object_keys

    files = []
    for bucket, key in user_object_keys(user_id):
        if bucket == settings.DATA_EXPORT_BUCKET:
            continue
        files.append((f"{bucket}/{key}", get_bytes(bucket, key)))
    return files


async def claim_export(db: AsyncSession, request_id: uuid.UUID, now: datetime) -> bool:
    """Marca o pedido como em processamento; False se outro processo ja pegou."""
    result = await db.execute(
        update(DataExportRequest)
        .where(DataExportRequest.id == request_id, DataExportRequest.status == "requested")
        .values(status="processing", started_at=now)
    )
    await db.commit()
    return result.rowcount == 1


async def process_export(db: AsyncSession, request_id: uuid.UUID, now: datetime | None = None) -> None:
    from app.services.email import send_export_ready
    from app.services.storage import put_bytes

    now = now or datetime.now(timezone.utc)
    if not await claim_export(db, request_id, now):
        return
    request = await db.get(DataExportRequest, request_id)
    user = await db.get(User, request.user_id)
    try:
        data = await collect_user_data(db, user)
        payload = build_zip(data, _user_files(user.id), now)
        key = f"{user.id}/{request.id}.zip"
        put_bytes(settings.DATA_EXPORT_BUCKET, key, payload, "application/zip")
    except Exception as exc:
        logger.error("Cópia de dados %s falhou: %s", request_id, exc, exc_info=True)
        request.status = "failed"
        request.error = str(exc)[:300]
        request.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return

    request.status = "ready"
    request.file_key = key
    request.file_size = len(payload)
    request.completed_at = datetime.now(timezone.utc)
    request.expires_at = request.completed_at + timedelta(days=settings.DATA_EXPORT_TTL_DAYS)
    await db.commit()
    try:
        await send_export_ready(user.email, expires_at=request.expires_at)
        request.email_sent_at = datetime.now(timezone.utc)
        await db.commit()
    except Exception as exc:
        logger.warning("E-mail da cópia de dados %s não enviado: %s", request_id, exc)


async def run_export_in_background(request_id: uuid.UUID) -> None:
    """Executado pela API logo apos o pedido (BackgroundTasks), com sessao propria."""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await process_export(db, request_id)


async def schedule_deletion(
    db: AsyncSession, user: User, grace_days: int, now: datetime | None = None,
) -> AccountDeletionRequest:
    now = now or datetime.now(timezone.utc)
    request = AccountDeletionRequest(
        id=uuid.uuid4(), user_id=user.id, email_hash=email_hash(user.email), status="scheduled",
        requested_at=now, scheduled_for=now + timedelta(days=grace_days),
    )
    db.add(request)
    await db.commit()
    return request


async def delete_user_account(db: AsyncSession, user: User) -> None:
    """Apaga objetos e registros do usuario. Falha de armazenamento interrompe antes do banco."""
    from app.services.storage import remove_object_strict, user_object_keys

    for bucket, key in user_object_keys(user.id):
        remove_object_strict(bucket, key)

    uid = user.id
    grow_ids = select(Grow.id).where(Grow.user_id == uid)
    plant_ids = select(Plant.id).where(Plant.user_id == uid)
    # Ordem explicita: plants.grow_id e RESTRICT e sensores usam SET NULL.
    await db.execute(delete(SensorDevice).where(
        or_(SensorDevice.grow_id.in_(grow_ids), SensorDevice.plant_id.in_(plant_ids))
    ))
    await db.execute(delete(GrowEvent).where(GrowEvent.plant_id.in_(plant_ids)))
    await db.execute(delete(BobFeedback).where(BobFeedback.user_id == uid))
    await db.execute(delete(Plant).where(Plant.user_id == uid))
    await db.execute(delete(Pot).where(Pot.grow_id.in_(grow_ids)))
    await db.execute(delete(EnvironmentReading).where(EnvironmentReading.user_id == uid))
    await db.execute(delete(Grow).where(Grow.user_id == uid))
    await db.execute(update(CreditPurchase).where(CreditPurchase.user_id == uid).values(user_id=None))
    await db.execute(
        update(AccountDeletionRequest).where(AccountDeletionRequest.user_id == uid).values(user_id=None)
    )
    await db.execute(delete(User).where(User.id == uid))


async def process_due_deletions(db: AsyncSession, now: datetime | None = None) -> int:
    from app.services.email import send_deletion_completed

    now = now or datetime.now(timezone.utc)
    due = (
        await db.execute(
            select(AccountDeletionRequest).where(
                AccountDeletionRequest.status.in_(("scheduled", "failed")),
                AccountDeletionRequest.scheduled_for <= now,
                AccountDeletionRequest.user_id.is_not(None),
            )
        )
    ).scalars().all()
    done = 0
    for request in due:
        request_id = request.id
        user = await db.get(User, request.user_id)
        if user is None:
            continue
        email = user.email
        try:
            await delete_user_account(db, user)
            await db.execute(
                update(AccountDeletionRequest)
                .where(AccountDeletionRequest.id == request_id)
                .values(status="completed", completed_at=now, error=None)
            )
            await db.commit()
            done += 1
        except Exception as exc:
            await db.rollback()
            logger.error("Exclusão de conta %s falhou: %s", request_id, exc, exc_info=True)
            await db.execute(
                update(AccountDeletionRequest)
                .where(AccountDeletionRequest.id == request_id)
                .values(status="failed", error=str(exc)[:300])
            )
            await db.commit()
            continue
        try:
            await send_deletion_completed(email)
        except Exception as exc:
            logger.warning("E-mail de exclusão concluída não enviado: %s", exc)
    return done


async def expire_old_exports(db: AsyncSession, now: datetime | None = None) -> int:
    from app.services.storage import remove_object_strict

    now = now or datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(DataExportRequest).where(
                DataExportRequest.status == "ready", DataExportRequest.expires_at <= now
            )
        )
    ).scalars().all()
    for row in rows:
        try:
            if row.file_key:
                remove_object_strict(settings.DATA_EXPORT_BUCKET, row.file_key)
        except Exception as exc:
            logger.warning("Não foi possível remover cópia expirada %s: %s", row.id, exc)
            continue
        row.status = "expired"
        row.file_key = None
    await db.commit()
    return len(rows)


async def pending_export_ids(db: AsyncSession, now: datetime | None = None) -> list[uuid.UUID]:
    """Pedidos a retomar: ainda nao iniciados ou presos em processamento."""
    now = now or datetime.now(timezone.utc)
    stuck = (
        await db.execute(
            select(DataExportRequest).where(
                DataExportRequest.status == "processing", DataExportRequest.started_at < now - STUCK_EXPORT
            )
        )
    ).scalars().all()
    for row in stuck:
        row.status = "requested"
    if stuck:
        await db.commit()
    return list(
        (
            await db.execute(
                select(DataExportRequest.id).where(
                    DataExportRequest.status == "requested",
                    DataExportRequest.requested_at < now - timedelta(minutes=2),
                )
            )
        ).scalars().all()
    )
