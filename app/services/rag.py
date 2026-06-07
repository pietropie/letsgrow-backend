import logging
import time
from datetime import date, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.ai_config import AIConfig
from app.models.grow import Grow
from app.models.knowledge import AIConversation, KnowledgeChunk
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading
from app.rag.prompts import build_system_prompt, build_grow_context
from app.rag.retriever import retrieve_chunks
from app.services import ai_provider

logger = logging.getLogger(__name__)

# ─── Cache de configuração + clients de IA ───────────────────────────────────
#
# A config (provider/modelo/dimensões) agora vive na tabela ai_config e é
# editável em runtime pelo painel admin (/admin/ai-panel) — sem redeploy.
#
# Para não bater no banco a cada mensagem de chat / chunk indexado, mantemos
# um cache em memória do processo com TTL curto. Quando o TTL expira, relemos
# a config; se ela mudou desde a última leitura, reconstruímos os clients
# (LangChain) — caso contrário reaproveitamos os já instanciados.
_CACHE_TTL_SECONDS = 60

_cache: dict = {
    "config": None,
    "llm": None,
    "embeddings": None,
    "fetched_at": 0.0,
}


def _fallback_config() -> AIConfig:
    """Usado apenas se a tabela ai_config estiver vazia (não deveria acontecer
    após a migration de seed — é uma rede de segurança)."""
    return AIConfig(
        provider="gemini",
        chat_model=settings.GEMINI_MODEL,
        temperature=0.3,
        embedding_provider="gemini",
        embedding_model=settings.EMBEDDING_MODEL,
        embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
    )


def _signature(config: AIConfig) -> tuple:
    return (
        config.provider,
        config.chat_model,
        config.temperature,
        config.embedding_provider,
        config.embedding_model,
        config.embedding_dimensions,
    )


async def _load_config(db: AsyncSession) -> AIConfig:
    result = await db.execute(select(AIConfig).order_by(AIConfig.created_at.asc()).limit(1))
    config = result.scalar_one_or_none()
    return config if config is not None else _fallback_config()


async def _refresh_cache(db: AsyncSession) -> AIConfig:
    config = await _load_config(db)
    rebuild = _cache["config"] is None or _signature(_cache["config"]) != _signature(config)

    if rebuild:
        logger.info(
            "Config de IA (re)carregada — chat: %s/%s | embeddings: %s/%s (%dd)",
            config.provider, config.chat_model,
            config.embedding_provider, config.embedding_model, config.embedding_dimensions,
        )
        _cache["llm"] = ai_provider.build_llm(config.provider, config.chat_model, config.temperature)
        _cache["embeddings"] = ai_provider.build_embeddings(config.embedding_provider, config.embedding_model)

    _cache["config"] = config
    _cache["fetched_at"] = time.monotonic()
    return config


async def get_ai_context(db: AsyncSession) -> tuple[AIConfig, object, object]:
    """
    Retorna (config, llm, embeddings) já prontos para uso, lendo da ai_config
    no banco e respeitando o cache de ~60s. É o ponto único de entrada usado
    por chat() (abaixo), retriever.py e scripts/index_brain.py — assim, trocar
    o provedor/modelo no painel admin reflete em todo o app sem redeploy.
    """
    now = time.monotonic()
    if _cache["config"] is None or (now - _cache["fetched_at"]) > _CACHE_TTL_SECONDS:
        await _refresh_cache(db)
    return _cache["config"], _cache["llm"], _cache["embeddings"]


def invalidate_cache() -> None:
    """Chamado pelo endpoint PUT /admin/ai-config logo após salvar uma nova
    config, para que a mudança valha já na próxima requisição (em vez de
    esperar até 60s pelo TTL natural)."""
    _cache["config"] = None
    _cache["fetched_at"] = 0.0


async def chat(
    db: AsyncSession,
    conversation: AIConversation,
    user_message: str,
    grow: Grow | None = None,
) -> str:
    # Build grow context summary (compact — saves tokens)
    grow_ctx = await build_grow_context(db, grow) if grow else ""

    # Retrieve relevant knowledge chunks
    chunks = await retrieve_chunks(db, user_message, grow, top_k=4)
    rag_context = "\n\n---\n\n".join(c.content for c in chunks)

    system_prompt = build_system_prompt(grow_ctx, rag_context)

    # Build message history (last 6 messages to save tokens)
    history = conversation.messages[-6:] if conversation.messages else []
    messages = [("system", system_prompt)]
    for msg in history:
        messages.append((msg["role"], msg["content"]))
    messages.append(("human", user_message))

    _, llm, _ = await get_ai_context(db)
    response = await llm.ainvoke(messages)
    return response.content
