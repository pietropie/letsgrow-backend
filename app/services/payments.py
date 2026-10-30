"""
Interface de provedor de pagamento para a compra de creditos avulsos na web.

O padrao (PAYMENT_PROVIDER=unconfigured) recusa a compra com PaymentUnavailable, e o
app mostra "Compra ainda nao disponivel". Com PAYMENT_PROVIDER=mercadopago, a compra
abre o Checkout Pro (Pix ou cartao). As notificacoes do Mercado Pago chegam todas em
/billing/webhooks/mercadopago (services/billing.py), inclusive as de creditos.
No Android a compra e pelo Google Play (POST /billing/google/verify), nunca por aqui.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.config import settings
from app.models.credits import CreditPurchase
from app.models.user import User


class PaymentUnavailable(Exception):
    pass


class InvalidWebhook(Exception):
    pass


@dataclass
class CheckoutSession:
    provider_ref: str
    checkout_url: str


@dataclass
class WebhookEvent:
    provider_ref: str
    # paid | failed | canceled
    status: str


class PaymentProvider(Protocol):
    name: str

    async def create_checkout(self, purchase: CreditPurchase, user: User) -> CheckoutSession: ...

    async def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        """Valida a assinatura do provedor e devolve o evento; InvalidWebhook se invalido."""
        ...


class UnconfiguredProvider:
    name = "unconfigured"

    async def create_checkout(self, purchase: CreditPurchase, user: User) -> CheckoutSession:
        raise PaymentUnavailable("Compra ainda não disponível")

    async def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        raise InvalidWebhook("Nenhum provedor de pagamento configurado")


class MercadoPagoProvider:
    name = "mercadopago"

    async def create_checkout(self, purchase: CreditPurchase, user: User) -> CheckoutSession:
        from app.services import mercadopago as mp

        if not mp.is_configured():
            raise PaymentUnavailable("Compra ainda não disponível")
        reference = f"credits:{purchase.id}"
        try:
            data = await mp.create_preference(
                external_reference=reference, title=f"LetsGrow · {purchase.label}",
                amount=float(purchase.price_brl), payer_email=user.email, kind="credits",
                ref_id=str(purchase.id), pix_only=False,
            )
        except mp.MercadoPagoError:
            raise PaymentUnavailable("Não foi possível iniciar o pagamento. Tente de novo.")
        url = data.get("init_point")
        if settings.MP_ACCESS_TOKEN.startswith("TEST-") and data.get("sandbox_init_point"):
            url = data["sandbox_init_point"]
        return CheckoutSession(provider_ref=reference, checkout_url=url)

    async def parse_webhook(self, body: bytes, headers: dict[str, str]) -> WebhookEvent:
        raise InvalidWebhook("Notificações do Mercado Pago chegam em /billing/webhooks/mercadopago")


_PROVIDERS: dict[str, type] = {"unconfigured": UnconfiguredProvider, "mercadopago": MercadoPagoProvider}


def get_payment_provider() -> PaymentProvider:
    provider = _PROVIDERS.get(settings.PAYMENT_PROVIDER)
    if provider is None:
        return UnconfiguredProvider()
    return provider()
