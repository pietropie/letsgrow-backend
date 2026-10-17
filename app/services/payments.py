"""
Interface de provedor de pagamento para a compra de creditos avulsos.

Nenhum provedor real esta integrado. O padrao (PAYMENT_PROVIDER=unconfigured)
recusa a compra com PaymentUnavailable, e o app mostra "Compra ainda nao
disponivel". Para integrar um provedor, implemente PaymentProvider (checkout +
validacao do webhook com a assinatura do provedor) e registre em _PROVIDERS.
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


_PROVIDERS: dict[str, type] = {"unconfigured": UnconfiguredProvider}


def get_payment_provider() -> PaymentProvider:
    provider = _PROVIDERS.get(settings.PAYMENT_PROVIDER)
    if provider is None:
        return UnconfiguredProvider()
    return provider()
