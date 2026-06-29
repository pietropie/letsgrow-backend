"""
Endpoint de clima — dados meteorológicos externos via Open-Meteo.

Uso:
    GET /api/v1/weather?lat=-23.55&lon=-46.63

Não requer autenticação (dados públicos), mas o app mobile passa o JWT
de qualquer forma via Authorization header (ignored here).

Open-Meteo é gratuito, sem API key, com limite generoso (~10k req/dia).
Docs: https://open-meteo.com/en/docs
"""
import time
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Cache em memória (TTL 10 min por lat/lon arredondado) ────────────────────
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 600  # segundos


def _cache_key(lat: float, lon: float) -> str:
    # Arredonda para 2 casas (~1 km de precisão) para maximizar cache hits
    return f"{round(lat, 2)},{round(lon, 2)}"


# ── Schema de resposta ────────────────────────────────────────────────────────

class WeatherResponse(BaseModel):
    temperature: float          # °C
    feels_like: float           # °C (apparent temperature)
    humidity: int               # % relativa
    uv_index: float             # 0–11+
    wind_speed: float           # km/h
    weather_code: int           # WMO code (ver tabela abaixo)
    weather_description: str    # texto em português
    is_day: bool                # true = dia, false = noite


# WMO Weather Interpretation Codes → descrição PT-BR
_WMO_DESCRIPTIONS: dict[int, str] = {
    0: "Céu limpo",
    1: "Predominantemente limpo",
    2: "Parcialmente nublado",
    3: "Nublado",
    45: "Névoa",
    48: "Névoa com gelo",
    51: "Garoa leve",
    53: "Garoa moderada",
    55: "Garoa intensa",
    61: "Chuva leve",
    63: "Chuva moderada",
    65: "Chuva forte",
    71: "Neve leve",
    73: "Neve moderada",
    75: "Neve forte",
    77: "Granizo",
    80: "Pancadas de chuva leves",
    81: "Pancadas de chuva moderadas",
    82: "Pancadas de chuva fortes",
    85: "Pancadas de neve leves",
    86: "Pancadas de neve fortes",
    95: "Tempestade",
    96: "Tempestade com granizo leve",
    99: "Tempestade com granizo forte",
}


# ── Endpoint ─────────────────────────────────────────────────────────────────

@router.get("/weather", response_model=WeatherResponse, tags=["weather"])
async def get_weather(
    lat: float = Query(..., ge=-90, le=90, description="Latitude (decimal)"),
    lon: float = Query(..., ge=-180, le=180, description="Longitude (decimal)"),
):
    """
    Retorna dados meteorológicos atuais para a latitude/longitude informada.

    - **lat**: latitude em decimal (ex: -23.5505)
    - **lon**: longitude em decimal (ex: -46.6333)

    Fonte: [Open-Meteo](https://open-meteo.com) — gratuito, sem API key.
    Cache local de 10 minutos por localização (~1 km de precisão).
    """
    key = _cache_key(lat, lon)
    now = time.monotonic()

    # Verifica cache
    if key in _cache:
        cached_at, cached_data = _cache[key]
        if now - cached_at < _CACHE_TTL:
            return cached_data

    # Chama Open-Meteo
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "uv_index",
            "wind_speed_10m",
            "weather_code",
            "is_day",
        ]),
        "wind_speed_unit": "kmh",
        "timezone": "auto",
    }

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout ao buscar dados de clima.")
    except httpx.HTTPStatusError as exc:
        logger.error("Open-Meteo error %s: %s", exc.response.status_code, exc.response.text)
        raise HTTPException(status_code=502, detail="Erro ao obter dados de clima.")
    except Exception as exc:
        logger.error("Erro inesperado na API de clima: %s", exc)
        raise HTTPException(status_code=502, detail="Erro ao obter dados de clima.")

    current = data.get("current", {})
    code = int(current.get("weather_code", 0))

    result = WeatherResponse(
        temperature=round(float(current.get("temperature_2m", 0)), 1),
        feels_like=round(float(current.get("apparent_temperature", 0)), 1),
        humidity=int(current.get("relative_humidity_2m", 0)),
        uv_index=round(float(current.get("uv_index", 0)), 1),
        wind_speed=round(float(current.get("wind_speed_10m", 0)), 1),
        weather_code=code,
        weather_description=_WMO_DESCRIPTIONS.get(code, "Condição desconhecida"),
        is_day=bool(current.get("is_day", 1)),
    )

    # Armazena no cache
    _cache[key] = (now, result)

    return result
