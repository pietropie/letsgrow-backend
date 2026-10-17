"""
Admin: regras de negocio configuraveis (creditos, conta, suporte, notificacoes).

GET   /admin/app-settings
PATCH /admin/app-settings
GET   /admin/credits/task-costs
PUT   /admin/credits/task-costs/{task_key}
GET   /admin/credits/packages
POST  /admin/credits/packages
PATCH /admin/credits/packages/{id}
POST  /admin/credits/adjust
GET   /admin/credits/ledger?email=
GET   /admin/support/messages?status=
PATCH /admin/support/messages/{id}
GET   /admin/bob-feedback?status=
PATCH /admin/bob-feedback/{id}
GET   /admin/account-requests
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.models.account import AccountDeletionRequest, DataExportRequest, SupportMessage
from app.models.bob import BobFeedback
from app.models.credits import CREDIT_TASKS, CreditLedgerEntry, CreditPackage, CreditTaskCost
from app.models.user import User
from app.services.app_settings import get_app_settings
from app.services.credits import CreditError, debit_adjustment, grant_lot, refresh_balance

router = APIRouter(dependencies=[Depends(require_admin_token)])


# ── Configuracao geral ────────────────────────────────────────────────────────

class AppSettingsOut(BaseModel):
    credits_enabled: bool
    credits_replace_query_limit: bool
    plan_credits_rollover: bool
    account_deletion_grace_days: int
    support_inbox_email: str | None
    notification_default_preview: Literal["full", "discreet"]
    notification_default_frequency: Literal["daily", "in_app"]
    updated_by: str | None
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class AppSettingsPatch(BaseModel):
    credits_enabled: bool | None = None
    credits_replace_query_limit: bool | None = None
    plan_credits_rollover: bool | None = None
    account_deletion_grace_days: int | None = Field(default=None, ge=1, le=90)
    # "" limpa a caixa (mensagens ficam so no admin)
    support_inbox_email: EmailStr | Literal[""] | None = None
    notification_default_preview: Literal["full", "discreet"] | None = None
    notification_default_frequency: Literal["daily", "in_app"] | None = None
    updated_by: str | None = Field(default=None, max_length=255)

    model_config = {"extra": "forbid"}


@router.get("/app-settings", response_model=AppSettingsOut)
async def get_settings_row(db: AsyncSession = Depends(get_db)):
    return await get_app_settings(db)


@router.patch("/app-settings", response_model=AppSettingsOut)
async def patch_settings_row(body: AppSettingsPatch, db: AsyncSession = Depends(get_db)):
    row = await get_app_settings(db)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is None and field != "support_inbox_email":
            continue
        if field == "support_inbox_email":
            value = value or None
        setattr(row, field, value)
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row


# ── Creditos ──────────────────────────────────────────────────────────────────

class TaskCostOut(BaseModel):
    task_key: str
    label: str
    credits: int | None
    is_active: bool
    updated_by: str | None


class TaskCostPut(BaseModel):
    credits: int = Field(ge=1, le=100000)
    is_active: bool
    updated_by: str | None = Field(default=None, max_length=255)

    model_config = {"extra": "forbid"}


@router.get("/credits/task-costs", response_model=list[TaskCostOut])
async def list_task_costs(db: AsyncSession = Depends(get_db)):
    rows = {row.task_key: row for row in (await db.execute(select(CreditTaskCost))).scalars().all()}
    return [
        TaskCostOut(
            task_key=key, label=label,
            credits=rows[key].credits if key in rows else None,
            is_active=rows[key].is_active if key in rows else False,
            updated_by=rows[key].updated_by if key in rows else None,
        )
        for key, label in CREDIT_TASKS.items()
    ]


@router.put("/credits/task-costs/{task_key}", response_model=TaskCostOut)
async def put_task_cost(task_key: str, body: TaskCostPut, db: AsyncSession = Depends(get_db)):
    if task_key not in CREDIT_TASKS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tarefa desconhecida")
    row = await db.get(CreditTaskCost, task_key)
    now = datetime.now(timezone.utc)
    if row is None:
        row = CreditTaskCost(task_key=task_key, created_at=now)
        db.add(row)
    row.credits = body.credits
    row.is_active = body.is_active
    row.updated_by = body.updated_by
    row.updated_at = now
    await db.commit()
    return TaskCostOut(task_key=task_key, label=CREDIT_TASKS[task_key], credits=row.credits,
                       is_active=row.is_active, updated_by=row.updated_by)


class PackageAdminOut(BaseModel):
    id: uuid.UUID
    label: str
    credits: int
    price_brl: float
    validity_days: int | None
    is_active: bool
    sort_order: int
    updated_by: str | None


class PackageCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    credits: int = Field(ge=1, le=1_000_000)
    price_brl: float = Field(gt=0, le=100_000)
    validity_days: int | None = Field(default=None, ge=1, le=3650)
    is_active: bool = False
    sort_order: int = 0
    updated_by: str | None = Field(default=None, max_length=255)

    model_config = {"extra": "forbid"}


class PackagePatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    credits: int | None = Field(default=None, ge=1, le=1_000_000)
    price_brl: float | None = Field(default=None, gt=0, le=100_000)
    validity_days: int | None = Field(default=None, ge=1, le=3650)
    is_active: bool | None = None
    sort_order: int | None = None
    updated_by: str | None = Field(default=None, max_length=255)

    model_config = {"extra": "forbid"}


def _package_out(row: CreditPackage) -> PackageAdminOut:
    return PackageAdminOut(
        id=row.id, label=row.label, credits=row.credits, price_brl=float(row.price_brl),
        validity_days=row.validity_days, is_active=row.is_active, sort_order=row.sort_order,
        updated_by=row.updated_by,
    )


@router.get("/credits/packages", response_model=list[PackageAdminOut])
async def admin_list_packages(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(CreditPackage).order_by(CreditPackage.sort_order, CreditPackage.credits))).scalars().all()
    return [_package_out(row) for row in rows]


@router.post("/credits/packages", response_model=PackageAdminOut, status_code=status.HTTP_201_CREATED)
async def admin_create_package(body: PackageCreate, db: AsyncSession = Depends(get_db)):
    now = datetime.now(timezone.utc)
    row = CreditPackage(id=uuid.uuid4(), created_at=now, updated_at=now, **body.model_dump())
    db.add(row)
    await db.commit()
    return _package_out(row)


@router.patch("/credits/packages/{package_id}", response_model=PackageAdminOut)
async def admin_patch_package(package_id: uuid.UUID, body: PackagePatch, db: AsyncSession = Depends(get_db)):
    """Pacotes nao sao apagados (compras antigas apontam para eles): desative com is_active=false."""
    row = await db.get(CreditPackage, package_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pacote não encontrado")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is None and field != "validity_days":
            continue
        setattr(row, field, value)
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return _package_out(row)


async def _user_by_email(db: AsyncSession, email: str) -> User:
    user = (await db.execute(select(User).where(User.email == email.strip().lower()))).scalar_one_or_none()
    if user is None:
        user = (await db.execute(select(User).where(User.email == email.strip()))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado")
    return user


class AdjustIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    # positivo = credito extra (estorno/cortesia); negativo = debito
    amount: int = Field(ge=-100_000, le=100_000)
    reason: str = Field(min_length=3, max_length=150)
    validity_days: int | None = Field(default=None, ge=1, le=3650)

    model_config = {"extra": "forbid"}


class LedgerRow(BaseModel):
    id: uuid.UUID
    kind: str
    bucket: str
    amount: int
    description: str
    expires_at: datetime | None
    task_key: str | None
    task_ref: str | None
    created_at: datetime


class UserLedgerOut(BaseModel):
    email: str
    available: int
    plan_available: int
    extra_available: int
    entries: list[LedgerRow]


async def _ledger_out(db: AsyncSession, user: User) -> UserLedgerOut:
    balance = await refresh_balance(db, user)
    rows = (
        await db.execute(
            select(CreditLedgerEntry)
            .where(CreditLedgerEntry.user_id == user.id)
            .order_by(CreditLedgerEntry.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    return UserLedgerOut(
        email=user.email, available=balance.available, plan_available=balance.plan_available,
        extra_available=balance.extra_available,
        entries=[LedgerRow(
            id=r.id, kind=r.kind, bucket=r.bucket, amount=r.amount, description=r.description,
            expires_at=r.expires_at, task_key=r.task_key, task_ref=r.task_ref, created_at=r.created_at,
        ) for r in rows],
    )


@router.post("/credits/adjust", response_model=UserLedgerOut)
async def admin_adjust_credits(body: AdjustIn, db: AsyncSession = Depends(get_db)):
    if body.amount == 0:
        raise HTTPException(status_code=422, detail="Informe um valor diferente de zero")
    user = await _user_by_email(db, body.email)
    now = datetime.now(timezone.utc)
    description = f"Ajuste: {body.reason}"
    if body.amount > 0:
        await grant_lot(
            db, user.id, kind="adjust", amount=body.amount, description=description,
            expires_at=now + timedelta(days=body.validity_days) if body.validity_days else None, now=now,
        )
    else:
        try:
            await debit_adjustment(db, user, -body.amount, description, now)
        except CreditError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail())
    await db.commit()
    return await _ledger_out(db, user)


@router.get("/credits/ledger", response_model=UserLedgerOut)
async def admin_user_ledger(email: str = Query(min_length=3), db: AsyncSession = Depends(get_db)):
    return await _ledger_out(db, await _user_by_email(db, email))


# ── Suporte e correcoes do Bobby ──────────────────────────────────────────────

class SupportAdminOut(BaseModel):
    id: uuid.UUID
    reply_email: str
    subject: str
    message: str
    status: str
    email_status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class StatusPatch(BaseModel):
    status: str


@router.get("/support/messages", response_model=list[SupportAdminOut])
async def admin_support_messages(
    status_filter: Literal["received", "answered"] | None = Query(default=None, alias="status"),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(SupportMessage).order_by(SupportMessage.created_at.desc()).limit(200)
    if status_filter:
        stmt = stmt.where(SupportMessage.status == status_filter)
    return list((await db.execute(stmt)).scalars().all())


@router.patch("/support/messages/{message_id}", response_model=SupportAdminOut)
async def admin_patch_support(message_id: uuid.UUID, body: StatusPatch, db: AsyncSession = Depends(get_db)):
    if body.status not in ("received", "answered"):
        raise HTTPException(status_code=422, detail="Status inválido")
    row = await db.get(SupportMessage, message_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mensagem não encontrada")
    row.status = body.status
    row.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return row


class FeedbackAdminOut(BaseModel):
    id: uuid.UUID
    user_email: str | None
    alert_id: str | None
    plant_id: uuid.UUID | None
    grow_id: uuid.UUID | None
    evidence_ids: list | None
    message: str
    status: str
    created_at: datetime
    reviewed_at: datetime | None


@router.get("/bob-feedback", response_model=list[FeedbackAdminOut])
async def admin_bob_feedback(
    status_filter: Literal["new", "reviewed"] | None = Query(default=None, alias="status"),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(BobFeedback, User.email)
        .join(User, User.id == BobFeedback.user_id)
        .order_by(BobFeedback.created_at.desc())
        .limit(200)
    )
    if status_filter:
        stmt = stmt.where(BobFeedback.status == status_filter)
    return [
        FeedbackAdminOut(
            id=fb.id, user_email=email, alert_id=fb.alert_id, plant_id=fb.plant_id, grow_id=fb.grow_id,
            evidence_ids=fb.evidence_ids, message=fb.message, status=fb.status,
            created_at=fb.created_at, reviewed_at=fb.reviewed_at,
        )
        for fb, email in (await db.execute(stmt)).all()
    ]


@router.patch("/bob-feedback/{feedback_id}", response_model=FeedbackAdminOut)
async def admin_patch_feedback(feedback_id: uuid.UUID, body: StatusPatch, db: AsyncSession = Depends(get_db)):
    if body.status not in ("new", "reviewed"):
        raise HTTPException(status_code=422, detail="Status inválido")
    fb = await db.get(BobFeedback, feedback_id)
    if fb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Correção não encontrada")
    fb.status = body.status
    fb.reviewed_at = datetime.now(timezone.utc) if body.status == "reviewed" else None
    await db.commit()
    user = await db.get(User, fb.user_id)
    return FeedbackAdminOut(
        id=fb.id, user_email=user.email if user else None, alert_id=fb.alert_id, plant_id=fb.plant_id,
        grow_id=fb.grow_id, evidence_ids=fb.evidence_ids, message=fb.message, status=fb.status,
        created_at=fb.created_at, reviewed_at=fb.reviewed_at,
    )


# ── Pedidos de copia e exclusao ───────────────────────────────────────────────

class ExportAdminOut(BaseModel):
    id: uuid.UUID
    user_email: str | None
    status: str
    requested_at: datetime
    completed_at: datetime | None
    expires_at: datetime | None
    error: str | None


class DeletionAdminOut(BaseModel):
    id: uuid.UUID
    user_email: str | None      # nulo depois da exclusao concluida
    status: str
    requested_at: datetime
    scheduled_for: datetime
    canceled_at: datetime | None
    completed_at: datetime | None
    error: str | None


class AccountRequestsOut(BaseModel):
    exports: list[ExportAdminOut]
    deletions: list[DeletionAdminOut]


@router.get("/account-requests", response_model=AccountRequestsOut)
async def admin_account_requests(db: AsyncSession = Depends(get_db)):
    exports = (
        await db.execute(
            select(DataExportRequest, User.email)
            .join(User, User.id == DataExportRequest.user_id)
            .order_by(DataExportRequest.requested_at.desc())
            .limit(100)
        )
    ).all()
    deletions = (
        await db.execute(
            select(AccountDeletionRequest, User.email)
            .outerjoin(User, User.id == AccountDeletionRequest.user_id)
            .order_by(AccountDeletionRequest.requested_at.desc())
            .limit(100)
        )
    ).all()
    return AccountRequestsOut(
        exports=[ExportAdminOut(
            id=r.id, user_email=email, status=r.status, requested_at=r.requested_at,
            completed_at=r.completed_at, expires_at=r.expires_at, error=r.error,
        ) for r, email in exports],
        deletions=[DeletionAdminOut(
            id=r.id, user_email=email, status=r.status, requested_at=r.requested_at,
            scheduled_for=r.scheduled_for, canceled_at=r.canceled_at, completed_at=r.completed_at, error=r.error,
        ) for r, email in deletions],
    )
