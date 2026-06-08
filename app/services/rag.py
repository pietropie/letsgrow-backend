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

    # Armazena uma cópia transient (desvinculada de qualquer Session) para
    # evitar o erro "Instance is not bound to a Session" quando o cache é
    # acessado em requisições futuras (onde a sessão original já fechou).
    _cache["config"] = AIConfig(
        provider=config.provider,
        chat_model=config.chat_model,
        temperature=config.temperature,
        embedding_provider=config.embedding_provider,
        embedding_model=config.embedding_model,
        embedding_dimensions=config.embedding_dimensions,
    )
    _cache["fetched_at"] = time.monotonic()
    return _cache["config"]


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


# ─── Análise de fotos de eventos do diário (multimodal) ──────────────────────
#
# Usa o mesmo `llm` resolvido via get_ai_context (cache de ~60s, configurável
# pelo painel admin) — mas aqui montamos uma mensagem multimodal (texto + N
# imagens em base64) seguindo o formato padrão de "content blocks" do
# LangChain (`image_url` com data URI), suportado nativamente por
# ChatGoogleGenerativeAI/ChatOpenAI/ChatAnthropic. Provedores sem suporte a
# visão (DeepSeek/Z.ai) vão simplesmente falhar na chamada — é o admin quem
# escolhe o provider/modelo em /admin/ai-panel, então recomendamos Gemini.

def _build_photo_analysis_prompt(plant: Plant, event) -> str:
    notes = f"\nObservações registradas pelo cultivador neste evento: {event.notes}" if event.notes else ""
    return (
        "Você é um consultor especialista em cultivo de cannabis analisando fotos "
        f"enviadas por um cultivador. Planta: {plant.strain_name} "
        f"(fase atual: {plant.current_phase}). Tipo do evento do diário: {event.event_type}."
        f"{notes}\n\n"
        "Com base SOMENTE no que é visível nas imagens, escreva uma análise objetiva "
        "cobrindo:\n"
        "1) Sinais de problemas visíveis (deficiências nutricionais, pragas, doenças, "
        "estresse hídrico/luminoso, queima de nutrientes, etc.) — ou ausência deles;\n"
        "2) Estágio de desenvolvimento e estado geral de saúde aparente;\n"
        "3) Recomendações práticas e específicas para o cultivador.\n\n"
        "Seja direto e use linguagem acessível. Se as fotos não permitirem uma "
        "avaliação confiável (ângulo ruim, baixa qualidade, etc.), diga isso "
        "explicitamente em vez de especular."
    )


def _read_photo_object(object_key: str) -> tuple[bytes, str]:
    """Lê os bytes de uma foto direto do MinIO (client interno — roda em thread,
    pois o SDK do MinIO é síncrono)."""
    from app.services.storage import BUCKET_EVENTS, get_minio_client

    response = get_minio_client().get_object(BUCKET_EVENTS, object_key)
    try:
        data = response.read()
        content_type = response.headers.get("Content-Type") or "image/jpeg"
        return data, content_type
    finally:
        response.close()
        response.release_conn()


async def analyze_event_photos(db: AsyncSession, *, plant: Plant, event) -> str:
    """Envia as fotos de um evento do diário para o LLM multimodal configurado
    e retorna uma análise em texto (problemas visíveis, estágio, recomendações)."""
    import asyncio
    import base64

    from langchain_core.messages import HumanMessage

    content_blocks: list[dict] = [
        {"type": "text", "text": _build_photo_analysis_prompt(plant, event)}
    ]

    for object_key in (event.photo_keys or [])[:6]:
        try:
            data, content_type = await asyncio.to_thread(_read_photo_object, object_key)
        except Exception as exc:
            logger.warning("Falha ao baixar foto %s para análise de IA: %s", object_key, exc)
            continue
        b64 = base64.b64encode(data).decode("ascii")
        content_blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:{content_type};base64,{b64}"},
        })

    if len(content_blocks) == 1:
        raise RuntimeError("Não foi possível carregar nenhuma das fotos deste evento para análise.")

    _, llm, _ = await get_ai_context(db)
    response = await llm.ainvoke([HumanMessage(content=content_blocks)])
    return response.content
