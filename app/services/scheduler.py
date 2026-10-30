"""
Scheduler de tarefas periódicas — usa APScheduler (AsyncIOScheduler).

Responsabilidades:
  - Disparar o envio de push notifications diárias no horário configurado
    pelo admin via painel (/notificacoes).
  - Permitir reagendamento dinâmico sem restart do servidor.

Uso no lifespan do FastAPI (app/main.py):
    from app.services.scheduler import scheduler, setup_daily_push
    scheduler.start()
    await setup_daily_push()   # lê config do banco e agenda
    yield
    scheduler.shutdown(wait=False)
"""
import json
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

_JOB_ID = "daily_push"
_PERSONAL_JOB_ID = "personal_push"
_MAINTENANCE_JOB_ID = "account_maintenance"
_BILLING_JOB_ID = "billing_reconcile"

# Instância global — importada pelo main.py e pelo endpoint admin
scheduler = AsyncIOScheduler(timezone="UTC")


async def _run_daily_push() -> None:
    """
    Callback chamado pelo scheduler no horário configurado pelo admin.
    Atende quem nunca salvou preferências de acompanhamento (comportamento
    anterior); quem salvou é atendido por _run_personal_push no horário local.
    Cria sua própria sessão de banco para não depender do ciclo de request.
    """
    logger.info("Scheduler: iniciando envio de push diário")
    try:
        from app.database import AsyncSessionLocal
        from app.api.v1.admin import _get_or_create_config
        from app.services.notification_delivery import deliver_daily_briefs

        async with AsyncSessionLocal() as db:
            now = datetime.now(timezone.utc)
            stats = await deliver_daily_briefs(db, scope="default", now=now)
            logger.info("Push diário concluído: %s", stats)

            # Persiste resultado e timestamp na ai_config
            try:
                config = await _get_or_create_config(db)
                config.daily_push_last_run_at = now
                config.daily_push_last_stats = json.dumps(stats)
                await db.commit()
            except Exception as exc:
                logger.warning("Não foi possível salvar stats do push: %s", exc)

    except Exception as exc:
        logger.error("Erro crítico no scheduler de push: %s", exc, exc_info=True)


async def _run_personal_push() -> None:
    """A cada 15 min: resumo diário de quem escolheu horário próprio."""
    try:
        from app.database import AsyncSessionLocal
        from app.api.v1.admin import _get_or_create_config
        from app.services.notification_delivery import deliver_daily_briefs

        async with AsyncSessionLocal() as db:
            config = await _get_or_create_config(db)
            if not config.daily_push_enabled:
                return
            stats = await deliver_daily_briefs(db, scope="personal")
            if stats["users_processed"]:
                logger.info("Push diário (horário do usuário): %s", stats)
    except Exception as exc:
        logger.error("Erro no push por horário do usuário: %s", exc, exc_info=True)


async def _run_account_maintenance() -> None:
    """Cópias de dados pendentes, exclusões vencidas e reenvio de e-mails de suporte."""
    try:
        from app.services.account_maintenance import run_account_maintenance
        await run_account_maintenance()
    except Exception as exc:
        logger.error("Erro na manutenção de contas: %s", exc, exc_info=True)


async def _run_billing_reconcile() -> None:
    """Pagamentos: confere no provedor o que um webhook perdido deixaria errado."""
    try:
        from app.database import AsyncSessionLocal
        from app.services.billing import reconcile

        async with AsyncSessionLocal() as db:
            stats = await reconcile(db)
        if any(stats.values()):
            logger.info("Reconciliação de pagamentos: %s", stats)
    except Exception as exc:
        logger.error("Erro na reconciliação de pagamentos: %s", exc, exc_info=True)


def _schedule_job(hour: int, minute: int) -> None:
    """Registra (ou substitui) o job de push diário no scheduler."""
    if scheduler.get_job(_JOB_ID):
        scheduler.remove_job(_JOB_ID)
    scheduler.add_job(
        _run_daily_push,
        trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        id=_JOB_ID,
        name="Daily Push Bob",
        replace_existing=True,
        misfire_grace_time=300,  # até 5 min de atraso tolerado
    )
    logger.info("Push diário agendado para %02d:%02d UTC", hour, minute)


def reschedule(hour: int, minute: int, enabled: bool) -> None:
    """
    Reagenda o job imediatamente — chamado pelo endpoint PUT /admin/push/schedule
    após salvar a nova config no banco.
    """
    if not enabled:
        if scheduler.get_job(_JOB_ID):
            scheduler.remove_job(_JOB_ID)
            logger.info("Push diário desabilitado — job removido")
        return
    _schedule_job(hour, minute)


def setup_background_jobs() -> None:
    """Jobs fixos: push no horário de cada usuário e manutenção de contas."""
    scheduler.add_job(
        _run_personal_push,
        trigger=CronTrigger(minute="*/15", timezone="UTC"),
        id=_PERSONAL_JOB_ID,
        name="Push diário no horário do usuário",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _run_account_maintenance,
        trigger=CronTrigger(minute="*/10", timezone="UTC"),
        id=_MAINTENANCE_JOB_ID,
        name="Manutenção de contas (cópias, exclusões, suporte)",
        replace_existing=True,
        misfire_grace_time=600,
    )
    scheduler.add_job(
        _run_billing_reconcile,
        trigger=CronTrigger(minute=7, timezone="UTC"),
        id=_BILLING_JOB_ID,
        name="Reconciliação de pagamentos (Mercado Pago e Google Play)",
        replace_existing=True,
        misfire_grace_time=1800,
    )


async def setup_daily_push() -> None:
    """
    Lê a configuração atual do banco e agenda o job.
    Chamado uma vez no lifespan do FastAPI, após o scheduler.start().
    """
    try:
        from app.database import AsyncSessionLocal
        from app.api.v1.admin import _get_or_create_config

        async with AsyncSessionLocal() as db:
            config = await _get_or_create_config(db)
            if config.daily_push_enabled:
                _schedule_job(config.daily_push_hour, config.daily_push_minute)
            else:
                logger.info("Push diário desabilitado na config — job não agendado")
    except Exception as exc:
        logger.warning("Não foi possível ler config de push no startup: %s", exc)
        # Fallback: agenda no horário padrão (09:00 UTC)
        _schedule_job(9, 0)
