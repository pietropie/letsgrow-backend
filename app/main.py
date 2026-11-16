import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.config import settings
from app.services.rate_limit import RateLimitMiddleware

logger = logging.getLogger(__name__)

# O uvicorn só configura os próprios loggers; sem isto os logger.info de app.*
# (startup, trechos do RAG por conversa etc.) não aparecem nos logs do Coolify.
_app_logger = logging.getLogger("app")
if not _app_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
    _app_logger.addHandler(_handler)
    _app_logger.setLevel(logging.INFO)
    _app_logger.propagate = False


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
        from app.services.scheduler import scheduler, setup_background_jobs, setup_daily_push
        scheduler.start()
        await setup_daily_push()
        setup_background_jobs()
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
        # A documentação é o mapa de todas as rotas: só com API_DOCS_ENABLED (desenvolvimento)
        docs_url="/api-docs" if settings.API_DOCS_ENABLED else None,
        redoc_url="/api-redoc" if settings.API_DOCS_ENABLED else None,
        openapi_url="/api-docs/openapi.json" if settings.API_DOCS_ENABLED else None,
        lifespan=lifespan,
    )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        # Erro sem tratamento: o detalhe fica no log; o cliente recebe uma mensagem genérica
        logger.exception("Erro sem tratamento em %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Erro interno. Tente de novo em instantes."})

    # Antes do CORS: o 429 também sai com os cabeçalhos de CORS (o último adicionado fica por fora)
    app.add_middleware(RateLimitMiddleware)

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

    # O antigo /admin/ai-panel (página pública que listava as rotas do admin) saiu: a troca de
    # provedor e modelo de IA fica no letsgrow-admin, que chama /api/v1/admin/ai-config com o token.
    return app


app = create_app()
