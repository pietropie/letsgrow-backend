import uuid

from pydantic import BaseModel


class StrainSearchResult(BaseModel):
    """Item leve retornado por GET /strains/search — usado para autocomplete."""

    id: uuid.UUID
    name: str
    strain_type: str | None
    genetics: str | None

    model_config = {"from_attributes": True}


class StrainResponse(BaseModel):
    """Registro completo de Strain — usado pelo card de informações (GET /strains/match)."""

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

    model_config = {"from_attributes": True}
