"""
Serviço de push notifications via Expo Push API (gratuito, sem SDK próprio).

Documentação: https://docs.expo.dev/push-notifications/sending-notifications/

Uso:
    from app.services.push import send_push, send_push_batch

    await send_push(token, title="Bob diz:", body="Hora de verificar a rega!")
    await send_push_batch(tokens, title="...", body="...")
"""
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def _is_valid_expo_token(token: str) -> bool:
    """Valida formato básico de token Expo."""
    return token.startswith("ExponentPushToken[") or token.startswith("ExpoPushToken[")


async def send_push(
    token: str,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
    *,
    sound: str = "default",
    badge: int | None = None,
) -> bool:
    """
    Envia uma notificação push via Expo.

    Retorna True em sucesso, False se o token for inválido ou a chamada falhar.
    Nunca lança exceção — erros são logados.
    """
    if not token or not _is_valid_expo_token(token):
        logger.debug("Token inválido ignorado: %s", token)
        return False

    payload: dict[str, Any] = {
        "to": token,
        "title": title,
        "body": body,
        "sound": sound,
    }
    if data:
        payload["data"] = data
    if badge is not None:
        payload["badge"] = badge

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(EXPO_PUSH_URL, json=payload, headers=_HEADERS)
            result = resp.json()

        # Expo retorna {"data": {"status": "ok"}} em sucesso
        status = result.get("data", {}).get("status", "")
        if status == "ok":
            return True

        # "DeviceNotRegistered" → token expirado — logar e retornar False
        details = result.get("data", {}).get("details", {})
        error = details.get("error", "")
        logger.warning("Expo push retornou status=%s error=%s token=...%s", status, error, token[-12:])
        return False

    except Exception as exc:
        logger.error("Falha ao enviar push para token ...%s: %s", token[-12:], exc)
        return False


async def send_push_batch(
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> dict[str, int]:
    """
    Envia push para múltiplos tokens em uma única chamada HTTP (batch Expo).

    A Expo aceita até 100 mensagens por chamada. Listas maiores são divididas
    automaticamente.

    Retorna {"sent": N, "failed": M}.
    """
    valid = [t for t in tokens if t and _is_valid_expo_token(t)]
    if not valid:
        return {"sent": 0, "failed": len(tokens)}

    messages = [
        {
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            **({"data": data} if data else {}),
        }
        for token in valid
    ]

    sent = 0
    failed = len(tokens) - len(valid)  # tokens inválidos já contam como falha

    # Expo aceita até 100 por batch
    chunk_size = 100
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for i in range(0, len(messages), chunk_size):
                chunk = messages[i : i + chunk_size]
                resp = await client.post(EXPO_PUSH_URL, json=chunk, headers=_HEADERS)
                results = resp.json().get("data", [])
                for item in results:
                    if isinstance(item, dict) and item.get("status") == "ok":
                        sent += 1
                    else:
                        failed += 1
    except Exception as exc:
        logger.error("Erro no batch de push: %s", exc)
        failed += len(valid) - sent

    logger.info("Push batch: %d enviados, %d falhas (total=%d)", sent, failed, len(tokens))
    return {"sent": sent, "failed": failed}
