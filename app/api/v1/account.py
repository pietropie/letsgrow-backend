"""
Conta (LGPD): copia dos dados e exclusao com carencia.

POST   /account/data-exports               – pede a copia (202); processada em segundo plano
GET    /account/data-exports               – pedidos e estado
GET    /account/data-exports/{id}/download – URL assinada curta (somente o dono)
GET    /account/deletion-policy            – carencia atual, antes de pedir a exclusao
GET    /account/deletion-request           – pedido de exclusao ativo (404 se nao houver)
POST   /account/deletion-request           – agenda a exclusao (carencia definida no admin)
DELETE /account/deletion-request           – cancela durante a carencia
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.account import AccountDeletionRequest, DataExportRequest
from app.models.user import User
from app.services.account_data import EXPORT_COOLDOWN, run_export_in_background, schedule_deletion
from app.services.app_settings import get_app_settings
from app.services.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


class DataExportOut(BaseModel):
    id: uuid.UUID
    status: str
    requested_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    file_size: int | None

    model_config = {"from_attributes": True}


class DownloadOut(BaseModel):
    url: str
    expires_in_seconds: int


class DeletionIn(BaseModel):
    confirm: bool


class DeletionOut(BaseModel):
    id: uuid.UUID
    status: str
    requested_at: datetime
    scheduled_for: datetime
    grace_days: int


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@router.post("/data-exports", response_model=DataExportOut, status_code=status.HTTP_202_ACCEPTED)
async def request_data_export(
    background: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(DataExportRequest)
            .where(DataExportRequest.user_id == current_user.id)
            .order_by(DataExportRequest.requested_at.desc())
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    if any(row.status in ("requested", "processing") for row in rows):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Já existe uma cópia em preparação")
    recent = [row for row in rows if row.status != "failed" and now - _utc(row.requested_at) < EXPORT_COOLDOWN]
    if recent:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Você já pediu uma cópia nas últimas 24 horas",
        )
    request = DataExportRequest(id=uuid.uuid4(), user_id=current_user.id, status="requested", requested_at=now)
    db.add(request)
    await db.commit()
    background.add_task(run_export_in_background, request.id)
    return request


@router.get("/data-exports", response_model=list[DataExportOut])
async def list_data_exports(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(DataExportRequest)
            .where(DataExportRequest.user_id == current_user.id)
            .order_by(DataExportRequest.requested_at.desc())
            .limit(10)
        )
    ).scalars().all()
    return list(rows)


@router.get("/data-exports/{export_id}/download", response_model=DownloadOut)
async def download_data_export(
    export_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.storage import presign_download_short

    row = (
        await db.execute(
            select(DataExportRequest).where(
                DataExportRequest.id == export_id, DataExportRequest.user_id == current_user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cópia não encontrada")
    if row.status != "ready" or not row.file_key or (row.expires_at and _utc(row.expires_at) <= datetime.now(timezone.utc)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cópia indisponível para download")
    seconds = 600
    return DownloadOut(url=presign_download_short(settings.DATA_EXPORT_BUCKET, row.file_key, seconds), expires_in_seconds=seconds)


async def _active_deletion(db: AsyncSession, user_id: uuid.UUID) -> AccountDeletionRequest | None:
    return (
        await db.execute(
            select(AccountDeletionRequest).where(
                AccountDeletionRequest.user_id == user_id,
                AccountDeletionRequest.status.in_(("scheduled", "failed")),
            )
        )
    ).scalar_one_or_none()


def _deletion_out(row: AccountDeletionRequest) -> DeletionOut:
    days = max(0, round((_utc(row.scheduled_for) - _utc(row.requested_at)).total_seconds() / 86400))
    return DeletionOut(
        id=row.id, status=row.status, requested_at=row.requested_at,
        scheduled_for=row.scheduled_for, grace_days=days,
    )


class DeletionPolicyOut(BaseModel):
    grace_days: int


@router.get("/deletion-policy", response_model=DeletionPolicyOut)
async def get_deletion_policy(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Carencia atual (admin), mostrada no dialogo de confirmacao antes do pedido."""
    return DeletionPolicyOut(grace_days=(await get_app_settings(db)).account_deletion_grace_days)


@router.get("/deletion-request", response_model=DeletionOut)
async def get_deletion_request(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _active_deletion(db, current_user.id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nenhuma exclusão solicitada")
    return _deletion_out(row)


@router.post("/deletion-request", response_model=DeletionOut, status_code=status.HTTP_202_ACCEPTED)
async def request_account_deletion(
    body: DeletionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.email import send_deletion_scheduled

    if not body.confirm:
        raise HTTPException(status_code=422, detail="Confirme a exclusão")
    if await _active_deletion(db, current_user.id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A exclusão já foi solicitada")
    grace = (await get_app_settings(db)).account_deletion_grace_days
    row = await schedule_deletion(db, current_user, grace)
    try:
        await send_deletion_scheduled(current_user.email, scheduled_for=row.scheduled_for)
    except Exception as exc:
        logger.warning("E-mail de exclusão agendada não enviado: %s", exc)
    return _deletion_out(row)


@router.delete("/deletion-request", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_account_deletion(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = await _active_deletion(db, current_user.id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nenhuma exclusão solicitada")
    row.status = "canceled"
    row.canceled_at = datetime.now(timezone.utc)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
