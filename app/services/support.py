"""Envio por e-mail das mensagens de suporte (a mensagem ja esta gravada no banco)."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import SupportMessage
from app.services.app_settings import get_app_settings

logger = logging.getLogger(__name__)

MAX_EMAIL_ATTEMPTS = 5


async def deliver_support_email(db: AsyncSession, message: SupportMessage) -> None:
    """Encaminha para a caixa do admin e confirma ao usuario.

    Caixa vazia no admin: o envio e pulado com log e a mensagem fica no admin.
    Falha: email_status=failed e a manutencao tenta de novo (ate 5 vezes).
    """
    from app.services.email import send_support_receipt, send_support_to_inbox

    inbox = (await get_app_settings(db)).support_inbox_email
    if not inbox:
        logger.info("Suporte %s gravado; caixa de suporte não configurada no admin, e-mail pulado", message.id)
        message.email_status = "skipped"
        await db.commit()
        return
    message.email_attempts += 1
    try:
        await send_support_to_inbox(
            inbox, message_id=str(message.id), user_email=message.reply_email,
            subject=message.subject, message=message.message,
        )
    except Exception as exc:
        logger.warning("Suporte %s: envio para a caixa falhou: %s", message.id, exc)
        message.email_status = "failed"
        await db.commit()
        return
    message.email_status = "sent"
    await db.commit()
    try:
        await send_support_receipt(message.reply_email, subject=message.subject)
    except Exception as exc:
        logger.warning("Suporte %s: confirmação ao usuário não enviada: %s", message.id, exc)


async def retry_support_emails(db: AsyncSession, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(SupportMessage).where(
                SupportMessage.email_status.in_(("pending", "failed")),
                SupportMessage.email_attempts < MAX_EMAIL_ATTEMPTS,
                SupportMessage.created_at < now - timedelta(minutes=2),
            )
        )
    ).scalars().all()
    for row in rows:
        await deliver_support_email(db, row)
    return len(rows)
