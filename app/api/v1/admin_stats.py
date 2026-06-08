"""
Endpoints administrativos — visão geral, RAG/Consultoria e status de integrações.

Não substituem nenhum dashboard de monitoramento "de verdade" — são consultas
simples (contagens/agregações) para o painel admin dar visibilidade rápida do
estado do app sem precisar abrir o banco na mão.

Protegidos pelo mesmo X-Admin-Token de app/api/v1/admin.py.
"""
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.config import settings
from app.database import get_db
from app.models.grow import Grow
from app.models.knowledge import AIConversation, KnowledgeChunk
from app.models.plant import Plant
from app.models.strain import Strain
from app.models.user import User

router = APIRouter()


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
