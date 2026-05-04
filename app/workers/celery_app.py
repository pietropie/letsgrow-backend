from celery import Celery

from app.config import settings

celery_app = Celery(
    "letsgrow",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.workers.knowledge"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="America/Sao_Paulo",
    enable_utc=True,
    beat_schedule={
        "reset-ai-monthly-quota": {
            "task": "app.workers.knowledge.reset_monthly_ai_quota",
            "schedule": 3600.0,  # check every hour; task is idempotent
        },
    },
)
