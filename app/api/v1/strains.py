from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.strain import Strain
from app.models.user import User
from app.schemas.strain import StrainResponse, StrainSearchResult
from app.services.auth import get_current_user

router = APIRouter()


def _aliases_match(aliases: list | None, needle: str, *, contains: bool = True) -> bool:
    """Checa se algum alias bate (case-insensitive) com `needle`.

    `contains=True`  → substring match (usado em /search)
    `contains=False` → match exato (usado em /match, antes do fallback parcial)
    """
    if not aliases:
        return False
    needle = needle.lower().strip()
    for alias in aliases:
        if not isinstance(alias, str):
            continue
        a = alias.lower().strip()
        if contains:
            if needle in a:
                return True
        else:
            if a == needle:
                return True
    return False


# ---------------------------------------------------------------------------
# GET /strains/search — autocomplete
# ---------------------------------------------------------------------------

@router.get("/search", response_model=list[StrainSearchResult])
async def search_strains(
    q: str = Query(..., min_length=1, description="Termo de busca (nome ou alias)"),
    limit: int = Query(default=10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Busca leve para autocomplete: nome (ilike) OU aliases que contenham `q`.

    A tabela de strains é relativamente pequena (algumas centenas de registros),
    então fazemos um filtro amplo por nome no banco e complementamos com checagem
    de aliases em Python — evita depender de operadores específicos de JSONB.
    """
    term = q.strip()
    if not term:
        return []

    like_term = f"%{term}%"
    stmt = select(Strain).where(
        or_(
            Strain.name.ilike(like_term),
            Strain.aliases.is_not(None),
        )
    )
    result = await db.execute(stmt)
    candidates = result.scalars().all()

    matches: list[Strain] = []
    term_lower = term.lower()
    for strain in candidates:
        if term_lower in strain.name.lower() or _aliases_match(strain.aliases, term, contains=True):
            matches.append(strain)

    # Prioriza correspondências que começam com o termo, depois ordena por nome
    def _sort_key(s: Strain):
        starts_with = not s.name.lower().startswith(term_lower)
        return (starts_with, s.name.lower())

    matches.sort(key=_sort_key)
    return matches[:limit]


# ---------------------------------------------------------------------------
# GET /strains/match — melhor correspondência para um nome livre
# ---------------------------------------------------------------------------

@router.get("/match", response_model=StrainResponse)
async def match_strain(
    name: str = Query(..., min_length=1, description="Nome livre digitado pelo usuário (ex: plant.strain_name)"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Encontra o melhor match de Strain para um nome livre.

    Estratégia (case-insensitive):
    1. Match exato contra `name`
    2. Match exato contra algum item de `aliases`
    3. Fallback: match parcial (substring, em qualquer direção) contra `name` ou `aliases`

    Retorna 404 se nada for encontrado.
    """
    term = name.strip()
    if not term:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strain não encontrada")

    term_lower = term.lower()

    result = await db.execute(select(Strain))
    all_strains = result.scalars().all()

    # 1. Exact match on name
    for strain in all_strains:
        if strain.name.lower().strip() == term_lower:
            return strain

    # 2. Exact match on aliases
    for strain in all_strains:
        if _aliases_match(strain.aliases, term, contains=False):
            return strain

    # 3. Partial match (substring either direction) on name or aliases
    for strain in all_strains:
        sname = strain.name.lower().strip()
        if term_lower in sname or sname in term_lower:
            return strain
        if _aliases_match(strain.aliases, term, contains=True):
            return strain

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strain não encontrada")
