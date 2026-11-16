"""
Limite de requisições por usuário e por IP (proteção contra uso da API fora do app).

Janela fixa contada no Redis (INCR + EXPIRE); se o Redis falhar, cai para um
contador em memória do processo, para a API nunca parar por causa do limitador.
Os números vêm do env (formato "quantidade/segundos") e são folgados: um uso real
do app não chega perto deles. Estourar responde 429 com Retry-After.

O IP é o da conexão. Só quando ela vem de um proxy confiável (o Traefik do Coolify,
numa rede privada) o X-Forwarded-For é lido, da direita para a esquerda, pulando os
proxies confiáveis: o primeiro endereço de fora é o do cliente.

Requisição com usuário logado conta só pelo usuário (o BFF do web fala com a API de
um IP só). O limite por IP vale para as rotas sem login: autenticação e públicas.
Para o web, o BFF manda o IP do navegador em X-LetsGrow-Client-IP, assinado com
X-LetsGrow-BFF-Key = BFF_SHARED_SECRET (comparação em tempo constante).
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import time
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    count: int
    seconds: int


def parse_rule(value: str) -> Rule:
    count, seconds = value.split("/")
    return Rule(int(count), int(seconds))


# (grupo, método, caminho) — a primeira que casar define o grupo da requisição
_GROUPS: list[tuple[str, str, re.Pattern]] = [
    ("auth", "POST", re.compile(r"^/api/v1/auth/(login|register|verify-email|resend-verification|forgot-password|"
                                r"reset-password|google|refresh)$")),
    ("chat", "POST", re.compile(r"^/api/v1/ai/chat(/stream|/[^/]+/regenerate)?$")),
    ("attachments", "POST", re.compile(r"^/api/v1/(uploads/presign|ai/conversations/[^/]+/attachments/save)$")),
    ("shop_click", "POST", re.compile(r"^/api/v1/shop/products/[^/]+/click$")),
]
# Fora do limitador: webhooks dos provedores (chegam em rajada), admin (token próprio) e a saúde
_EXEMPT = re.compile(r"^/api/v1/(credits|billing)/webhooks?/|^/api/v1/admin/|^/health$")


def _rules() -> dict[str, dict[str, Rule]]:
    """Grupo → {"user" | "ip": regra}. Lido do env a cada chamada (barato) para os testes poderem trocar."""
    s = settings
    return {
        "auth": {"ip": parse_rule(s.RATE_LIMIT_AUTH_IP)},
        "chat": {"user": parse_rule(s.RATE_LIMIT_CHAT_USER), "ip": parse_rule(s.RATE_LIMIT_CHAT_IP)},
        "attachments": {"user": parse_rule(s.RATE_LIMIT_ATTACHMENTS_USER)},
        "shop_click": {"user": parse_rule(s.RATE_LIMIT_SHOP_CLICK_USER), "ip": parse_rule(s.RATE_LIMIT_SHOP_CLICK_IP)},
        "public": {"ip": parse_rule(s.RATE_LIMIT_PUBLIC_IP)},
        "default": {"user": parse_rule(s.RATE_LIMIT_DEFAULT_USER), "ip": parse_rule(s.RATE_LIMIT_DEFAULT_IP)},
    }


def _trusted_networks():
    return [ipaddress.ip_network(cidr.strip(), strict=False)
            for cidr in settings.TRUSTED_PROXIES.split(",") if cidr.strip()]


def _is_trusted(address: str, networks) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def bff_client_ip(bff_key: str | None, client_ip_header: str | None) -> str | None:
    """IP do navegador repassado pelo BFF do web, só com o segredo certo e um IP válido.

    O BFF chama a API pelo endereço público e o Traefik descarta o X-Forwarded-For de fora;
    sem isto, todo o tráfego do web seria um IP só. O valor do segredo nunca vai para o log.
    """
    secret = settings.BFF_SHARED_SECRET
    if not secret or not bff_key or not client_ip_header:
        return None
    if not hmac.compare_digest(bff_key.encode(), secret.encode()):
        logger.warning("X-LetsGrow-BFF-Key inválida; o IP repassado foi ignorado")
        return None
    try:
        return str(ipaddress.ip_address(client_ip_header.strip()))
    except ValueError:
        logger.warning("X-LetsGrow-Client-IP inválido; usado o IP da conexão")
        return None


def client_ip(peer: str | None, forwarded_for: str | None, bff_key: str | None = None,
              client_ip_header: str | None = None) -> str:
    """IP real do cliente: o do BFF assinado; senão o X-Forwarded-For, só vindo de proxy confiável."""
    from_bff = bff_client_ip(bff_key, client_ip_header)
    if from_bff:
        return from_bff
    peer = peer or "unknown"
    networks = _trusted_networks()
    if not forwarded_for or not _is_trusted(peer, networks):
        return peer
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, networks):
            return hop
    return hops[0] if hops else peer


def _user_id(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    from app.services.auth import decode_token
    try:
        return str(decode_token(authorization.split(" ", 1)[1].strip(), "access"))
    except Exception:  # token inválido: conta pelo IP; a rota responde 401 depois
        return None


class _MemoryCounter:
    def __init__(self) -> None:
        self.values: dict[str, tuple[int, float]] = {}

    def hit(self, key: str, seconds: int, now: float) -> int:
        count, expires = self.values.get(key, (0, 0.0))
        if expires <= now:
            count = 0
            if len(self.values) > 50_000:  # limpeza ocasional das janelas vencidas
                self.values = {k: v for k, v in self.values.items() if v[1] > now}
        count += 1
        self.values[key] = (count, now + seconds)
        return count


memory = _MemoryCounter()


async def _hit(key: str, seconds: int, now: float) -> int:
    try:
        from app.services.redis_client import _get_redis
        redis = _get_redis()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, seconds + 1)
        return int(count)
    except Exception:
        return memory.hit(key, seconds, now)


def group_for(method: str, path: str, has_user: bool) -> str | None:
    if _EXEMPT.search(path) or not path.startswith("/api/v1/"):
        return None
    for group, group_method, pattern in _GROUPS:
        if method == group_method and pattern.match(path):
            return group
    return "default" if has_user else "public"


async def check(method: str, path: str, peer: str | None, headers: dict[str, str]) -> int | None:
    """Conta a requisição. Devolve os segundos de espera se algum limite estourou, senão None."""
    if not settings.RATE_LIMIT_ENABLED:
        return None
    user = _user_id(headers.get("authorization"))
    group = group_for(method, path, user is not None)
    if group is None:
        return None
    ip = client_ip(peer, headers.get("x-forwarded-for"), headers.get("x-letsgrow-bff-key"),
                   headers.get("x-letsgrow-client-ip"))
    rules = _rules()
    now = time.time()
    checks = [(group, kind, rule) for kind, rule in rules[group].items()]
    if group not in ("default", "public"):
        # o teto geral também vale para as rotas com limite próprio
        checks += [("default", kind, rule) for kind, rule in rules["default"].items()]
    wait = None
    for name, kind, rule in checks:
        # Logado conta só por usuário: o BFF do web chama a API de um IP só para todo mundo
        if kind == "ip" and user is not None:
            continue
        subject = user if kind == "user" else ip
        if subject is None:
            continue
        window = int(now // rule.seconds)
        count = await _hit(f"rl:{name}:{kind}:{subject}:{rule.seconds}:{window}", rule.seconds, now)
        if count > rule.count:
            left = rule.seconds - int(now % rule.seconds)
            wait = max(wait or 0, left)
    if wait is not None:
        logger.warning("Limite de requisições: grupo=%s ip=%s usuário=%s", group, ip, user or "-")
    return wait


class RateLimitMiddleware:
    """Middleware ASGI puro: não envolve a resposta, então o streaming (SSE) do chat continua igual."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        peer = (scope.get("client") or (None, None))[0]
        wait = await check(scope["method"], scope["path"], peer, headers)
        if wait is None:
            return await self.app(scope, receive, send)
        body = json.dumps({"detail": f"Muitas requisições. Tente de novo em {wait} s."}).encode()
        await send({"type": "http.response.start", "status": 429, "headers": [
            (b"content-type", b"application/json"), (b"retry-after", str(wait).encode()),
            (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})
