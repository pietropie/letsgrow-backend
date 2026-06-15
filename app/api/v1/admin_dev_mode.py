"""
Endpoint admin para ativar/desativar Dev Mode por usuário.

PATCH /admin/customers/{user_id}/dev-mode
  Body: {"is_dev_mode": true | false}
  Auth: X-Admin-Token (mesmo padrão de app/api/v1/admin.py)

Quando is_dev_mode=True, o app mobile pode exibir features beta
ainda não lançadas para o usuário em questão.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.database import get_db
from app.models.user import User

router = APIRouter()


class DevModeIn(BaseModel):
    is_dev_mode: bool


class DevModeOut(BaseModel):
    user_id: uuid.UUID
    email: str
    username: str
    is_dev_mode: bool

    model_config = {"from_attributes": True}


@router.patch(
    "/customers/{user_id}/dev-mode",
    response_model=DevModeOut,
    summary="Ativa ou desativa Dev Mode para um usuário",
)
async def set_dev_mode(
    user_id: uuid.UUID,
    body: DevModeIn,
    _: None = Depends(require_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Alterna o campo is_dev_mode do usuário.

    Quando ativo, o app mobile exibe features beta controladas
    pelas feature flags globais marcadas como dev-only.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Usuário {user_id} não encontrado",
        )

    user.is_dev_mode = body.is_dev_mode
    await db.commit()
    await db.refresh(user)

    return DevModeOut(
        user_id=user.id,
        email=user.email,
        username=user.username,
        is_dev_mode=user.is_dev_mode,
    )
