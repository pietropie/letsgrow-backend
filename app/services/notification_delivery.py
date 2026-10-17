"""
Entrega de push respeitando as preferencias de acompanhamento de cada usuario.

- frequency "in_app": nenhum push externo (resumo diario nem envio do admin).
- Horario silencioso: nenhum push externo dentro da janela (horario local).
- preview "discreet": titulo/corpo genericos, sem nome de planta nem conteudo.
- Quem nunca salvou preferencias segue os padroes do admin (app_settings) e o
  horario global do push diario (comportamento anterior).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_settings import AppSettings
from app.models.notification import NotificationPreference
from app.models.plant import Plant
from app.models.user import User

logger = logging.getLogger(__name__)

DISCREET_TITLE = "LetsGrow"
DISCREET_BODY = "Há uma atualização no LetsGrow."
SLOT_MINUTES = 15
_ANTI_SPAM_SECONDS = 82_800  # 23h


@dataclass
class EffectivePrefs:
    frequency: str
    preview: str
    timezone: str | None
    brief_time: time | None
    quiet_start: time | None
    quiet_end: time | None
    is_default: bool


def effective_prefs(pref: NotificationPreference | None, app_settings: AppSettings) -> EffectivePrefs:
    if pref is None:
        return EffectivePrefs(
            frequency=app_settings.notification_default_frequency,
            preview=app_settings.notification_default_preview,
            timezone=None, brief_time=None, quiet_start=None, quiet_end=None,
            is_default=True,
        )
    return EffectivePrefs(
        frequency=pref.frequency, preview=pref.preview, timezone=pref.timezone,
        brief_time=pref.brief_time, quiet_start=pref.quiet_start, quiet_end=pref.quiet_end,
        is_default=False,
    )


def time_in_window(value: time, start: time, end: time) -> bool:
    """Janela [start, end) que pode cruzar a meia-noite (ex.: 21:00 as 08:00)."""
    if start == end:
        return False
    if start < end:
        return start <= value < end
    return value >= start or value < end


def _local_now(prefs: EffectivePrefs, now: datetime) -> datetime | None:
    if not prefs.timezone:
        return None
    return now.astimezone(ZoneInfo(prefs.timezone))


def in_quiet_hours(prefs: EffectivePrefs, now: datetime) -> bool:
    local = _local_now(prefs, now)
    if local is None or prefs.quiet_start is None or prefs.quiet_end is None:
        return False
    return time_in_window(local.time().replace(second=0, microsecond=0), prefs.quiet_start, prefs.quiet_end)


def external_push_allowed(prefs: EffectivePrefs, now: datetime) -> bool:
    return prefs.frequency == "daily" and not in_quiet_hours(prefs, now)


def personal_brief_due(prefs: EffectivePrefs, now: datetime) -> bool:
    """True quando o horario local escolhido cai no intervalo de 15 min atual."""
    local = _local_now(prefs, now)
    if local is None or prefs.brief_time is None:
        return False
    slot_start = local.replace(minute=local.minute - local.minute % SLOT_MINUTES, second=0, microsecond=0)
    chosen = slot_start.replace(hour=prefs.brief_time.hour, minute=prefs.brief_time.minute)
    return slot_start <= chosen < slot_start + timedelta(minutes=SLOT_MINUTES)


def push_content(prefs: EffectivePrefs, title: str, body: str) -> tuple[str, str]:
    if prefs.preview == "discreet":
        return DISCREET_TITLE, DISCREET_BODY
    return title, body


async def load_prefs(db: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, NotificationPreference]:
    if not user_ids:
        return {}
    rows = (
        await db.execute(select(NotificationPreference).where(NotificationPreference.user_id.in_(user_ids)))
    ).scalars().all()
    return {row.user_id: row for row in rows}


async def deliver_daily_briefs(db: AsyncSession, *, scope: str, now: datetime | None = None) -> dict:
    """Envia o resumo diario.

    scope="default"  – somente quem nunca salvou preferencias (job no horario do admin)
    scope="personal" – quem salvou preferencias e cujo horario local cai neste intervalo
    scope="all"      – disparo manual do admin; respeita in_app, silencio e previa
    """
    from app.services.app_settings import get_app_settings
    from app.services.daily_brief import generate_daily_brief, invalidate_brief
    from app.services.push import send_push

    now = now or datetime.now(timezone.utc)
    app_settings = await get_app_settings(db)
    users = (
        await db.execute(
            select(User).where(User.is_active == True, User.expo_push_token.isnot(None))  # noqa: E712
        )
    ).scalars().all()
    prefs_map = await load_prefs(db, [u.id for u in users])
    stats = {
        "users_processed": 0, "pushed": 0, "skipped_no_token": 0, "skipped_no_plants": 0,
        "skipped_in_app": 0, "skipped_quiet": 0, "errors": 0,
    }

    for user in users:
        pref = prefs_map.get(user.id)
        if scope == "default" and pref is not None:
            continue
        prefs = effective_prefs(pref, app_settings)
        if scope == "personal" and (pref is None or not personal_brief_due(prefs, now)):
            continue
        stats["users_processed"] += 1
        if not user.expo_push_token:
            stats["skipped_no_token"] += 1
            continue
        if prefs.frequency != "daily":
            stats["skipped_in_app"] += 1
            continue
        if in_quiet_hours(prefs, now):
            stats["skipped_quiet"] += 1
            continue
        if user.daily_brief_sent_at:
            last = user.daily_brief_sent_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < _ANTI_SPAM_SECONDS:
                continue
        has_plant = (
            await db.execute(
                select(Plant.id).where(Plant.user_id == user.id, Plant.is_active == True).limit(1)  # noqa: E712
            )
        ).scalar_one_or_none()
        if not has_plant:
            stats["skipped_no_plants"] += 1
            continue
        try:
            await invalidate_brief(user.id)
            brief = await generate_daily_brief(db, user.id, force_refresh=True)
            title, body = push_content(prefs, brief.title, brief.body)
            data = {"type": "daily_brief"}
            if prefs.preview != "discreet":
                data["urgent_count"] = brief.urgent_count
            ok = await send_push(token=user.expo_push_token, title=title, body=body, data=data)
            if ok:
                stats["pushed"] += 1
                user.daily_brief_sent_at = now
            else:
                stats["errors"] += 1
        except Exception as exc:
            logger.error("Push para user %s falhou: %s", user.id, exc)
            stats["errors"] += 1

    await db.commit()
    return stats


async def tokens_for_external_push(
    db: AsyncSession, users: list[User], now: datetime | None = None,
) -> tuple[list[str], list[str], int]:
    """Separa tokens para um envio do admin: (previa completa, previa discreta, bloqueados).

    Bloqueados = usuarios em "somente no app" ou dentro do horario silencioso.
    """
    from app.services.app_settings import get_app_settings

    now = now or datetime.now(timezone.utc)
    app_settings = await get_app_settings(db)
    prefs_map = await load_prefs(db, [u.id for u in users])
    full: list[str] = []
    discreet: list[str] = []
    blocked = 0
    for user in users:
        if not user.expo_push_token:
            continue
        prefs = effective_prefs(prefs_map.get(user.id), app_settings)
        if not external_push_allowed(prefs, now):
            blocked += 1
        elif prefs.preview == "discreet":
            discreet.append(user.expo_push_token)
        else:
            full.append(user.expo_push_token)
    return full, discreet, blocked
