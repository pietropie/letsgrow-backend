"""
Endpoints administrativos — hoje, só a configuração de IA (provider/modelo).

Protegidos por um token compartilhado simples (header X-Admin-Token), não
pelo JWT de usuário comum: ainda não existe um conceito de "admin" no User,
e criar um exigiria migration + fluxo de promoção de usuário. Um token forte
guardado só no .env/Coolify resolve a necessidade real ("eu, Pietro, preciso
trocar isso rápido em uma emergência") sem esse trabalho extra. Ver
app/config.py:ADMIN_TOKEN para como gerar e configurar.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.ai_config import AIConfig
from app.services import ai_provider
from app.services.rag import invalidate_cache

router = APIRouter()


async def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    if not settings.ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Painel admin desativado — configure ADMIN_TOKEN no ambiente do servidor.",
        )
    if not x_admin_token or x_admin_token != settings.ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token de admin inválido")


class AIConfigOut(BaseModel):
    id: uuid.UUID
    provider: str
    chat_model: str
    temperature: float
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    updated_by: str | None
    updated_at: datetime
    reindex_required: bool = False

    model_config = {"from_attributes": True}


class AIConfigIn(BaseModel):
    provider: str = Field(description="Provedor de chat: gemini | anthropic | openai")
    chat_model: str
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    embedding_provider: str = Field(description="Provedor de embedding: gemini | openai")
    embedding_model: str
    embedding_dimensions: int = Field(default=768, gt=0)
    updated_by: str | None = Field(default=None, description="Quem está alterando (ex.: seu e-mail) — fica registrado para auditoria")


class AIProviderOptions(BaseModel):
    chat_providers: list[str]
    embedding_providers: list[str]


async def _get_or_create_config(db: AsyncSession) -> AIConfig:
    result = await db.execute(select(AIConfig).order_by(AIConfig.created_at.asc()).limit(1))
    config = result.scalar_one_or_none()
    if config is None:
        # Rede de segurança — a migration a2b3c4d5e6f7 já cria a linha seed,
        # mas cobrimos o caso de banco recriado fora do fluxo normal.
        config = AIConfig(
            provider="gemini",
            chat_model=settings.GEMINI_MODEL,
            temperature=0.3,
            embedding_provider="gemini",
            embedding_model=settings.EMBEDDING_MODEL,
            embedding_dimensions=settings.EMBEDDING_DIMENSIONS,
        )
        db.add(config)
        await db.commit()
        await db.refresh(config)
    return config


@router.get("/ai-config/options", response_model=AIProviderOptions)
async def get_ai_provider_options(_: None = Depends(require_admin_token)):
    return AIProviderOptions(
        chat_providers=list(ai_provider.CHAT_PROVIDERS),
        embedding_providers=list(ai_provider.EMBEDDING_PROVIDERS),
    )


@router.get("/ai-config", response_model=AIConfigOut)
async def get_ai_config(
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    config = await _get_or_create_config(db)
    return config


@router.put("/ai-config", response_model=AIConfigOut)
async def update_ai_config(
    body: AIConfigIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    provider = body.provider.strip().lower()
    embedding_provider = body.embedding_provider.strip().lower()

    if provider not in ai_provider.CHAT_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"provider inválido — use um de: {', '.join(ai_provider.CHAT_PROVIDERS)}",
        )
    if embedding_provider not in ai_provider.EMBEDDING_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"embedding_provider inválido — use um de: {', '.join(ai_provider.EMBEDDING_PROVIDERS)}",
        )

    config = await _get_or_create_config(db)

    embedding_changed = (
        config.embedding_provider != embedding_provider
        or config.embedding_model != body.embedding_model.strip()
        or config.embedding_dimensions != body.embedding_dimensions
    )

    config.provider = provider
    config.chat_model = body.chat_model.strip()
    config.temperature = body.temperature
    config.embedding_provider = embedding_provider
    config.embedding_model = body.embedding_model.strip()
    config.embedding_dimensions = body.embedding_dimensions
    config.updated_by = body.updated_by

    await db.commit()
    await db.refresh(config)

    # Faz a mudança valer já na próxima requisição, em vez de esperar o TTL
    # de ~60s do cache em app/services/rag.py
    invalidate_cache()

    response = AIConfigOut.model_validate(config)
    # Trocar provider/modelo/dimensões de EMBEDDING não atualiza vetores já
    # gravados — a busca por similaridade ficaria comparando vetores de
    # espaços diferentes. O painel mostra esse aviso e o comando para
    # reindexar (scripts/index_brain.py --wiki-path ... --clear).
    response.reindex_required = embedding_changed
    return response
