"""Ownership of private object keys issued by the upload endpoints."""
import uuid

from fastapi import HTTPException, status

from app.services.storage import BUCKET_AVATARS, BUCKET_EVENTS


def owns_upload(bucket: str, key: str, user_id: uuid.UUID) -> bool:
    if bucket not in {BUCKET_EVENTS, BUCKET_AVATARS}:
        return False
    # Reject ambiguous paths before checking the exact owner segment.
    if not isinstance(key, str) or not key or any(c in key for c in ("\\", "%", "?", "#")):
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in key):
        return False
    parts = key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if bucket == BUCKET_AVATARS and parts[0] == "avatars":
        parts = parts[1:]  # /users/me/avatar/presign uses this prefix.
    return len(parts) == 2 and parts[0] == str(user_id)


def require_owned_upload(bucket: str, key: str, user_id: uuid.UUID) -> None:
    if not owns_upload(bucket, key, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Arquivo não encontrado")


def require_owned_event_photos(keys: list[str] | None, user_id: uuid.UUID) -> None:
    for key in keys or []:
        require_owned_upload(BUCKET_EVENTS, key, user_id)
