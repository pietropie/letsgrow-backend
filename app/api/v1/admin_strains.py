"""
Endpoints administrativos — CRUD de strains.

O catálogo de strains hoje é alimentado por scripts/index_brain.py a partir de
Brain/wiki/strains/*.md (upsert por slug). Estes endpoints permitem ajustes
manuais rápidos pelo painel — correções pontuais sem precisar editar o
markdown e reindexar. Edições feitas aqui podem ser sobrescritas na próxima
indexação se o .md correspondente também for alterado.

Protegidos pelo mesmo X-Admin-Token de app/api/v1/admin.py.
"""
import re
import unicodedata
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.models.strain import Strain

router = APIRouter()


def _slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug or "strain"


class StrainAdminItem(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    aliases: list[str] | None
    strain_type: str | None
    genetics: str | None
    breeder: str | None
    thc_pct: str | None
    cbd_pct: str | None
    dominant_terpene: str | None
    flowering_days: int | None
    height_cm: str | None
    summary: str | None
    source_file: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StrainListResponse(BaseModel):
    items: list[StrainAdminItem]
    total: int
    limit: int
    offset: int


class StrainIn(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    slug: str | None = Field(default=None, description="Se omitido, é gerado a partir do nome")
    aliases: list[str] | None = None
    strain_type: str | None = Field(default=None, description="photo | auto")
    genetics: str | None = Field(default=None, description="indica | sativa | hybrid")
    breeder: str | None = None
    thc_pct: str | None = None
    cbd_pct: str | None = None
    dominant_terpene: str | None = None
    flowering_days: int | None = None
    height_cm: str | None = None
    summary: str | None = None


@router.get("/strains", response_model=StrainListResponse)
async def list_strains(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
    search: str | None = Query(default=None, description="Filtra por nome ou slug"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(Strain)
    count_stmt = select(func.count()).select_from(Strain)

    if search:
        needle = f"%{search.strip().lower()}%"
        cond = or_(func.lower(Strain.name).like(needle), func.lower(Strain.slug).like(needle))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = (await db.execute(count_stmt)).scalar_one()

    stmt = stmt.order_by(Strain.name.asc()).limit(limit).offset(offset)
    strains = (await db.execute(stmt)).scalars().all()

    return StrainListResponse(
        items=[StrainAdminItem.model_validate(s) for s in strains],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/strains/{strain_id}", response_model=StrainAdminItem)
async def get_strain(
    strain_id: uuid.UUID,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    strain = await db.get(Strain, strain_id)
    if strain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strain não encontrada")
    return strain


async def _ensure_unique_slug(db: AsyncSession, slug: str, *, exclude_id: uuid.UUID | None = None) -> None:
    stmt = select(Strain.id).where(Strain.slug == slug)
    if exclude_id is not None:
        stmt = stmt.where(Strain.id != exclude_id)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Já existe uma strain com o slug '{slug}' — escolha outro nome/slug.",
        )


@router.post("/strains", response_model=StrainAdminItem, status_code=status.HTTP_201_CREATED)
async def create_strain(
    body: StrainIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    slug = (body.slug or _slugify(body.name)).strip().lower()
    await _ensure_unique_slug(db, slug)

    strain = Strain(
        name=body.name.strip(),
        slug=slug,
        aliases=body.aliases,
        strain_type=body.strain_type,
        genetics=body.genetics,
        breeder=body.breeder,
        thc_pct=body.thc_pct,
        cbd_pct=body.cbd_pct,
        dominant_terpene=body.dominant_terpene,
        flowering_days=body.flowering_days,
        height_cm=body.height_cm,
        summary=body.summary,
        source_file=f"manual/{slug}.md",
    )
    db.add(strain)
    await db.commit()
    await db.refresh(strain)
    return strain


@router.put("/strains/{strain_id}", response_model=StrainAdminItem)
async def update_strain(
    strain_id: uuid.UUID,
    body: StrainIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    strain = await db.get(Strain, strain_id)
    if strain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strain não encontrada")

    slug = (body.slug or _slugify(body.name)).strip().lower()
    await _ensure_unique_slug(db, slug, exclude_id=strain_id)

    strain.name = body.name.strip()
    strain.slug = slug
    strain.aliases = body.aliases
    strain.strain_type = body.strain_type
    strain.genetics = body.genetics
    strain.breeder = body.breeder
    strain.thc_pct = body.thc_pct
    strain.cbd_pct = body.cbd_pct
    strain.dominant_terpene = body.dominant_terpene
    strain.flowering_days = body.flowering_days
    strain.height_cm = body.height_cm
    strain.summary = body.summary

    await db.commit()
    await db.refresh(strain)
    return strain


@router.delete("/strains/{strain_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_strain(
    strain_id: uuid.UUID,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    strain = await db.get(Strain, strain_id)
    if strain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strain não encontrada")
    await db.delete(strain)
    await db.commit()
    return None
