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

# Instância global — importada pelo main.py e pelo endpoint admin
scheduler = AsyncIOScheduler(timezone="UTC")


async def _run_daily_push() -> None:
    """
    Callback chamado pelo scheduler no horário configurado.
    Cria sua própria sessão de banco para não depender do ciclo de request.
    """
    logger.info("Scheduler: iniciando envio de push diário")
    try:
        from app.database import AsyncSessionLocal
        from app.api.v1.admin import _get_or_create_config
        from app.services.daily_brief import invalidate_brief
        from app.services.push import send_push
        from app.models.plant import Plant
        from app.models.user import User
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            users_result = await db.execute(
                select(User).where(
                    User.is_active == True,  # noqa: E712
                    User.expo_push_token.isnot(None),
                )
            )
            users = users_result.scalars().all()

            pushed = skipped_no_token = skipped_no_plants = errors = 0
            now = datetime.now(timezone.utc)

            for user in users:
                if not user.expo_push_token:
                    skipped_no_token += 1
                    continue
                if user.daily_brief_sent_at:
                    last = user.daily_brief_sent_at
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if (now - last).total_seconds() < 82_800:
                        continue

                plant_count = (
                    await db.execute(
                        select(Plant.id).where(
                            Plant.user_id == user.id,
                            Plant.is_active == True,  # noqa: E712
                        ).limit(1)
                    )
                ).scalar_one_or_none()

                if not plant_count:
                    skipped_no_plants += 1
                    continue

                try:
                    from app.services.daily_brief import generate_daily_brief
                    await invalidate_brief(user.id)
                    brief = await generate_daily_brief(db, user.id, force_refresh=True)
                    ok = await send_push(
                        token=user.expo_push_token,
                        title=brief.title,
                        body=brief.body,
                        data={"type": "daily_brief", "urgent_count": brief.urgent_count},
                    )
                    if ok:
                        pushed += 1
                        user.daily_brief_sent_at = now
                    else:
                        errors += 1
                except Exception as exc:
                    logger.error("Push para user %s falhou: %s", user.id, exc)
                    errors += 1

            await db.commit()

            stats = {
                "users_processed": len(users),
                "pushed": pushed,
                "skipped_no_token": skipped_no_token,
                "skipped_no_plants": skipped_no_plants,
                "errors": errors,
            }
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
