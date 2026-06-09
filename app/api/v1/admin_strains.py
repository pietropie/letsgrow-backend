"""
Endpoints administrativos — CRUD de strains.

O catálogo de strains hoje é alimentado por scripts/index_brain.py a partir de
Brain/wiki/strains/*.md (upsert por slug). Estes endpoints permitem ajustes
manuais rápidos pelo painel — correções pontuais sem precisar editar o
markdown e reindexar. Edições feitas aqui podem ser sobrescritas na próxima
indexação se o .md correspondente também for alterado.

Protegidos pelo mesmo X-Admin-Token de app/api/v1/admin.py.
"""
import io
import re
import unicodedata
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.models.strain import Strain
from app.services.storage import BUCKET_STRAINS, delete_object, get_minio_client, strain_image_url_for_response

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
    image_url: str | None
    source_file: str
    created_at: datetime
    updated_at: datetime

    @field_validator("image_url", mode="before")
    @classmethod
    def _resolve_image_url(cls, v: str | None) -> str | None:
        """Converte object key ou URL direta armazenada no banco → URL presignada."""
        return strain_image_url_for_response(v)

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
    # Remove imagem do MinIO se existir
    if strain.image_url:
        _delete_strain_image_from_storage(strain_id)
    await db.delete(strain)
    await db.commit()
    return None


# ---------------------------------------------------------------------------
# POST /admin/strains/{strain_id}/image — upload de foto da strain
# ---------------------------------------------------------------------------

def _strain_object_key(strain_id: uuid.UUID) -> str:
    return f"{strain_id}/cover.jpg"


def _delete_strain_image_from_storage(strain_id: uuid.UUID) -> None:
    delete_object(BUCKET_STRAINS, _strain_object_key(strain_id))


@router.post("/strains/{strain_id}/image", response_model=StrainAdminItem)
async def upload_strain_image(
    strain_id: uuid.UUID,
    file: UploadFile = File(..., description="Imagem JPG/PNG da strain (max 10MB)"),
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Faz upload da imagem de capa de uma strain diretamente pelo painel admin.
    O arquivo é enviado como multipart/form-data e salvo no MinIO.
    A URL pública é gravada em strain.image_url.
    """
    strain = await db.get(Strain, strain_id)
    if strain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strain não encontrada")

    # Valida content_type
    allowed = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
    ct = (file.content_type or "").lower()
    if ct not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Tipo de arquivo não suportado: {ct}. Use JPG, PNG ou WebP.",
        )

    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:  # 10MB
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Imagem muito grande — máximo 10MB.",
        )

    object_key = _strain_object_key(strain_id)
    try:
        client = get_minio_client()
        # Cria o bucket se não existir (race-condition no startup: MinIO pode
        # não estar pronto quando ensure_buckets() rodou no lifespan)
        if not client.bucket_exists(BUCKET_STRAINS):
            client.make_bucket(BUCKET_STRAINS)
        client.put_object(
            BUCKET_STRAINS,
            object_key,
            data=io.BytesIO(contents),
            length=len(contents),
            content_type=ct or "image/jpeg",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Falha ao salvar imagem no storage: {exc}",
        )

    # Armazena apenas o object key; a URL presignada é gerada on-the-fly
    # em StrainAdminItem._resolve_image_url (e PlantSummary em plants.py).
    strain.image_url = _strain_object_key(strain_id)
    await db.commit()
    await db.refresh(strain)
    return strain


@router.delete("/strains/{strain_id}/image", status_code=status.HTTP_204_NO_CONTENT)
async def remove_strain_image(
    strain_id: uuid.UUID,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Remove a imagem de capa de uma strain."""
    strain = await db.get(Strain, strain_id)
    if strain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strain não encontrada")

    _delete_strain_image_from_storage(strain_id)
    strain.image_url = None
    await db.commit()
    return None
