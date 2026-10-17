"""Job periodico (APScheduler, a cada 10 min): pendencias de conta, suporte e creditos."""
import logging

from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


async def run_account_maintenance() -> None:
    from app.services.account_data import (
        expire_old_exports, pending_export_ids, process_due_deletions, process_export,
    )
    from app.services.credits import release_stale_reservations
    from app.services.support import retry_support_emails

    async with AsyncSessionLocal() as db:
        for request_id in await pending_export_ids(db):
            await process_export(db, request_id)
        expired = await expire_old_exports(db)
        deleted = await process_due_deletions(db)
        resent = await retry_support_emails(db)
        released = await release_stale_reservations(db)
    if expired or deleted or resent or released:
        logger.info(
            "Manutenção: cópias expiradas=%d, contas excluídas=%d, e-mails de suporte=%d, reservas liberadas=%d",
            expired, deleted, resent, released,
        )
