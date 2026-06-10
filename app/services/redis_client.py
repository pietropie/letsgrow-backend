"""
Utilitário Redis para armazenamento de OTPs (recuperação de senha e verificação de email).

Recuperação de senha:
  Chave: pwd_reset:{email}    TTL: 900s (15 min)

Verificação de email:
  Chave: email_verify:{email}  TTL: 900s (15 min)
"""

import random
import string

import redis.asyncio as aioredis

from app.config import settings

_redis: aioredis.Redis | None = None
OTP_TTL = 900  # 15 minutos


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def _gen_code() -> str:
    return "".join(random.choices(string.digits, k=6))


# ─── Recuperação de senha ─────────────────────────────────────────────────────

async def create_otp(email: str) -> str:
    code = _gen_code()
    await _get_redis().set(f"pwd_reset:{email.lower()}", code, ex=OTP_TTL)
    return code


async def verify_otp(email: str, code: str) -> bool:
    stored = await _get_redis().get(f"pwd_reset:{email.lower()}")
    return stored is not None and stored == code.strip()


async def delete_otp(email: str) -> None:
    await _get_redis().delete(f"pwd_reset:{email.lower()}")


# ─── Verificação de email ─────────────────────────────────────────────────────

async def create_email_verification_otp(email: str) -> str:
    code = _gen_code()
    await _get_redis().set(f"email_verify:{email.lower()}", code, ex=OTP_TTL)
    return code


async def verify_email_verification_otp(email: str, code: str) -> bool:
    stored = await _get_redis().get(f"email_verify:{email.lower()}")
    return stored is not None and stored == code.strip()


async def delete_email_verification_otp(email: str) -> None:
    await _get_redis().delete(f"email_verify:{email.lower()}")
