"""
Endpoints administrativos — (re)indexação da Brain wiki em produção.

A pasta Brain/wiki não vai na imagem da API e o Postgres de produção só é
acessível pela rede interna do compose. Por isso o conteúdo dos .md é enviado
pelo próprio admin (scripts/push_brain.py, em lotes) e indexado aqui, usando o
mesmo indexer e o mesmo parser de strains dos scripts locais.

- POST /admin/knowledge/reindex — gera embeddings e substitui os chunks de
  cada arquivo enviado (idempotente por caminho).
- POST /admin/knowledge/prune — apaga chunks de arquivos que saíram da wiki.
- POST /admin/strains/reindex — upsert do catálogo de strains por slug.
- GET  /admin/knowledge/policy-audit — chunks e strains que ainda ferem a política da
  Play (lojas, extração, consumo; app/rag/content_policy.py). Vazio = base limpa.
- POST /admin/knowledge/search — o que a busca do Bobby traria para uma pergunta, com
  o texto guardado no banco (antes do filtro da busca), para conferir a base.

Chunks user_experience e os extraídos de conversas nunca são apagados.
Protegidos pelo mesmo X-Admin-Token de app/api/v1/admin.py.
"""
import logging
import posixpath

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.models.knowledge import KnowledgeChunk
from app.models.strain import Strain
from app.rag import content_policy
from app.rag.indexer import index_documents, prune_wiki_chunks, should_index
from app.rag.retriever import retrieve_chunks
from app.services.rag import get_ai_context
from app.services.strain_catalog import upsert_strains

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_FILE_CHARS = 200_000


class WikiFile(BaseModel):
    path: str = Field(..., min_length=4, max_length=255)
    # caminho relativo a Brain/wiki, sempre com "/" (ex.: "tecnicas/lst.md")
    content: str = Field(..., max_length=_MAX_FILE_CHARS)

    @field_validator("path")
    @classmethod
    def _safe_relative_md(cls, v: str) -> str:
        if (
            not v.endswith(".md")
            or v.startswith("/")
            or "\\" in v
            or ":" in v
            or posixpath.normpath(v) != v
            or ".." in v.split("/")
        ):
            raise ValueError("path deve ser relativo a Brain/wiki, com '/', terminando em .md")
        return v


class KnowledgeReindexRequest(BaseModel):
    files: list[WikiFile] = Field(..., min_length=1, max_length=50)


class KnowledgeReindexResponse(BaseModel):
    files_received: int
    files_indexed: int
    chunks_indexed: int
    skipped: list[str]


class KnowledgePruneRequest(BaseModel):
    keep_paths: list[str] = Field(..., min_length=1, max_length=5000)


class KnowledgePruneResponse(BaseModel):
    chunks_deleted: int


class StrainsReindexRequest(BaseModel):
    files: list[WikiFile] = Field(..., min_length=1, max_length=500)


class StrainsReindexResponse(BaseModel):
    created: int
    updated: int
    ignored: list[str]


@router.post("/knowledge/reindex", response_model=KnowledgeReindexResponse)
async def reindex_knowledge(
    body: KnowledgeReindexRequest,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Indexa um lote de .md da wiki no pgvector, com o embedding ativo em /admin/ia."""
    documents = [(f.path, f.content) for f in body.files if should_index(f.path)]
    skipped = [f.path for f in body.files if not should_index(f.path)]

    config, _llm, embeddings = await get_ai_context(db)
    try:
        chunks = await index_documents(db, documents, embeddings, ai_config=config)
    except Exception as exc:  # erro do provedor de embedding (cota, chave, rede)
        await db.rollback()
        logger.exception("Falha ao indexar lote da wiki")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Falha ao gerar embeddings ({config.embedding_provider}): {type(exc).__name__}: {str(exc)[:300]}",
        ) from exc

    logger.info("Wiki reindex: %d arquivos, %d chunks", len(documents), chunks)
    return KnowledgeReindexResponse(
        files_received=len(body.files),
        files_indexed=len(documents),
        chunks_indexed=chunks,
        skipped=skipped,
    )


@router.post("/knowledge/prune", response_model=KnowledgePruneResponse)
async def prune_knowledge(
    body: KnowledgePruneRequest,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Apaga chunks da wiki cujo arquivo não está em `keep_paths`."""
    deleted = await prune_wiki_chunks(db, set(body.keep_paths))
    logger.info("Wiki prune: %d chunks apagados", deleted)
    return KnowledgePruneResponse(chunks_deleted=deleted)


@router.post("/strains/reindex", response_model=StrainsReindexResponse)
async def reindex_strains(
    body: StrainsReindexRequest,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Upsert do catálogo a partir de strains/*.md (slug = nome do arquivo)."""
    documents = [
        (posixpath.splitext(posixpath.basename(f.path))[0], f.content, f.path)
        for f in body.files
        if f.path.startswith("strains/")
    ]
    ignored = [f.path for f in body.files if not f.path.startswith("strains/")]
    created, updated = await upsert_strains(db, documents)
    return StrainsReindexResponse(created=created, updated=updated, ignored=ignored)


class PolicyFinding(BaseModel):
    kind: str            # chunk | strain
    id: str
    source_file: str | None
    reason: str          # blocked_path | blocked_text
    excerpt: str


class PolicyAuditResponse(BaseModel):
    chunks_checked: int
    strains_checked: int
    findings: list[PolicyFinding]


def _excerpt(text: str) -> str:
    match = content_policy._BLOCKED_TEXT.search(text)
    if not match:
        return text[:160]
    start = max(match.start() - 60, 0)
    return text[start:match.end() + 60]


@router.get("/knowledge/policy-audit", response_model=PolicyAuditResponse)
async def knowledge_policy_audit(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Varre todos os chunks ativos e o catálogo de strains atrás de conteúdo fora da política."""
    findings: list[PolicyFinding] = []
    chunks = (await db.execute(select(KnowledgeChunk.id, KnowledgeChunk.source_file, KnowledgeChunk.content))).all()
    for chunk_id, source_file, content in chunks:
        if content_policy.is_blocked_path(source_file or ""):
            findings.append(PolicyFinding(kind="chunk", id=str(chunk_id), source_file=source_file,
                                          reason="blocked_path", excerpt=(content or "")[:160]))
        elif content and content_policy.is_blocked_text(content):
            findings.append(PolicyFinding(kind="chunk", id=str(chunk_id), source_file=source_file,
                                          reason="blocked_text", excerpt=_excerpt(content)))
    strains = (await db.execute(select(Strain.id, Strain.source_file, Strain.summary, Strain.breeder))).all()
    for strain_id, source_file, summary, breeder in strains:
        text = " ".join(t for t in (summary, breeder) if t)
        if content_policy.is_blocked_text(text):
            findings.append(PolicyFinding(kind="strain", id=str(strain_id), source_file=source_file,
                                          reason="blocked_text", excerpt=_excerpt(text)))
    return PolicyAuditResponse(chunks_checked=len(chunks), strains_checked=len(strains), findings=findings)


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=300)
    top_k: int = Field(default=6, ge=1, le=10)


class KnowledgeSearchHit(BaseModel):
    source_file: str | None
    content: str
    policy_ok: bool      # o texto guardado já está dentro da política


@router.post("/knowledge/search", response_model=list[KnowledgeSearchHit])
async def knowledge_search(
    body: KnowledgeSearchRequest,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Mesma busca vetorial do chat (embedding ativo em /admin/ia), sem filtro de fase."""
    chunks = await retrieve_chunks(db, body.query, None, top_k=body.top_k)
    return [
        KnowledgeSearchHit(
            source_file=c.source_file, content=c.content or "",
            policy_ok=not content_policy.is_blocked_path(c.source_file or "")
            and not content_policy.is_blocked_text(c.content or ""),
        )
        for c in chunks
    ]
