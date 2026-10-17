"""
Catálogo de strains a partir de Brain/wiki/strains/*.md.

Faz parse do frontmatter YAML (formato simples chave: valor / listas [a, b, c])
e da seção "## Resumo" de cada arquivo, e faz UPSERT idempotente por `slug`
(slug = nome do arquivo .md sem extensão).

Usado por scripts/index_strains.py (wiki no disco) e por
POST /admin/strains/reindex (produção, onde a wiki não está na imagem).

Não usa PyYAML — frontmatter é parseado com regex simples, suficiente para o
formato usado na wiki.
"""
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strain import Strain

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SUMMARY_RE = re.compile(r"##\s*Resumo\s*\n+(.*?)(?:\n##\s|\Z)", re.DOTALL)

_SUMMARY_MAX_CHARS = 600

# Tags / seed_type values that indicate an autoflowering strain
_AUTO_HINTS = ("auto", "automática", "automatica", "autoflor")

_DOMINANCIA_MAP = {
    "indica": "indica",
    "sativa": "sativa",
    "hibrida": "hybrid",
    "híbrida": "hybrid",
    "hybrid": "hybrid",
}


def _parse_frontmatter(text: str) -> dict:
    """Parse simples de frontmatter YAML: suporta `chave: valor` e `chave: [a, b, c]`.

    Não é um parser YAML completo — cobre o formato usado nos arquivos da wiki
    (chaves simples, listas inline, valores com ou sem aspas).
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}

    fm: dict = {}
    raw = match.group(1)
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # Skip nested/indented lines (e.g. "sources:\n  - ...")
        if line.startswith((" ", "\t", "-")):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if not value:
            # Could be a multi-line list (e.g. `sources:`) — not needed for our fields
            continue

        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            items = [v.strip().strip('"').strip("'") for v in inner.split(",")]
            fm[key] = [v for v in items if v]
        else:
            fm[key] = value.strip('"').strip("'")

    return fm


def _extract_summary(body: str) -> str | None:
    match = _SUMMARY_RE.search(body)
    if not match:
        return None
    summary = match.group(1).strip()
    summary = re.sub(r"\s+", " ", summary)
    if len(summary) > _SUMMARY_MAX_CHARS:
        summary = summary[:_SUMMARY_MAX_CHARS].rsplit(" ", 1)[0].rstrip(",.;: ") + "…"
    return summary or None


def _infer_strain_type(fm: dict) -> str:
    haystack = " ".join(
        [
            str(fm.get("seed_type", "")),
            " ".join(fm.get("tags", []) or []),
            " ".join(fm.get("aliases", []) or []),
            str(fm.get("title", "")),
        ]
    ).lower()
    return "auto" if any(hint in haystack for hint in _AUTO_HINTS) else "photo"


def _infer_genetics(fm: dict) -> str | None:
    dominancia = str(fm.get("dominancia", "")).strip().lower()
    return _DOMINANCIA_MAP.get(dominancia)


def _parse_int(value) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _name_from_frontmatter(fm: dict, slug: str) -> str:
    title = fm.get("title")
    if title:
        # Title is often "Gelato #41 — Cookie Fam Genetics" — keep only the strain name part
        name = title.split("—")[0].split("-")[0].strip()
        if name:
            return name
    return slug.replace("-", " ").title()


def build_strain_kwargs(slug: str, text: str, source_file: str) -> dict | None:
    """Campos do Strain a partir de um .md da wiki, ou None se não for strain."""
    fm = _parse_frontmatter(text)
    if not fm:
        return None
    if fm.get("type") not in (None, "strain"):
        return None

    return dict(
        name=_name_from_frontmatter(fm, slug),
        slug=slug,
        aliases=fm.get("aliases") or None,
        strain_type=_infer_strain_type(fm),
        genetics=_infer_genetics(fm),
        breeder=fm.get("breeder"),
        thc_pct=fm.get("thc_pct"),
        cbd_pct=fm.get("cbd_pct"),
        dominant_terpene=fm.get("terpeno_dominante"),
        flowering_days=_parse_int(fm.get("floração_dias")),
        height_cm=fm.get("altura_cm"),
        summary=_extract_summary(text),
        source_file=source_file.replace("\\", "/"),
    )


async def upsert_strains(db: AsyncSession, documents: list[tuple[str, str, str]]) -> tuple[int, int]:
    """Upsert por slug de `documents` = [(slug, markdown, source_file)].
    Retorna (criadas, atualizadas). Arquivos que não são strain são ignorados."""
    created = 0
    updated = 0

    for slug, text, source_file in documents:
        kwargs = build_strain_kwargs(slug, text, source_file)
        if kwargs is None:
            continue

        result = await db.execute(select(Strain).where(Strain.slug == slug))
        existing = result.scalar_one_or_none()

        if existing:
            for field, value in kwargs.items():
                setattr(existing, field, value)
            updated += 1
        else:
            db.add(Strain(**kwargs))
            created += 1

    await db.commit()
    return created, updated
