from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.grow import Grow
from app.models.knowledge import KnowledgeChunk
from app.models.plant import Plant
from app.services import ai_provider

# Phase → relevance filter map
_PHASE_FILTER = {
    "germination": ["all", "germination", "seedling"],
    "seedling": ["all", "germination", "seedling", "veg"],
    "veg": ["all", "veg"],
    "flower": ["all", "flower"],
    "harvest": ["all", "harvest", "flower"],
    "done": ["all"],
}


async def _get_dominant_phase(db: AsyncSession, grow: Grow | None) -> str | None:
    """Deriva a fase dominante buscando plantas do usuário dono do grow."""
    if not grow:
        return None
    # Após a refatoração, Plant não tem mais pot_id/grow_id direto.
    # Usamos grow_label para tentar correlacionar, mas a forma mais confiável
    # é buscar pela plant mais recente do usuário dono do grow.
    result = await db.execute(
        select(Plant)
        .where(Plant.user_id == grow.user_id, Plant.is_active == True)
        .order_by(Plant.created_at.desc())
        .limit(1)
    )
    plant = result.scalar_one_or_none()
    return plant.current_phase if plant else None


async def retrieve_chunks(
    db: AsyncSession,
    query: str,
    grow: Grow | None = None,
    top_k: int = 4,
) -> list[KnowledgeChunk]:
    from app.services.rag import get_ai_context

    config, _, embeddings = await get_ai_context(db)
    # Mesmo provider/modelo/dimensões usados na indexação — essencial, pois
    # vetores de modelos/dimensões diferentes não são comparáveis entre si
    # (ver app/services/ai_provider.embed_query para a normalização entre
    # provedores).
    query_vector = await ai_provider.embed_query(
        embeddings,
        query,
        provider=config.embedding_provider,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    phase = await _get_dominant_phase(db, grow)
    phase_tags = _PHASE_FILTER.get(phase, ["all"]) if phase else None

    # Build pgvector cosine similarity query
    vector_str = "[" + ",".join(str(v) for v in query_vector) + "]"

    if phase_tags:
        # Filter by phase relevance overlap — JSON array contains any of the phase tags
        phase_conditions = " OR ".join(
            f"phase_relevance @> '[{chr(34)}{tag}{chr(34)}]'::jsonb" for tag in phase_tags
        )
        query_sql = text(
            f"""
            SELECT * FROM knowledge_chunks
            WHERE ({phase_conditions}) OR phase_relevance IS NULL
            ORDER BY embedding <=> :vector
            LIMIT :limit
            """
        )
    else:
        query_sql = text(
            """
            SELECT * FROM knowledge_chunks
            ORDER BY embedding <=> :vector
            LIMIT :limit
            """
        )

    result = await db.execute(query_sql, {"vector": vector_str, "limit": top_k})
    rows = result.fetchall()

    # Map rows back to KnowledgeChunk objects
    chunks = []
    for row in rows:
        chunk_result = await db.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.id == row[0])
        )
        chunk = chunk_result.scalar_one_or_none()
        if chunk:
            chunks.append(chunk)

    return chunks
