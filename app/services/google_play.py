"""
Cliente da Google Play Developer API (app Android) e verificacao do push do Pub/Sub.

- Assinaturas: purchases.subscriptionsv2.get + acknowledge.
- Creditos (consumiveis): purchases.products.get + consume.
- RTDN: o Pub/Sub entrega por push com um token OIDC do Google no Authorization;
  validamos assinatura, audience e o e-mail da conta configurada.

A service account vem de GOOGLE_PLAY_SERVICE_ACCOUNT_JSON (variavel do Coolify).
O token de acesso e assinado aqui com google-auth e trocado via httpx.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from google.auth import crypt, jwt

from app.config import settings

logger = logging.getLogger(__name__)

_API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
_TIMEOUT = httpx.Timeout(20.0)

_token: tuple[str, float] | None = None
_certs: tuple[dict, float] | None = None
_lock = asyncio.Lock()


class GooglePlayError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.GOOGLE_PLAY_SERVICE_ACCOUNT_JSON)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _access_token() -> str:
    global _token
    async with _lock:
        if _token and _token[1] > time.time() + 60:
            return _token[0]
        if not is_configured():
            raise GooglePlayError("Google Play não configurado")
        info = json.loads(settings.GOOGLE_PLAY_SERVICE_ACCOUNT_JSON)
        signer = crypt.RSASigner.from_service_account_info(info)
        now = int(time.time())
        assertion = jwt.encode(signer, {
            "iss": info["client_email"], "scope": _SCOPE, "aud": info["token_uri"],
            "iat": now, "exp": now + 3600,
        })
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(info["token_uri"], data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion,
            })
        if response.status_code >= 400:
            raise GooglePlayError(f"Token da service account recusado ({response.status_code})")
        data = response.json()
        _token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        return _token[0]


async def _request(method: str, path: str) -> dict:
    token = await _access_token()
    url = f"{_API}/{settings.GOOGLE_PLAY_PACKAGE_NAME}/{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.request(method, url, headers={"Authorization": f"Bearer {token}"})
    if response.status_code >= 400:
        logger.warning("Google Play %s %s -> %s: %s", method, path.split("/tokens/")[0],
                       response.status_code, response.text[:300])
        raise GooglePlayError(f"Google Play respondeu {response.status_code}")
    return response.json() if response.content else {}


async def get_subscription(purchase_token: str) -> dict:
    return await _request("GET", f"purchases/subscriptionsv2/tokens/{quote(purchase_token, safe='')}")


async def acknowledge_subscription(product_id: str, purchase_token: str) -> None:
    await _request("POST", f"purchases/subscriptions/{quote(product_id, safe='')}/tokens/"
                           f"{quote(purchase_token, safe='')}:acknowledge")


async def get_product_purchase(product_id: str, purchase_token: str) -> dict:
    return await _request("GET", f"purchases/products/{quote(product_id, safe='')}/tokens/"
                                 f"{quote(purchase_token, safe='')}")


async def consume_product(product_id: str, purchase_token: str) -> None:
    await _request("POST", f"purchases/products/{quote(product_id, safe='')}/tokens/"
                           f"{quote(purchase_token, safe='')}:consume")


async def _google_certs() -> dict:
    global _certs
    if _certs and _certs[1] > time.time():
        return _certs[0]
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(_CERTS_URL)
    response.raise_for_status()
    _certs = (response.json(), time.time() + 3600)
    return _certs[0]


async def verify_pubsub_push(authorization: str | None) -> bool:
    """Confere o token OIDC que o Pub/Sub manda no push das notificacoes do Play."""
    audience = settings.GOOGLE_PLAY_RTDN_AUDIENCE
    expected_email = settings.GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT
    if not audience or not expected_email or not authorization or not authorization.startswith("Bearer "):
        return False
    try:
        claims = jwt.decode(authorization[7:], certs=await _google_certs(), audience=audience)
    except Exception as exc:  # assinatura, validade ou audience invalidos
        logger.warning("Push do Pub/Sub recusado: %s", exc)
        return False
    return (
        claims.get("iss") in ("accounts.google.com", "https://accounts.google.com")
        and claims.get("email") == expected_email
        and bool(claims.get("email_verified"))
    )
