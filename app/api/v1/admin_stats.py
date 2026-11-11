"""
Endpoints administrativos — visão geral, RAG/Consultoria e status de integrações.

Não substituem nenhum dashboard de monitoramento "de verdade" — são consultas
simples (contagens/agregações) para o painel admin dar visibilidade rápida do
estado do app sem precisar abrir o banco na mão.

Protegidos pelo mesmo X-Admin-Token de app/api/v1/admin.py.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Date, func, or_, select, text, type_coerce
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, load_only

from app.api.v1.admin import require_admin_token
from app.config import settings
from app.database import get_db
from app.models.billing import ACCESS_STATUSES, Subscription
from app.models.credits import CreditPurchase
from app.models.grow import Grow
from app.models.knowledge import AIConversation, KnowledgeChunk
from app.models.plant import Plant
from app.models.strain import Strain
from app.models.user import User
from app.services.billing import grants_access

router = APIRouter()
BUSINESS_TIME_ZONE = ZoneInfo("America/Sao_Paulo")


def _interval(value: str):
    """Helper para `now() - interval '...'` em filtros — evita parametrizar
    intervalos como string (asyncpg não aceita bind direto para INTERVAL)."""
    return text(f"interval '{value}'")


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

class OverviewStats(BaseModel):
    users_total: int
    users_by_plan: dict[str, int]
    grows_total: int
    plants_total: int
    strains_total: int
    conversations_total: int
    knowledge_chunks_total: int


@router.get("/stats/overview", response_model=OverviewStats)
async def get_overview_stats(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    users_total = (await db.execute(select(func.count()).select_from(User))).scalar_one()

    plan_rows = (
        await db.execute(select(User.plan, func.count()).group_by(User.plan))
    ).all()
    users_by_plan = {plan: count for plan, count in plan_rows}

    grows_total = (await db.execute(select(func.count()).select_from(Grow))).scalar_one()
    plants_total = (await db.execute(select(func.count()).select_from(Plant))).scalar_one()
    strains_total = (await db.execute(select(func.count()).select_from(Strain))).scalar_one()
    conversations_total = (
        await db.execute(select(func.count()).select_from(AIConversation))
    ).scalar_one()
    knowledge_chunks_total = (
        await db.execute(select(func.count()).select_from(KnowledgeChunk))
    ).scalar_one()

    return OverviewStats(
        users_total=users_total,
        users_by_plan=users_by_plan,
        grows_total=grows_total,
        plants_total=plants_total,
        strains_total=strains_total,
        conversations_total=conversations_total,
        knowledge_chunks_total=knowledge_chunks_total,
    )


# ---------------------------------------------------------------------------
# Indicadores de negócio
# ---------------------------------------------------------------------------

class BusinessPeriod(BaseModel):
    start_date: date
    end_date: date
    new_users: int
    paid_credit_purchases: int


class BusinessDay(BaseModel):
    date: date
    new_users: int
    paid_credit_purchases: int


class BusinessStats(BaseModel):
    current: BusinessPeriod
    previous: BusinessPeriod
    active_subscribers: int
    daily: list[BusinessDay]


def _business_daily_counts(
    timestamp: InstrumentedAttribute,
    start: datetime,
    end: datetime,
    previous_start: datetime,
    previous_end: datetime,
):
    local_date = type_coerce(func.date(func.timezone(BUSINESS_TIME_ZONE.key, timestamp)), Date)
    return (
        select(local_date.label("day"), func.count())
        .where(timestamp >= previous_start, timestamp < end, or_(timestamp >= start, timestamp < previous_end))
        .group_by(local_date)
    )


@router.get("/stats/business", response_model=BusinessStats)
async def get_business_stats(
    days: Literal["7", "30", "90"] = "30",
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(BUSINESS_TIME_ZONE)
    period_length = timedelta(days=int(days))
    current_date = local_now.date() - period_length + timedelta(days=1)
    previous_date = current_date - period_length
    start = datetime.combine(current_date, time.min, BUSINESS_TIME_ZONE).astimezone(timezone.utc)
    previous_start = datetime.combine(previous_date, time.min, BUSINESS_TIME_ZONE).astimezone(timezone.utc)
    # O último dia do período anterior termina à mesma hora local de hoje.
    previous_end = (local_now - period_length).astimezone(timezone.utc)

    new_users = dict((await db.execute(
        _business_daily_counts(User.created_at, start, now, previous_start, previous_end)
    )).all())
    paid_credit_purchases = dict((await db.execute(
        _business_daily_counts(CreditPurchase.paid_at, start, now, previous_start, previous_end)
        .where(CreditPurchase.status == "paid")
    )).all())

    # Limita a consulta aos candidatos e aplica a mesma regra de acesso usada no app.
    earliest_end = now - timedelta(days=max(0, settings.BILLING_GRACE_DAYS))
    subscriptions = (await db.execute(
        select(Subscription)
        .options(load_only(
            Subscription.user_id, Subscription.status, Subscription.provider, Subscription.kind,
            Subscription.current_period_start, Subscription.current_period_end,
        ))
        .where(
            Subscription.user_id.is_not(None),
            Subscription.status.in_(ACCESS_STATUSES),
            Subscription.current_period_end > earliest_end,
            or_(Subscription.current_period_start.is_(None), Subscription.current_period_start <= now),
        )
    )).scalars().all()
    active_subscribers = len({sub.user_id for sub in subscriptions if grants_access(sub, now)})

    dates = (current_date + timedelta(days=offset) for offset in range(period_length.days))
    daily = [
        BusinessDay(
            date=day,
            new_users=new_users.get(day, 0),
            paid_credit_purchases=paid_credit_purchases.get(day, 0),
        )
        for day in dates
    ]
    return BusinessStats(
        current=BusinessPeriod(
            start_date=current_date,
            end_date=local_now.date(),
            new_users=sum(day.new_users for day in daily),
            paid_credit_purchases=sum(day.paid_credit_purchases for day in daily),
        ),
        previous=BusinessPeriod(
            start_date=previous_date,
            end_date=current_date - timedelta(days=1),
            new_users=sum(count for day, count in new_users.items() if day < current_date),
            paid_credit_purchases=sum(count for day, count in paid_credit_purchases.items() if day < current_date),
        ),
        active_subscribers=active_subscribers,
        daily=daily,
    )


# ---------------------------------------------------------------------------
# RAG (base de conhecimento indexada do wiki)
# ---------------------------------------------------------------------------

class RagSourceTypeCount(BaseModel):
    source_type: str
    count: int


class RagStats(BaseModel):
    total_chunks: int
    chunks_by_source_type: list[RagSourceTypeCount]
    chunks_with_embedding: int
    chunks_without_embedding: int
    last_indexed_at: datetime | None
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int


@router.get("/stats/rag", response_model=RagStats)
async def get_rag_stats(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    total_chunks = (
        await db.execute(select(func.count()).select_from(KnowledgeChunk))
    ).scalar_one()

    by_type_rows = (
        await db.execute(
            select(KnowledgeChunk.source_type, func.count())
            .group_by(KnowledgeChunk.source_type)
            .order_by(func.count().desc())
        )
    ).all()

    with_embedding = (
        await db.execute(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.embedding.is_not(None))
        )
    ).scalar_one()

    last_indexed_at = (
        await db.execute(select(func.max(KnowledgeChunk.created_at)))
    ).scalar_one()

    # A config "ativa" vem da mesma fonte usada em produção (ai_config, com
    # fallback para os defaults de app/config.py) — ver app/services/rag.py.
    from app.services.rag import get_ai_context

    ai_config, _llm, _embeddings = await get_ai_context(db)

    return RagStats(
        total_chunks=total_chunks,
        chunks_by_source_type=[
            RagSourceTypeCount(source_type=st or "desconhecido", count=count) for st, count in by_type_rows
        ],
        chunks_with_embedding=with_embedding,
        chunks_without_embedding=total_chunks - with_embedding,
        last_indexed_at=last_indexed_at,
        embedding_provider=ai_config.embedding_provider,
        embedding_model=ai_config.embedding_model,
        embedding_dimensions=ai_config.embedding_dimensions,
    )


# ---------------------------------------------------------------------------
# Consultoria (conversas com a IA)
# ---------------------------------------------------------------------------

class ConsultoriaStats(BaseModel):
    conversations_total: int
    conversations_last_7d: int
    conversations_last_30d: int
    active_users_last_30d: int
    ai_queries_this_month_total: int
    top_users_by_usage: list["TopUserUsage"]


class TopUserUsage(BaseModel):
    user_id: str
    email: str
    plan: str
    ai_queries_this_month: int
    conversations_count: int


ConsultoriaStats.model_rebuild()


@router.get("/stats/consultoria", response_model=ConsultoriaStats)
async def get_consultoria_stats(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    now = func.now()

    conversations_total = (
        await db.execute(select(func.count()).select_from(AIConversation))
    ).scalar_one()

    conversations_last_7d = (
        await db.execute(
            select(func.count())
            .select_from(AIConversation)
            .where(AIConversation.created_at >= now - _interval("7 days"))
        )
    ).scalar_one()

    conversations_last_30d = (
        await db.execute(
            select(func.count())
            .select_from(AIConversation)
            .where(AIConversation.created_at >= now - _interval("30 days"))
        )
    ).scalar_one()

    active_users_last_30d = (
        await db.execute(
            select(func.count(func.distinct(AIConversation.user_id))).where(
                AIConversation.created_at >= now - _interval("30 days")
            )
        )
    ).scalar_one()

    ai_queries_total = (
        await db.execute(select(func.coalesce(func.sum(User.ai_queries_this_month), 0)))
    ).scalar_one()

    top_rows = (
        await db.execute(
            select(
                User.id,
                User.email,
                User.plan,
                User.ai_queries_this_month,
                func.count(AIConversation.id).label("conversations_count"),
            )
            .outerjoin(AIConversation, AIConversation.user_id == User.id)
            .group_by(User.id)
            .order_by(User.ai_queries_this_month.desc())
            .limit(10)
        )
    ).all()

    return ConsultoriaStats(
        conversations_total=conversations_total,
        conversations_last_7d=conversations_last_7d,
        conversations_last_30d=conversations_last_30d,
        active_users_last_30d=active_users_last_30d,
        ai_queries_this_month_total=int(ai_queries_total),
        top_users_by_usage=[
            TopUserUsage(
                user_id=str(row.id),
                email=row.email,
                plan=row.plan,
                ai_queries_this_month=row.ai_queries_this_month,
                conversations_count=row.conversations_count,
            )
            for row in top_rows
        ],
    )


# ---------------------------------------------------------------------------
# Integrações / chaves de API configuradas
# ---------------------------------------------------------------------------

class IntegrationStatus(BaseModel):
    name: str
    key_configured: bool
    used_for: str


class IntegrationsStatus(BaseModel):
    integrations: list[IntegrationStatus]


@router.get("/integrations", response_model=IntegrationsStatus)
async def get_integrations_status(_: None = Depends(require_admin_token)):
    """Mostra só SE cada chave está configurada — nunca o valor da chave."""
    return IntegrationsStatus(
        integrations=[
            IntegrationStatus(
                name="Google Gemini",
                key_configured=bool(settings.GOOGLE_API_KEY),
                used_for="Chat + embeddings (provider 'gemini')",
            ),
            IntegrationStatus(
                name="Anthropic (Claude)",
                key_configured=bool(settings.ANTHROPIC_API_KEY),
                used_for="Chat (provider 'anthropic')",
            ),
            IntegrationStatus(
                name="OpenAI",
                key_configured=bool(settings.OPENAI_API_KEY),
                used_for="Chat + embeddings (provider 'openai')",
            ),
            IntegrationStatus(
                name="DeepSeek",
                key_configured=bool(settings.DEEPSEEK_API_KEY),
                used_for="Chat (provider 'deepseek')",
            ),
            IntegrationStatus(
                name="Z.ai (GLM)",
                key_configured=bool(settings.ZAI_API_KEY),
                used_for="Chat (provider 'zai')",
            ),
            IntegrationStatus(
                name="Google OAuth",
                key_configured=bool(settings.GOOGLE_CLIENT_ID),
                used_for="Login social no app mobile",
            ),
            IntegrationStatus(
                name="MinIO / armazenamento de fotos",
                key_configured=bool(settings.MINIO_ACCESS_KEY and settings.MINIO_SECRET_KEY),
                used_for="Upload e exibição de fotos dos eventos",
            ),
            IntegrationStatus(
                name="MQTT (sensores)",
                key_configured=bool(settings.MQTT_HOST and settings.MQTT_HOST != "localhost"),
                used_for="Ingestão de leituras dos sensores Arduino/ESP32",
            ),
        ]
    )
