"""Admin endpoints for the conversation → RAG pipeline.

Flow:
  1. Admin lists all customer conversations (GET /admin/ai/conversations)
  2. Admin (or an automated job) triggers extraction for a specific conversation
     (POST /admin/ai/conversations/{id}/extract)
     → LLM analyses the conversation and creates KnowledgeChunk(s) with status="draft"
  3. Admin reviews draft chunks — edits content, reads confidence + reasoning
  4. Admin approves (PATCH .../approve) → status="active" → chunk enters the RAG
     Admin rejects (PATCH .../reject) → status="rejected" → chunk never surfaces

All routes require a valid ADMIN_TOKEN header (reusing the existing admin dependency).
"""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token as require_admin
from app.database import get_db
from app.models.knowledge import AIConversation, KnowledgeChunk
from app.models.user import User

router = APIRouter()


# ─── Schemas ──────────────────────────────────────────────────────────────────


class ConversationAdminItem(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_email: str | None
    title: str | None
    plant_id: uuid.UUID | None
    grow_id: uuid.UUID | None
    message_count: int
    created_at: datetime
    updated_at: datetime
    extracted: bool  # True if at least one draft/active chunk was extracted


class DraftChunkOut(BaseModel):
    id: uuid.UUID
    content: str
    source_type: str
    phase_relevance: list | None
    tags: list | None
    confidence_score: float | None
    extraction_reasoning: str | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ExtractResponse(BaseModel):
    conversation_id: uuid.UUID
    chunks_created: int
    chunks: list[DraftChunkOut]


class PatchChunkBody(BaseModel):
    content: str | None = None
    source_type: str | None = None
    phase_relevance: list | None = None
    tags: list | None = None


# ─── Extraction prompt ────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """Voce e um especialista em cannabis indoor/outdoor e seu papel e extrair conhecimento anonimizado de conversas entre cultivadores e um consultor de cultivo (Bob).

Seu objetivo e identificar INSIGHTS DE CULTIVO uteis e generalizaveis que possam beneficiar outros cultivadores.

Para cada insight identificado, retorne um objeto JSON com:
- "content": texto do conhecimento em portugues, anonimizado, sem mencionar o usuario, escrito como fato/tecnica/recomendacao generica (max 400 chars)
- "source_type": um de: concept | technique | problem | strain | insumo | user_experience
- "phase_relevance": lista de fases relevantes de: ["all"] ou subconjunto de ["germination","seedling","veg","flower","harvest"]
- "tags": lista de palavras-chave curtas relevantes (max 6)
- "confidence": numero 0.0 a 1.0 indicando qualidade/utilidade do insight
- "reasoning": breve justificativa em portugues de por que este trecho e valioso (max 150 chars)

Retorne APENAS um array JSON valido. Se nao houver insights uteis, retorne [].
Nao invente informacoes que nao estejam na conversa.
Foque em insights praticos, especificos e verificaveis.
Ignore perguntas triviais, saudacoes e respostas genericas sem valor educacional."""


def _build_extract_prompt(conversation: AIConversation) -> str:
    lines = ["Conversa a analisar:\n"]
    for msg in conversation.messages:
        role = "Cultivador" if msg.get("role") == "user" else "Bob (consultor)"
        content = msg.get("content", "")
        if isinstance(content, list):
            # multimodal — pega só o texto
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/ai/conversations", response_model=list[ConversationAdminItem])
async def admin_list_conversations(
    limit: int = 50,
    offset: int = 0,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Lista todas as conversas de clientes com o Bob, mais recentes primeiro."""
    result = await db.execute(
        select(AIConversation)
        .order_by(AIConversation.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    conversations = result.scalars().all()

    # Collect IDs with extracted chunks in one query
    conv_ids = [c.id for c in conversations]
    extracted_ids: set[uuid.UUID] = set()
    if conv_ids:
        chunk_result = await db.execute(
            select(KnowledgeChunk.source_conversation_id).where(
                KnowledgeChunk.source_conversation_id.in_(conv_ids),
                KnowledgeChunk.status.in_(["draft", "active"]),
            )
        )
        extracted_ids = {row[0] for row in chunk_result.fetchall()}

    # Fetch user emails
    user_ids = list({c.user_id for c in conversations})
    user_map: dict[uuid.UUID, str] = {}
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        for u in users_result.scalars().all():
            user_map[u.id] = u.email

    return [
        ConversationAdminItem(
            id=c.id,
            user_id=c.user_id,
            user_email=user_map.get(c.user_id),
            title=c.title,
            plant_id=c.plant_id,
            grow_id=c.grow_id,
            message_count=len(c.messages),
            created_at=c.created_at,
            updated_at=c.updated_at,
            extracted=c.id in extracted_ids,
        )
        for c in conversations
    ]


@router.get("/ai/conversations/{conversation_id}/chunks", response_model=list[DraftChunkOut])
async def admin_get_conversation_chunks(
    conversation_id: uuid.UUID,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Retorna todos os chunks (draft/active/rejected) extraídos de uma conversa."""
    result = await db.execute(
        select(KnowledgeChunk)
        .where(KnowledgeChunk.source_conversation_id == conversation_id)
        .order_by(KnowledgeChunk.created_at.desc())
    )
    return result.scalars().all()


@router.post("/ai/conversations/{conversation_id}/extract", response_model=ExtractResponse)
async def admin_extract_conversation(
    conversation_id: uuid.UUID,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Usa o LLM para extrair insights da conversa e criar KnowledgeChunks com status=draft."""
    conv_result = await db.execute(
        select(AIConversation).where(AIConversation.id == conversation_id)
    )
    conversation = conv_result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversa nao encontrada")

    if not conversation.messages:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Conversa sem mensagens para extrair",
        )

    # Load LLM
    from app.services.rag import get_ai_context
    from langchain_core.messages import HumanMessage, SystemMessage

    _, llm, _ = await get_ai_context(db)

    user_prompt = _build_extract_prompt(conversation)
    lc_messages = [
        SystemMessage(content=_EXTRACT_SYSTEM),
        HumanMessage(content=user_prompt),
    ]

    response = await llm.ainvoke(lc_messages)
    raw = response.content.strip()

    # Parse JSON — be forgiving of markdown code fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("`").strip()

    try:
        extracted: list[dict] = json.loads(raw)
        if not isinstance(extracted, list):
            extracted = []
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"LLM retornou JSON invalido: {raw[:200]}",
        )

    created_chunks: list[KnowledgeChunk] = []
    for item in extracted:
        if not item.get("content"):
            continue
        chunk = KnowledgeChunk(
            content=item["content"],
            source_file=f"conversation:{conversation_id}",
            source_type=item.get("source_type", "user_experience"),
            phase_relevance=item.get("phase_relevance"),
            tags=item.get("tags"),
            status="draft",
            confidence_score=float(item.get("confidence", 0.5)),
            source_conversation_id=conversation_id,
            extraction_reasoning=item.get("reasoning"),
            # embedding will be generated on approve
        )
        db.add(chunk)
        created_chunks.append(chunk)

    await db.commit()
    for chunk in created_chunks:
        await db.refresh(chunk)

    return ExtractResponse(
        conversation_id=conversation_id,
        chunks_created=len(created_chunks),
        chunks=[DraftChunkOut.model_validate(c) for c in created_chunks],
    )


@router.patch("/ai/knowledge-chunks/{chunk_id}", response_model=DraftChunkOut)
async def admin_update_chunk(
    chunk_id: uuid.UUID,
    body: PatchChunkBody,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Edita o conteudo/metadata de um chunk draft antes de aprovar."""
    result = await db.execute(select(KnowledgeChunk).where(KnowledgeChunk.id == chunk_id))
    chunk = result.scalar_one_or_none()
    if not chunk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chunk nao encontrado")

    if body.content is not None:
        chunk.content = body.content
    if body.source_type is not None:
        chunk.source_type = body.source_type
    if body.phase_relevance is not None:
        chunk.phase_relevance = body.phase_relevance
    if body.tags is not None:
        chunk.tags = body.tags

    await db.commit()
    await db.refresh(chunk)
    return chunk


@router.post("/ai/knowledge-chunks/{chunk_id}/approve", response_model=DraftChunkOut)
async def admin_approve_chunk(
    chunk_id: uuid.UUID,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Aprova um chunk draft: gera o embedding e define status=active para entrar no RAG."""
    result = await db.execute(select(KnowledgeChunk).where(KnowledgeChunk.id == chunk_id))
    chunk = result.scalar_one_or_none()
    if not chunk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chunk nao encontrado")

    if chunk.status == "active":
        return chunk  # idempotente

    # Generate embedding
    from app.services import ai_provider
    from app.services.rag import get_ai_context

    config, _, embeddings = await get_ai_context(db)
    vector = await ai_provider.embed_query(
        embeddings,
        chunk.content,
        provider=config.embedding_provider,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )
    chunk.embedding = vector
    chunk.status = "active"

    await db.commit()
    await db.refresh(chunk)
    return chunk


@router.post("/ai/knowledge-chunks/{chunk_id}/reject", response_model=DraftChunkOut)
async def admin_reject_chunk(
    chunk_id: uuid.UUID,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Rejeita um chunk draft — nao entra no RAG."""
    result = await db.execute(select(KnowledgeChunk).where(KnowledgeChunk.id == chunk_id))
    chunk = result.scalar_one_or_none()
    if not chunk:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chunk nao encontrado")

    chunk.status = "rejected"
    await db.commit()
    await db.refresh(chunk)
    return chunk
