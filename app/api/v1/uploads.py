import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.models.user import User
from app.services.auth import get_current_user
from app.services.storage import BUCKET_AVATARS, BUCKET_EVENTS, presign_download, presign_upload

router = APIRouter()


class PresignRequest(BaseModel):
    bucket: str = "events"  # "events" ou "avatars"
    content_type: str = "image/jpeg"


class PresignResponse(BaseModel):
    upload_url: str
    object_key: str
    bucket: str


@router.post("/presign", response_model=PresignResponse)
async def presign_upload_url(
    body: PresignRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Retorna URL pré-assinada para o mobile fazer PUT diretamente no MinIO.
    Fluxo: mobile pede URL → faz PUT na URL → salva o object_key no evento.
    """
    allowed = {BUCKET_EVENTS, BUCKET_AVATARS}
    bucket = body.bucket if body.bucket in allowed else BUCKET_EVENTS

    ext = "jpg" if "jpeg" in body.content_type else body.content_type.split("/")[-1]
    object_key = f"{current_user.id}/{uuid.uuid4()}.{ext}"

    url = presign_upload(bucket, object_key, body.content_type)
    return PresignResponse(upload_url=url, object_key=object_key, bucket=bucket)


class DownloadUrlResponse(BaseModel):
    url: str


@router.get("/url", response_model=DownloadUrlResponse)
async def get_download_url(
    bucket: str,
    key: str,
    current_user: User = Depends(get_current_user),
):
    """Gera URL temporária para visualizar uma foto salva no MinIO."""
    url = presign_download(bucket, key)
    return DownloadUrlResponse(url=url)
