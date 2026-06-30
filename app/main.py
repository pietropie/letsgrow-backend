import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.api.v1.router import api_router
from app.config import settings
from app.web.admin_panel import AI_PANEL_HTML

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.storage import ensure_buckets
    try:
        ensure_buckets()
        logger.info("MinIO buckets ok")
    except Exception as exc:
        logger.warning("MinIO não disponível no startup: %s", exc)

    mqtt_task = None
    if settings.MQTT_HOST:
        from app.workers.mqtt_listener import run_mqtt_listener
        mqtt_task = asyncio.create_task(run_mqtt_listener())
        logger.info("MQTT listener started")

    # Scheduler de push diário (APScheduler)
    _scheduler = None
    try:
        from app.services.scheduler import scheduler, setup_daily_push
        scheduler.start()
        await setup_daily_push()
        _scheduler = scheduler
        logger.info("APScheduler iniciado")
    except ImportError:
        logger.warning("apscheduler não instalado — scheduler de push desabilitado")
    except Exception as exc:
        logger.warning("Falha ao iniciar scheduler: %s", exc)

    yield

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)

    if mqtt_task:
        mqtt_task.cancel()
        try:
            await mqtt_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description=(
            "API do Let's Grow — plataforma de cultivo assistida por IA.\n\n"
            "**Autenticação:** `Bearer <JWT>` em todos os endpoints protegidos.\n"
            "Endpoints `/admin/*` exigem `X-Admin-Token: <ADMIN_TOKEN>` no header."
        ),
        docs_url="/api-docs",
        redoc_url="/api-redoc",
        openapi_url="/api-docs/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api/v1")

    @app.get("/health", tags=["health"])
    async def health():
        return {"status": "ok", "version": settings.APP_VERSION}

    @app.get("/admin/ai-panel", response_class=HTMLResponse, tags=["admin"], include_in_schema=False)
    async def ai_panel():
        """
        Painel HTML mínimo para trocar provider/modelo de IA em runtime —
        protegido client-side pelo token enviado a /api/v1/admin/ai-config
        (ver app/web/admin_panel.py e app/api/v1/admin.py). A própria página
        é pública, mas sem o ADMIN_TOKEN correto nenhuma chamada à API funciona.
        """
        return HTMLResponse(content=AI_PANEL_HTML)

    return app


app = create_app()
