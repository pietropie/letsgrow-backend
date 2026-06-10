"""
Utilitário Redis para armazenamento de OTPs de recuperação de senha.

Chave: pwd_reset:{email}
Valor: código de 6 dígitos (str)
TTL: 900 segundos (15 minutos)
"""

import random
import string

import redis.asyncio as aioredis

from app.config import settings

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


OTP_TTL = 900  # 15 minutos
_OTP_PREFIX = "pwd_reset:"


def _key(email: str) -> str:
    return f"{_OTP_PREFIX}{email.lower()}"


async def create_otp(email: str) -> str:
    """Gera e armazena um OTP de 6 dígitos. Retorna o código."""
    code = "".join(random.choices(string.digits, k=6))
    r = _get_redis()
    await r.set(_key(email), code, ex=OTP_TTL)
    return code


async def verify_otp(email: str, code: str) -> bool:
    """Valida o OTP. Retorna True se correto (mas NÃO deleta — chamar delete_otp após usar)."""
    r = _get_redis()
    stored = await r.get(_key(email))
    return stored is not None and stored == code.strip()


async def delete_otp(email: str) -> None:
    """Remove o OTP do Redis (chamar após reset bem-sucedido)."""
    r = _get_redis()
    await r.delete(_key(email))
