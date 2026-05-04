from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.grow import Grow
from app.models.knowledge import KnowledgeChunk
from app.models.plant import Plant
from app.models.pot import Pot

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
    if not grow:
        return None
    result = await db.execute(
        select(Plant)
        .join(Pot)
        .where(Pot.grow_id == grow.id, Plant.is_active == True)
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
    from app.services.rag import get_embeddings

    embeddings = get_embeddings()
    query_vector = await embeddings.aembed_query(query)

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
