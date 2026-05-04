"""
Indexer — reads Brain/wiki/**/*.md and upserts chunks into knowledge_chunks.

Run once (or after wiki updates):
    python scripts/index_brain.py
"""
import re
import uuid
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeChunk

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
    return re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL | re.MULTILINE)


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


async def index_wiki(
    db: AsyncSession,
    wiki_path: Path,
    embeddings,
    clear_existing: bool = False,
) -> int:
    if clear_existing:
        await db.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.source_type != "user_experience")
        )
        await db.commit()

    md_files = list(wiki_path.rglob("*.md"))
    total = 0

    for md_file in md_files:
        rel = md_file.relative_to(wiki_path).as_posix()

        # Skip templates and index files
        if any(skip in rel for skip in ["templates/", "index.md", "log.md", "lint/"]):
            continue

        content = md_file.read_text(encoding="utf-8")
        tags = _extract_frontmatter_tags(content)
        body = _strip_frontmatter(content)
        source_type = _infer_source_type(rel)
        phase_relevance = _infer_phase_relevance(tags, body)

        sections = _split_by_sections(body)
        if not sections:
            continue

        vectors = await embeddings.aembed_documents(sections)

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
