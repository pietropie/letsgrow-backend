"""
Endpoints administrativos — hoje, só a configuração de IA (provider/modelo).

Protegidos por um token compartilhado simples (header X-Admin-Token), não
pelo JWT de usuário comum: ainda não existe um conceito de "admin" no User,
e criar um exigiria migration + fluxo de promoção de usuário. Um token forte
guardado só no .env/Coolify resolve a necessidade real ("eu, Pietro, preciso
trocar isso rápido em uma emergência") sem esse trabalho extra. Ver
app/config.py:ADMIN_TOKEN para como gerar e configurar.
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.ai_config import AIConfig
from app.models.plant import Plant
from app.models.user import User
from app.services import ai_provider
from app.services.rag import invalidate_cache

logger = logging.getLogger(__name__)

router = APIRouter()


async def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    if not settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Painel admin desativado — configure ADMIN_TOKEN no ambiente do servidor.",
        )
    if not x_admin_token or x_admin_token != settings.ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token de admin inválido")


class AIConfigOut(BaseModel):
    id: uuid.UUID
    provider: str
    chat_model: str
    temperature: float
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    updated_by: str | None
    updated_at: datetime
    reindex_required: bool = False
    injection_message: str | None = None

    model_config = {"from_attributes": True}


class AIConfigIn(BaseModel):
    provider: str = Field(description="Provedor de chat: gemini | anthropic | openai")
    chat_model: str
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    embedding_provider: str = Field(description="Provedor de embedding: gemini | openai")
    embedding_model: str
    embedding_dimensions: int = Field(default=768, gt=0)
    updated_by: str | None = Field(default=None, description="Quem está alterando (ex.: seu e-mail) — fica registrado para auditoria")
    injection_message: str | None = Field(default=None, description="Mensagem personalizada quando o guard bloqueia prompt injection")


class AIProviderOptions(BaseModel):
    chat_providers: list[str]
    embedding_providers: list[str]


async def _get_or_create_config(db: AsyncSession) -> AIConfig:
    result = await db.execute(select(AIConfig).order_by(AIConfig.created_at.asc()).limit(1))
    config = result.scalar_one_or_none()
    if config is None:
        # Rede de segurança — a migration a2b3c4d5e6f7 já cria a linha seed,
        # mas cobrimos o caso de banco recriado fora do fluxo normal.
        config = AIConfig(
            provider="gemini",
            chat_model=settings.GEMINI_MODEL,
            temperature=0.3,
            embedding_provider="gemini",
            embedding_model=settings.EMBEDDING_MODEL,
            embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
        )
        db.add(config)
        await db.commit()
        await db.refresh(config)
    return config


@router.get("/ai-config/options", response_model=AIProviderOptions)
async def get_ai_provider_options(_: None = Depends(require_admin_token)):
    return AIProviderOptions(
        chat_providers=list(ai_provider.CHAT_PROVIDERS),
        embedding_providers=list(ai_provider.EMBEDDING_PROVIDERS),
    )


@router.get("/ai-config", response_model=AIConfigOut)
async def get_ai_config(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    config = await _get_or_create_config(db)
    return config


@router.put("/ai-config", response_model=AIConfigOut)
async def update_ai_config(
    body: AIConfigIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    provider = body.provider.strip().lower()
    embedding_provider = body.embedding_provider.strip().lower()

    if provider not in ai_provider.CHAT_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"provider inválido — use um de: {', '.join(ai_provider.CHAT_PROVIDERS)}",
        )
    if embedding_provider not in ai_provider.EMBEDDING_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"embedding_provider inválido — use um de: {', '.join(ai_provider.EMBEDDING_PROVIDERS)}",
        )

    config = await _get_or_create_config(db)

    embedding_changed = (
        config.embedding_provider != embedding_provider
        or config.embedding_model != body.embedding_model.strip()
        or config.embedding_dimensions != body.embedding_dimensions
    )

    config.provider = provider
    config.chat_model = body.chat_model.strip()
    config.temperature = body.temperature
    config.embedding_provider = embedding_provider
    config.embedding_model = body.embedding_model.strip()
    config.embedding_dimensions = body.embedding_dimensions
    config.updated_by = body.updated_by
    config.injection_message = body.injection_message.strip() if body.injection_message else None

    await db.commit()
    await db.refresh(config)

    # Faz a mudança valer já na próxima requisição, em vez de esperar o TTL
    # de ~60s do cache em app/services/rag.py
    invalidate_cache()

    response = AIConfigOut.model_validate(config)
    # Trocar provider/modelo/dimensões de EMBEDDING não atualiza vetores já
    # gravados — a busca por similaridade ficaria comparando vetores de
    # espaços diferentes. O painel mostra esse aviso e o comando para
    # reindexar (scripts/index_brain.py --wiki-path ... --clear).
    response.reindex_required = embedding_changed
    return response


# ─── Push notifications: schedule ────────────────────────────────────────────

class PushScheduleOut(BaseModel):
    enabled: bool
    hour: int
    minute: int
    # Resultado do último envio (None se nunca rodou)
    last_run_at: datetime | None = None
    last_stats: dict | None = None


class PushScheduleIn(BaseModel):
    enabled: bool = True
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)


@router.get("/push/schedule", response_model=PushScheduleOut)
async def get_push_schedule(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Retorna a configuração atual de agendamento de push."""
    import json as _json
    config = await _get_or_create_config(db)
    stats = None
    if config.daily_push_last_stats:
        try:
            stats = _json.loads(config.daily_push_last_stats)
        except Exception:
            pass
    return PushScheduleOut(
        enabled=config.daily_push_enabled,
        hour=config.daily_push_hour,
        minute=config.daily_push_minute,
        last_run_at=config.daily_push_last_run_at,
        last_stats=stats,
    )


@router.put("/push/schedule", response_model=PushScheduleOut)
async def update_push_schedule(
    body: PushScheduleIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Atualiza horário/ativação do push diário e reagenda o job imediatamente.
    O novo horário entra em vigor sem precisar reiniciar o servidor.
    """
    import json as _json
    from app.services.scheduler import reschedule

    config = await _get_or_create_config(db)
    config.daily_push_enabled = body.enabled
    config.daily_push_hour = body.hour
    config.daily_push_minute = body.minute
    await db.commit()
    await db.refresh(config)

    # Reagenda em tempo real
    reschedule(body.hour, body.minute, body.enabled)

    stats = None
    if config.daily_push_last_stats:
        try:
            stats = _json.loads(config.daily_push_last_stats)
        except Exception:
            pass
    return PushScheduleOut(
        enabled=config.daily_push_enabled,
        hour=config.daily_push_hour,
        minute=config.daily_push_minute,
        last_run_at=config.daily_push_last_run_at,
        last_stats=stats,
    )


# ─── Push notifications: disparo manual ──────────────────────────────────────

class DailyPushResult(BaseModel):
    users_processed: int
    pushed: int
    skipped_no_token: int
    skipped_no_plants: int
    errors: int


@router.post("/push/daily", response_model=DailyPushResult)
async def send_daily_push(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Dispara as notificações push diárias do Bob para todos os usuários ativos
    que tenham um expo_push_token registrado e pelo menos 1 planta ativa.

    Invocado pelo Coolify cron job (ou manualmente para testar):
        curl -X POST https://api.letsgrow.app/api/v1/admin/push/daily \\
             -H "X-Admin-Token: $ADMIN_TOKEN"

    Anti-spam: um usuário não recebe mais de 1 push/dia (controlado por
    daily_brief_sent_at no modelo User).
    """
    from app.services.daily_brief import generate_daily_brief, invalidate_brief
    from app.services.push import send_push

    # Busca usuários com token e plantas ativas
    users_result = await db.execute(
        select(User).where(
            User.is_active == True,  # noqa: E712
            User.expo_push_token.isnot(None),
        )
    )
    users = users_result.scalars().all()

    pushed = 0
    skipped_no_token = 0
    skipped_no_plants = 0
    errors = 0
    now = datetime.now(timezone.utc)

    for user in users:
        if not user.expo_push_token:
            skipped_no_token += 1
            continue

        # Anti-spam: pula se já foi enviado hoje
        if user.daily_brief_sent_at:
            last = user.daily_brief_sent_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < 82_800:  # menos de 23h
                continue

        # Verifica se tem plantas ativas
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
            # Força refresh do brief (sem cache) para ter dados frescos
            await invalidate_brief(user.id)
            brief = await generate_daily_brief(db, user.id, force_refresh=True)

            # Só envia se há algo relevante (não é só "tudo em dia")
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
            logger.error("Erro ao processar push para user %s: %s", user.id, exc)
            errors += 1

    await db.commit()
    logger.info(
        "Daily push concluído: %d pushed, %d sem token, %d sem plantas, %d erros",
        pushed, skipped_no_token, skipped_no_plants, errors,
    )
    return DailyPushResult(
        users_processed=len(users),
        pushed=pushed,
        skipped_no_token=skipped_no_token,
        skipped_no_plants=skipped_no_plants,
        errors=errors,
    )


# ─── Push notifications: envio personalizado ─────────────────────────────────

class CustomPushIn(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=300)
    target: str = Field(default="all", pattern="^(all|user|segment)$")
    user_email: str | None = None     # obrigatório se target == "user"
    deep_link: str | None = None      # ex: "/(app)/ai"
    # Filtros de segmento — usados quando target == "segment"
    segment_plans: list[str] | None = None
    segment_plant_phases: list[str] | None = None
    segment_harvest_within_days: int | None = None
    segment_has_sensor: bool | None = None
    segment_account_anniversary: bool | None = None
    segment_downgraded_within_days: int | None = None
    segment_inactive_for_days: int | None = None


class CustomPushResult(BaseModel):
    sent: int
    failed: int
    skipped_no_token: int


class SegmentPreviewIn(BaseModel):
    plans: list[str] | None = None
    plant_phases: list[str] | None = None
    harvest_within_days: int | None = None
    has_sensor: bool | None = None
    account_anniversary: bool | None = None
    downgraded_within_days: int | None = None
    inactive_for_days: int | None = None


class SegmentPreviewOut(BaseModel):
    count: int           # total de usuários no segmento
    pushable_count: int  # usuários com expo_push_token registrado
    sample_emails: list[str]


@router.post("/push/segment-preview", response_model=SegmentPreviewOut)
async def preview_segment(
    body: SegmentPreviewIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Retorna quantos usuários seriam atingidos pelos filtros informados,
    mais uma amostra de até 5 e-mails para validação.
    """
    from app.services.segment_evaluator import SegmentFilters, preview_segment as _preview

    filters = SegmentFilters(**body.model_dump())
    return await _preview(db, filters)


@router.post("/push/custom", response_model=CustomPushResult)
async def send_custom_push(
    body: CustomPushIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Envia notificação push personalizada.

    - target="all"     → broadcast para todos com token
    - target="user"    → somente o usuário com o e-mail informado
    - target="segment" → aplica filtros de segmento (segment_*)
    """
    from app.services.push import send_push, send_push_batch
    from app.services.segment_evaluator import SegmentFilters, evaluate_segment

    data: dict = {"type": "custom"}
    if body.deep_link:
        data["deep_link"] = body.deep_link

    # ── Usuário específico ───────────────────────────────────────────────────
    if body.target == "user":
        if not body.user_email:
            raise HTTPException(status_code=422, detail="user_email é obrigatório para target='user'")
        user_result = await db.execute(
            select(User).where(User.email == body.user_email, User.is_active == True)  # noqa: E712
        )
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado ou inativo.")
        if not user.expo_push_token:
            return CustomPushResult(sent=0, failed=0, skipped_no_token=1)
        ok = await send_push(token=user.expo_push_token, title=body.title, body=body.body, data=data)
        return CustomPushResult(sent=1 if ok else 0, failed=0 if ok else 1, skipped_no_token=0)

    # ── Segmento ─────────────────────────────────────────────────────────────
    if body.target == "segment":
        filters = SegmentFilters(
            plans=body.segment_plans,
            plant_phases=body.segment_plant_phases,
            harvest_within_days=body.segment_harvest_within_days,
            has_sensor=body.segment_has_sensor,
            account_anniversary=body.segment_account_anniversary,
            downgraded_within_days=body.segment_downgraded_within_days,
            inactive_for_days=body.segment_inactive_for_days,
        )
        users = await evaluate_segment(db, filters)
        tokens = [u.expo_push_token for u in users if u.expo_push_token]
        if not tokens:
            return CustomPushResult(sent=0, failed=0, skipped_no_token=0)
        stats = await send_push_batch(tokens, title=body.title, body=body.body, data=data)
        logger.info("Custom push (segment): sent=%d failed=%d", stats["sent"], stats["failed"])
        return CustomPushResult(sent=stats["sent"], failed=stats["failed"], skipped_no_token=0)

    # ── Broadcast (all) ──────────────────────────────────────────────────────
    users_result = await db.execute(
        select(User).where(User.is_active == True, User.expo_push_token.isnot(None))  # noqa: E712
    )
    users = users_result.scalars().all()
    tokens = [u.expo_push_token for u in users if u.expo_push_token]
    stats = await send_push_batch(tokens, title=body.title, body=body.body, data=data)
    logger.info("Custom push (broadcast): sent=%d failed=%d", stats["sent"], stats["failed"])
    return CustomPushResult(sent=stats["sent"], failed=stats["failed"], skipped_no_token=0)
