"""
Indexer — reads Brain/wiki/**/*.md and upserts chunks into knowledge_chunks.

Local (wiki no disco):   python scripts/index_brain.py
Produção (wiki fora da imagem): python scripts/push_brain.py — envia os .md
para POST /admin/knowledge/reindex. Ver analise-2026-10-16/REINDEXAR-RAG.md.
"""
import re
import uuid
from pathlib import Path

from sqlalchemy import delete, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_config import AIConfig
from app.models.knowledge import KnowledgeChunk
from app.rag.content_policy import is_blocked_path, is_blocked_tag, sanitize_markdown
from app.services import ai_provider

# Tags from frontmatter that map to phase relevance
_PHASE_TAG_MAP = {
    "veg": "veg",
    "vegetativo": "veg",
    "vegetação": "veg",
    "floração": "flower",
    "flower": "flower",
    "floracao": "flower",
    "germinação": "germination",
    "colheita": "harvest",
    "harvest": "harvest",
    "seedling": "seedling",
    "muda": "seedling",
}

# Source type inference from folder name
_FOLDER_TYPE_MAP = {
    "concepts": "concept",
    "tecnicas": "technique",
    "people/problemas": "problem",
    "strains": "strain",
    "insumos": "insumo",
    "sources": "source",
}


def _infer_source_type(rel_path: str) -> str:
    for folder, stype in _FOLDER_TYPE_MAP.items():
        if folder in rel_path:
            return stype
    return "concept"


def _extract_frontmatter_tags(content: str) -> list[str]:
    match = re.search(r"^---\s*(.*?)\s*---", content, re.DOTALL | re.MULTILINE)
    if not match:
        return []
    fm = match.group(1)
    tag_match = re.search(r"tags:\s*\[([^\]]*)\]", fm)
    if not tag_match:
        return []
    raw = tag_match.group(1)
    return [t.strip().strip('"').strip("'") for t in raw.split(",") if t.strip()]


def _strip_frontmatter(content: str) -> str:
    # Só o bloco do topo: os "---" do corpo (separadores, tabelas) não são frontmatter
    return re.sub(r"\A﻿?\s*---[ \t]*\n.*?\n---[ \t]*(\n\s*|\Z)", "", content, count=1, flags=re.DOTALL)


def _split_by_sections(content: str, min_chars: int = 200) -> list[str]:
    """Split on H2/H3 headings; merge short sections."""
    sections = re.split(r"(?m)^#{2,3}\s+", content)
    chunks = []
    buffer = ""
    for section in sections:
        section = section.strip()
        if not section:
            continue
        buffer = (buffer + "\n\n" + section).strip() if buffer else section
        if len(buffer) >= min_chars:
            chunks.append(buffer)
            buffer = ""
    if buffer:
        chunks.append(buffer)
    return chunks


def _infer_phase_relevance(tags: list[str], content: str) -> list[str]:
    phases = set()
    combined = " ".join(tags).lower() + " " + content[:500].lower()
    for keyword, phase in _PHASE_TAG_MAP.items():
        if keyword in combined:
            phases.add(phase)
    return list(phases) if phases else ["all"]


# Arquivos da wiki que não entram no RAG: modelos, índices, lint, a nota de
# boas-vindas do Obsidian e diários pessoais de cultivo (cultivations/ é o
# grow do dono — não deve aparecer nas respostas do Bobby para outros clientes).
# Lojas, extração e afins ficam fora pela política da Play (app/rag/content_policy.py).
_SKIP_PATTERNS = ("templates/", "index.md", "log.md", "lint/", "Brain/", "cultivations/")


def should_index(rel_path: str) -> bool:
    return (
        rel_path.endswith(".md")
        and not any(skip in rel_path for skip in _SKIP_PATTERNS)
        and not is_blocked_path(rel_path)
    )


def _wiki_chunks_filter():
    """Chunks que vieram da wiki — preserva user_experience e os extraídos de
    conversas (aprovados no painel), que não têm .md de origem."""
    return (
        KnowledgeChunk.source_type != "user_experience",
        KnowledgeChunk.source_conversation_id.is_(None),
    )


async def index_documents(
    db: AsyncSession,
    documents: list[tuple[str, str]],
    embeddings,
    ai_config: AIConfig | None = None,
) -> int:
    """
    Indexa `documents` = [(caminho relativo à wiki, conteúdo markdown)].

    Cada arquivo substitui os chunks que já existiam para o mesmo caminho, então
    rodar de novo não duplica nada. Usado por index_wiki (script local) e por
    POST /admin/knowledge/reindex (produção, onde a wiki não está na imagem).

    `embeddings` é o client (LangChain) já instanciado para o provedor de
    embedding ativo, e `ai_config` traz provider/modelo/dimensões — ambos
    obtidos via app.services.rag.get_ai_context, que lê a config editável
    no painel admin (/admin/ia). Se `ai_config` não for passado, caímos
    nos defaults do .env (compatibilidade com chamadas antigas/scripts).
    """
    if ai_config is None:
        from app.config import settings as _settings
        embed_provider, embed_model, embed_dims = "gemini", _settings.EMBEDDING_MODEL, _settings.EMBEDDING_DIMENSIONS
    else:
        embed_provider, embed_model, embed_dims = (
            ai_config.embedding_provider, ai_config.embedding_model, ai_config.embedding_dimensions
        )

    total = 0

    for rel, content in documents:
        if not should_index(rel):
            continue

        tags = [tag for tag in _extract_frontmatter_tags(content) if not is_blocked_tag(tag)]
        body = sanitize_markdown(_strip_frontmatter(content))
        source_type = _infer_source_type(rel)
        phase_relevance = _infer_phase_relevance(tags, body)

        # Só títulos sobraram (o filtro tirou o conteúdo): não vale um chunk
        has_content = re.sub(r"(?m)^#{1,6}\s.*$", "", body).strip()
        sections = _split_by_sections(body) if has_content else []
        if not sections:
            # Nada sobrou depois do filtro: os chunks antigos do arquivo também saem
            await db.execute(
                delete(KnowledgeChunk).where(KnowledgeChunk.source_file == rel, *_wiki_chunks_filter())
            )
            continue

        # Normaliza a chamada entre provedores (output_dimensionality no Gemini,
        # dimensions na OpenAI, nativo nos demais) — ver app/services/ai_provider.
        vectors = await ai_provider.embed_documents(
            embeddings, sections,
            provider=embed_provider, model=embed_model, dimensions=embed_dims,
        )

        await db.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.source_file == rel, *_wiki_chunks_filter())
        )
        for section, vector in zip(sections, vectors):
            chunk = KnowledgeChunk(
                id=uuid.uuid4(),
                content=section,
                embedding=vector,
                source_file=rel,
                source_type=source_type,
                phase_relevance=phase_relevance,
                tags=tags,
                chunk_metadata={"file": rel},
            )
            db.add(chunk)
            total += 1

    await db.commit()
    return total


async def prune_wiki_chunks(db: AsyncSession, keep_paths: set[str]) -> int:
    """Remove chunks de arquivos que saíram da wiki (ou que passaram a ser
    ignorados). Retorna quantos chunks foram apagados."""
    keep_paths = {p for p in keep_paths if should_index(p)}
    result = await db.execute(
        delete(KnowledgeChunk).where(
            KnowledgeChunk.source_file.not_in(keep_paths) if keep_paths else true(),
            *_wiki_chunks_filter(),
        )
    )
    await db.commit()
    return result.rowcount or 0


async def index_wiki(
    db: AsyncSession,
    wiki_path: Path,
    embeddings,
    clear_existing: bool = False,
    ai_config: AIConfig | None = None,
) -> int:
    """Lê wiki_path/**/*.md do disco e indexa. `clear_existing` remove também
    os chunks de arquivos que não existem mais na pasta."""
    documents = [
        (md_file.relative_to(wiki_path).as_posix(), md_file.read_text(encoding="utf-8"))
        for md_file in sorted(wiki_path.rglob("*.md"))
    ]
    documents = [(rel, content) for rel, content in documents if should_index(rel)]

    if clear_existing:
        await prune_wiki_chunks(db, {rel for rel, _ in documents})

    return await index_documents(db, documents, embeddings, ai_config=ai_config)
