"""
Suporte.

POST /support/messages – grava a mensagem e envia por e-mail (caixa definida no admin)
GET  /support/messages – mensagens do proprio usuario
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.account import SupportMessage
from app.models.user import User
from app.services.auth import get_current_user
from app.services.support import deliver_support_email

router = APIRouter()

MAX_PER_HOUR = 5


class SupportIn(BaseModel):
    subject: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=10, max_length=4000)

    model_config = {"extra": "forbid"}

    @field_validator("subject", "message")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class SupportOut(BaseModel):
    id: uuid.UUID
    subject: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/messages", response_model=SupportOut, status_code=status.HTTP_201_CREATED)
async def send_support_message(
    body: SupportIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if len(body.subject) < 3 or len(body.message) < 10:
        raise HTTPException(status_code=422, detail="Assunto ou mensagem muito curtos")
    now = datetime.now(timezone.utc)
    recent = (
        await db.execute(
            select(func.count(SupportMessage.id)).where(
                SupportMessage.user_id == current_user.id,
                SupportMessage.created_at > now - timedelta(hours=1),
            )
        )
    ).scalar() or 0
    if recent >= MAX_PER_HOUR:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas mensagens em pouco tempo. Tente novamente mais tarde.",
        )
    message = SupportMessage(
        id=uuid.uuid4(), user_id=current_user.id, reply_email=current_user.email,
        subject=body.subject, message=body.message, status="received",
        email_status="pending", email_attempts=0, created_at=now, updated_at=now,
    )
    db.add(message)
    await db.commit()
    await deliver_support_email(db, message)
    return message


@router.get("/messages", response_model=list[SupportOut])
async def list_support_messages(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(SupportMessage)
            .where(SupportMessage.user_id == current_user.id)
            .order_by(SupportMessage.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return list(rows)
