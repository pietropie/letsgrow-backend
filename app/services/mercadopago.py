"""
Cliente do Mercado Pago (webapp): Checkout Pro, assinaturas (preapproval) e webhooks.

- Checkout Pro (preferencia): creditos (Pix ou cartao) e periodo avulso de plano (Pix).
- Preapproval: assinatura recorrente no cartao.
- Webhook: a assinatura x-signature e validada aqui; o estado nunca vem do corpo da
  notificacao: o backend sempre consulta o pagamento/preapproval na API.

Sandbox = mesmo endpoint com o access token de teste (TEST-...).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0)


class MercadoPagoError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.MP_ACCESS_TOKEN)


def verify_signature(x_signature: str | None, x_request_id: str | None, data_id: str | None) -> bool:
    """Valida o cabecalho x-signature ("ts=...,v1=...") com MP_WEBHOOK_SECRET.

    Manifesto documentado pelo MP: "id:{data.id};request-id:{x-request-id};ts:{ts};",
    omitindo as partes ausentes; data.id vem da query string e vai em minusculas.
    """
    secret = settings.MP_WEBHOOK_SECRET
    if not secret or not x_signature:
        return False
    parts = dict(
        item.strip().split("=", 1) for item in x_signature.split(",") if "=" in item
    )
    ts, v1 = parts.get("ts"), parts.get("v1")
    if not ts or not v1:
        return False
    manifest = ""
    if data_id:
        manifest += f"id:{data_id.lower()};"
    if x_request_id:
        manifest += f"request-id:{x_request_id};"
    manifest += f"ts:{ts};"
    expected = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _request(method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                   idempotency_key: str | None = None) -> dict:
    if not is_configured():
        raise MercadoPagoError("Mercado Pago não configurado")
    headers = {"Authorization": f"Bearer {settings.MP_ACCESS_TOKEN}"}
    if idempotency_key:
        headers["X-Idempotency-Key"] = idempotency_key
    async with httpx.AsyncClient(base_url=settings.MP_API_URL, timeout=_TIMEOUT) as client:
        response = await client.request(method, path, json=json, params=params, headers=headers)
    if response.status_code >= 400:
        # Corpo do erro do MP nao traz dado de cartao; o token nunca e logado.
        logger.warning("Mercado Pago %s %s -> %s: %s", method, path, response.status_code, response.text[:500])
        raise MercadoPagoError(f"Mercado Pago respondeu {response.status_code}")
    return response.json()


def _notification_url() -> str:
    return f"{settings.BILLING_PUBLIC_API_URL.rstrip('/')}/billing/webhooks/mercadopago"


_RETURN_PAGES = {
    "credits": "/conta/creditos/retorno?purchase_id=",
    "subscription": "/conta/planos/retorno?subscription_id=",
}


def return_url(kind: str, ref_id: str) -> str:
    """Pagina de retorno do webapp (a mesma para sucesso, pendente e falha).

    A pagina ignora os parametros do MP e consulta o estado pelo id na API.
    """
    base = settings.WEB_APP_URL.rstrip("/")
    return f"{base}{_RETURN_PAGES[kind]}{ref_id}" if base else ""


async def create_preference(
    *, external_reference: str, title: str, amount: float, payer_email: str, kind: str, ref_id: str,
    pix_only: bool,
) -> dict:
    """Checkout Pro de pagamento unico. Boleto fica fora (confirmacao demora dias)."""
    excluded = [{"id": "ticket"}]
    if pix_only:
        excluded += [{"id": "credit_card"}, {"id": "debit_card"}, {"id": "prepaid_card"}]
    body = {
        "items": [{"id": external_reference, "title": title, "quantity": 1,
                   "currency_id": "BRL", "unit_price": round(float(amount), 2)}],
        "payer": {"email": payer_email},
        "external_reference": external_reference,
        "notification_url": _notification_url(),
        "payment_methods": {"excluded_payment_types": excluded, "installments": 1},
        "statement_descriptor": "LETSGROW",
    }
    back = return_url(kind, ref_id)
    if back:
        body["back_urls"] = {"success": back, "pending": back, "failure": back}
        body["auto_return"] = "approved"
    return await _request("POST", "/checkout/preferences", json=body, idempotency_key=external_reference)


async def create_preapproval(
    *, external_reference: str, reason: str, amount: float, months: int, payer_email: str,
    ref_id: str, start_date: datetime | None,
) -> dict:
    """Assinatura no cartao sem plano associado: devolve init_point para o usuario autorizar."""
    recurring: dict = {"frequency": months, "frequency_type": "months",
                       "transaction_amount": round(float(amount), 2), "currency_id": "BRL"}
    if start_date is not None:
        recurring["start_date"] = start_date.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    body = {
        "reason": reason,
        "external_reference": external_reference,
        "payer_email": payer_email,
        "auto_recurring": recurring,
        "back_url": return_url("subscription", ref_id) or settings.BILLING_PUBLIC_API_URL,
        "status": "pending",
    }
    return await _request("POST", "/preapproval", json=body, idempotency_key=external_reference)


async def get_payment(payment_id: str) -> dict:
    return await _request("GET", f"/v1/payments/{payment_id}")


async def search_payments(external_reference: str) -> list[dict]:
    data = await _request("GET", "/v1/payments/search", params={
        "external_reference": external_reference, "sort": "date_created", "criteria": "desc",
    })
    return data.get("results") or []


async def get_preapproval(preapproval_id: str) -> dict:
    return await _request("GET", f"/preapproval/{preapproval_id}")


async def get_authorized_payment(authorized_payment_id: str) -> dict:
    return await _request("GET", f"/authorized_payments/{authorized_payment_id}")


async def cancel_preapproval(preapproval_id: str) -> dict:
    return await _request("PUT", f"/preapproval/{preapproval_id}", json={"status": "cancelled"})
