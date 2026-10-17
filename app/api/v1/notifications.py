"""
Preferencias de acompanhamento (resumo diario por push).

GET /users/me/notification-preferences?timezone=America/Sao_Paulo
PUT /users/me/notification-preferences

Sem preferencia salva, o GET devolve os padroes do admin (is_default=true). O
horario padrao e o do push diario global (UTC no admin), convertido para o fuso
informado pelo aparelho quando houver.
"""
from datetime import datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.ai_config import AIConfig
from app.models.notification import NotificationPreference
from app.models.user import User
from app.services.app_settings import get_app_settings
from app.services.auth import get_current_user
from app.services.notification_delivery import time_in_window

router = APIRouter()


def _valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Fuso horário inválido")
    return value


class NotificationPreferencesIn(BaseModel):
    frequency: Literal["daily", "in_app"]
    brief_time: time
    timezone: str
    quiet_start: time | None = None
    quiet_end: time | None = None
    preview: Literal["full", "discreet"]

    model_config = {"extra": "forbid"}

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        return _valid_timezone(value)

    @field_validator("brief_time", "quiet_start", "quiet_end")
    @classmethod
    def _minutes(cls, value: time | None) -> time | None:
        return value.replace(second=0, microsecond=0, tzinfo=None) if value else value

    @model_validator(mode="after")
    def _quiet(self):
        if (self.quiet_start is None) != (self.quiet_end is None):
            raise ValueError("Informe início e fim do horário silencioso")
        if self.quiet_start is not None:
            if self.quiet_start == self.quiet_end:
                raise ValueError("Início e fim do horário silencioso precisam ser diferentes")
            if self.frequency == "daily" and time_in_window(self.brief_time, self.quiet_start, self.quiet_end):
                raise ValueError("O horário do resumo está dentro do horário silencioso")
        return self


class NotificationPreferencesOut(BaseModel):
    frequency: Literal["daily", "in_app"]
    brief_time: str | None          # "HH:MM" no fuso abaixo
    timezone: str | None
    quiet_start: str | None
    quiet_end: str | None
    preview: Literal["full", "discreet"]
    is_default: bool


def _hhmm(value: time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


@router.get("/me/notification-preferences", response_model=NotificationPreferencesOut)
async def get_notification_preferences(
    tz: str | None = Query(default=None, alias="timezone", description="Fuso IANA do aparelho"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pref = await db.get(NotificationPreference, current_user.id)
    if pref is not None:
        return NotificationPreferencesOut(
            frequency=pref.frequency, brief_time=_hhmm(pref.brief_time), timezone=pref.timezone,
            quiet_start=_hhmm(pref.quiet_start), quiet_end=_hhmm(pref.quiet_end),
            preview=pref.preview, is_default=False,
        )

    app_settings = await get_app_settings(db)
    config = (await db.execute(select(AIConfig).order_by(AIConfig.created_at.asc()).limit(1))).scalar_one_or_none()
    brief_time: str | None = None
    zone: str | None = None
    if config is not None:
        utc_time = datetime.now(timezone.utc).replace(
            hour=config.daily_push_hour, minute=config.daily_push_minute, second=0, microsecond=0
        )
        if tz:
            try:
                zone = _valid_timezone(tz)
            except ValueError:
                raise HTTPException(status_code=422, detail="Fuso horário inválido")
            brief_time = utc_time.astimezone(ZoneInfo(zone)).strftime("%H:%M")
        else:
            zone = "UTC"
            brief_time = utc_time.strftime("%H:%M")
    return NotificationPreferencesOut(
        frequency=app_settings.notification_default_frequency,
        brief_time=brief_time, timezone=zone, quiet_start=None, quiet_end=None,
        preview=app_settings.notification_default_preview, is_default=True,
    )


@router.put("/me/notification-preferences", response_model=NotificationPreferencesOut)
async def put_notification_preferences(
    body: NotificationPreferencesIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pref = await db.get(NotificationPreference, current_user.id)
    now = datetime.now(timezone.utc)
    if pref is None:
        pref = NotificationPreference(user_id=current_user.id, created_at=now)
        db.add(pref)
    for field, value in body.model_dump().items():
        setattr(pref, field, value)
    pref.updated_at = now
    await db.commit()
    return NotificationPreferencesOut(
        frequency=pref.frequency, brief_time=_hhmm(pref.brief_time), timezone=pref.timezone,
        quiet_start=_hhmm(pref.quiet_start), quiet_end=_hhmm(pref.quiet_end),
        preview=pref.preview, is_default=False,
    )
