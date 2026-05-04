"""
Celery tasks for knowledge base maintenance.
"""
import asyncio
from datetime import datetime, timezone

from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.knowledge.reset_monthly_ai_quota")
def reset_monthly_ai_quota() -> None:
    """Reset ai_queries_this_month for users whose reset_at is in the past month."""
    asyncio.run(_reset_quota())


async def _reset_quota() -> None:
    from sqlalchemy import update
    from app.database import AsyncSessionLocal
    from app.models.user import User

    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        await db.execute(
            update(User)
            .where(
                User.ai_queries_reset_at
                < now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            )
            .values(
                ai_queries_this_month=0,
                ai_queries_reset_at=now,
            )
        )
        await db.commit()
