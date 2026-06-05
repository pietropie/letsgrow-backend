from functools import lru_cache

from minio import Minio
from minio.error import S3Error

from app.config import get_settings

BUCKET_EVENTS = "events"
BUCKET_AVATARS = "avatars"
_PRESIGN_EXPIRY_SECONDS = 3600  # 1h para upload, 24h para download


@lru_cache(maxsize=1)
def get_minio_client() -> Minio:
    settings = get_settings()
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )


def ensure_buckets() -> None:
    """Cria os buckets necessários se não existirem. Chamado no lifespan."""
    client = get_minio_client()
    for bucket in (BUCKET_EVENTS, BUCKET_AVATARS):
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)


def presign_upload(bucket: str, object_key: str, content_type: str = "image/jpeg") -> str:
    """Retorna URL pré-assinada para PUT direto do mobile → MinIO."""
    from datetime import timedelta

    client = get_minio_client()
    return client.presigned_put_object(
        bucket,
        object_key,
        expires=timedelta(seconds=_PRESIGN_EXPIRY_SECONDS),
    )


def presign_download(bucket: str, object_key: str) -> str:
    """Retorna URL pré-assinada para GET (visualização de foto)."""
    from datetime import timedelta

    client = get_minio_client()
    return client.presigned_get_object(
        bucket,
        object_key,
        expires=timedelta(seconds=_PRESIGN_EXPIRY_SECONDS * 24),
    )


def delete_object(bucket: str, object_key: str) -> None:
    try:
        get_minio_client().remove_object(bucket, object_key)
    except S3Error:
        pass
