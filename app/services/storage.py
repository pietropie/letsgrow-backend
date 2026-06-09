import json
from functools import lru_cache

from minio import Minio
from minio.error import S3Error

from app.config import get_settings

BUCKET_EVENTS = "events"
BUCKET_AVATARS = "avatars"
BUCKET_STRAINS = "strain-images"

# Política de leitura pública — permite acesso anônimo às imagens de strain
# via URL direta (sem presign). Aplicada ao BUCKET_STRAINS em ensure_buckets().
_STRAIN_PUBLIC_POLICY = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"AWS": "*"},
        "Action": ["s3:GetObject"],
        "Resource": [f"arn:aws:s3:::{BUCKET_STRAINS}/*"],
    }],
})
_PRESIGN_EXPIRY_SECONDS = 3600  # 1h para upload, 24h para download


@lru_cache(maxsize=1)
def get_minio_client() -> Minio:
    """Client para operações internas (criar bucket, apagar objeto etc.) —
    usa MINIO_ENDPOINT, o host interno do docker-compose ("minio:9000")."""
    settings = get_settings()
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )


def _minio_public_secure(settings) -> bool:
    """Resolve MINIO_PUBLIC_SECURE (string, pode vir vazia) -> bool.

    "" ou qualquer valor não reconhecido => herda MINIO_SECURE.
    "true"/"1"/"yes" => True; "false"/"0"/"no" => False (case-insensitive).
    """
    raw = (settings.MINIO_PUBLIC_SECURE or "").strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    return settings.MINIO_SECURE


@lru_cache(maxsize=1)
def get_minio_presign_client() -> Minio:
    """Client usado SÓ para gerar URLs pré-assinadas (upload/download).

    O SDK do MinIO monta a URL pré-assinada a partir do host com que o client
    foi instanciado — então, se usássemos o client interno, o app mobile
    receberia uma URL com host "minio" (não resolvível fora da rede do
    docker-compose) e quebraria com "Unable to resolve host 'minio'".

    Por isso usamos MINIO_PUBLIC_ENDPOINT (host alcançável pelo celular) aqui;
    se não estiver configurado, caímos para MINIO_ENDPOINT — o que só funciona
    se esse host também for alcançável de fora (ok em dev local, não em prod).
    """
    settings = get_settings()
    endpoint = settings.MINIO_PUBLIC_ENDPOINT or settings.MINIO_ENDPOINT
    return Minio(
        endpoint,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=_minio_public_secure(settings),
    )


def ensure_buckets() -> None:
    """Cria os buckets necessários se não existirem. Chamado no lifespan."""
    client = get_minio_client()
    for bucket in (BUCKET_EVENTS, BUCKET_AVATARS):
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    # strain-images precisa de leitura pública (URLs diretas, sem presign)
    if not client.bucket_exists(BUCKET_STRAINS):
        client.make_bucket(BUCKET_STRAINS)
    try:
        client.set_bucket_policy(BUCKET_STRAINS, _STRAIN_PUBLIC_POLICY)
    except Exception:
        pass  # não bloqueia o startup se MinIO ainda não aceitou a policy


def presign_upload(bucket: str, object_key: str, content_type: str = "image/jpeg") -> str:
    """Retorna URL pré-assinada para PUT direto do mobile → MinIO.

    Usa get_minio_presign_client() (host público) — não o client interno —
    para o app conseguir resolver e alcançar a URL pela internet."""
    from datetime import timedelta

    client = get_minio_presign_client()
    return client.presigned_put_object(
        bucket,
        object_key,
        expires=timedelta(seconds=_PRESIGN_EXPIRY_SECONDS),
    )


def presign_download(bucket: str, object_key: str) -> str:
    """Retorna URL pré-assinada para GET (visualização de foto).

    Mesmo motivo do presign_upload: precisa do host público, não do interno."""
    from datetime import timedelta

    client = get_minio_presign_client()
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
