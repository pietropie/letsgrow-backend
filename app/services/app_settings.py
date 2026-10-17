"""Leitura das configuracoes de negocio editaveis no admin (linha unica)."""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_settings import DEFAULT_APP_SETTINGS, AppSettings


async def get_app_settings(db: AsyncSession) -> AppSettings:
    """Retorna a linha de configuracao; cria com os padroes neutros se faltar.

    A migration x0y1z2a3b4c5 ja cria a linha. Isto e rede de seguranca para
    banco recriado fora do fluxo normal (mesmo padrao de AIConfig).
    """
    row = (
        await db.execute(select(AppSettings).order_by(AppSettings.created_at.asc()).limit(1))
    ).scalar_one_or_none()
    if row is None:
        row = AppSettings(id=uuid.uuid4(), **DEFAULT_APP_SETTINGS)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row
