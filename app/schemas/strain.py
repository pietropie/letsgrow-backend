import uuid

from pydantic import BaseModel, field_validator


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
    # Caminho do arquivo interno da wiki: não sai para o app (proteção da API). O campo
    # continua na resposta, sempre nulo, para os clientes que já o tipam como opcional.
    source_file: str | None = None

    model_config = {"from_attributes": True}

    @field_validator("source_file", mode="before")
    @classmethod
    def _hide_source_file(cls, value):
        return None
