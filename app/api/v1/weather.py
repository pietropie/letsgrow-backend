"""
Endpoint de clima — dados meteorológicos externos via Open-Meteo.

Uso:
    GET /api/v1/weather?lat=-23.55&lon=-46.63   público (o JWT enviado é ignorado)
    GET /api/v1/weather?grow_id=<uuid>          exige JWT; usa o local do ambiente do dono
    GET /api/v1/weather/places?q=campinas       exige JWT; busca de cidade (geocoding)

Open-Meteo é gratuito, sem API key, com limite generoso (~10k req/dia).
Docs: https://open-meteo.com/en/docs
"""
import time
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.user import User
from app.services.auth import get_current_user
from app.models.plant import Plant
from app.services.bob_weather import most_advanced_phase, weather_source, weather_tip
from app.services.grow_env import latest_grow_measurements

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Cache em memória (TTL 10 min por lat/lon arredondado) ────────────────────
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 600  # segundos
_CACHE_MAX = 2048

# Busca de cidade: nomes mudam pouco, cache de 1 h por texto normalizado
_places_cache: dict[str, tuple[float, Any]] = {}
_PLACES_TTL = 3600
_PLACES_MAX = 512

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"


def _cache_key(lat: float, lon: float) -> str:
    # Arredonda para 2 casas (~1 km de precisão) para maximizar cache hits
    return f"{round(lat, 2)},{round(lon, 2)}"


def _cached(cache: dict[str, tuple[float, Any]], key: str, ttl: float) -> Any | None:
    hit = cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    return None


def _remember(cache: dict[str, tuple[float, Any]], key: str, value: Any, ttl: float, limit: int) -> None:
    now = time.monotonic()
    if len(cache) >= limit:
        for stale in [k for k, (at, _) in cache.items() if now - at >= ttl]:
            del cache[stale]
        while len(cache) >= limit:
            del cache[next(iter(cache))]  # o mais antigo (dict mantém a ordem de inserção)
    cache[key] = (now, value)


async def _open_meteo(url: str, params: dict, timeout: float) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout ao buscar dados de clima.")
    except httpx.HTTPStatusError as exc:
        logger.error("Open-Meteo error %s: %s", exc.response.status_code, exc.response.text)
        raise HTTPException(status_code=502, detail="Erro ao obter dados de clima.")
    except Exception as exc:
        logger.error("Erro inesperado na API de clima: %s", exc)
        raise HTTPException(status_code=502, detail="Erro ao obter dados de clima.")


# ── Schema de resposta ────────────────────────────────────────────────────────

class WeatherTip(BaseModel):
    """Dica do Bobby "lá fora × dentro": regras determinísticas (app/services/bob_weather.py), nunca o LLM."""
    rule_id: str                   # R1..R7
    title: str
    body: str
    severity: Literal["info", "attention"]
    # Formato de `sources` do chat. kind: weather | environment_reading | event | sensor_reading
    # | target | plant (fase) | light_schedule
    sources: list[dict[str, Any]]
    cta_prompt: str                # primeira pergunta de "Perguntar ao Bobby sobre isso"


class WeatherResponse(BaseModel):
    temperature: float          # °C
    feels_like: float           # °C (apparent temperature)
    humidity: int               # % relativa
    uv_index: float             # 0–11+
    wind_speed: float           # km/h
    weather_code: int           # WMO code (ver tabela abaixo)
    weather_description: str    # texto em português
    is_day: bool                # true = dia, false = noite
    temp_max: float | None = None   # °C, máxima de hoje no fuso do local
    temp_min: float | None = None   # °C, mínima de hoje no fuso do local
    precipitation_probability_max: int | None = None  # %, maior chance de chuva hoje
    fetched_at: datetime        # quando o Open-Meteo respondeu (UTC); repetido enquanto vale o cache
    timezone: str | None = None             # fuso do local, ex. "America/Sao_Paulo"
    utc_offset_seconds: int | None = None   # deslocamento do fuso do local em relação a UTC
    # Só com grow_id em ambiente fechado; null enquanto nenhuma regra se aplica
    weather_tip: WeatherTip | None = None


class Place(BaseModel):
    name: str                   # "Campinas"
    admin1: str | None          # estado/região: "São Paulo"
    country: str | None         # "Brasil"
    lat: float                  # 2 casas, como o servidor guarda no ambiente
    lon: float
    label: str                  # sugestão para Grow.location_label: "Campinas, SP"


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


# Siglas dos estados para o rótulo curto ("Campinas, SP")
_BR_STATES = {
    "Acre": "AC", "Alagoas": "AL", "Amapá": "AP", "Amazonas": "AM", "Bahia": "BA", "Ceará": "CE",
    "Distrito Federal": "DF", "Espírito Santo": "ES", "Goiás": "GO", "Maranhão": "MA", "Mato Grosso": "MT",
    "Mato Grosso do Sul": "MS", "Minas Gerais": "MG", "Pará": "PA", "Paraíba": "PB", "Paraná": "PR",
    "Pernambuco": "PE", "Piauí": "PI", "Rio de Janeiro": "RJ", "Rio Grande do Norte": "RN",
    "Rio Grande do Sul": "RS", "Rondônia": "RO", "Roraima": "RR", "Santa Catarina": "SC",
    "São Paulo": "SP", "Sergipe": "SE", "Tocantins": "TO",
}


def _place_label(name: str, admin1: str | None, country: str | None, country_code: str | None) -> str:
    if country_code == "BR":
        region = _BR_STATES.get(admin1 or "", admin1)
    else:
        region = admin1 or country
    label = f"{name}, {region}" if region and region != name else name
    return label[:120]


# ── Autorização opcional ─────────────────────────────────────────────────────

_optional_bearer = HTTPBearer(auto_error=False)


async def grow_weather_owner(
    grow_id: uuid.UUID | None = Query(None, description="Ambiente do usuário; o servidor usa o local salvo nele."),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Só autentica quando há grow_id: lat/lon continua público e ignora o JWT, como antes."""
    if grow_id is None:
        return None
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Faça login para ver o clima do ambiente")
    return await get_current_user(credentials, db)


async def _forecast(lat: float, lon: float) -> WeatherResponse:
    key = _cache_key(lat, lon)
    cached = _cached(_cache, key, _CACHE_TTL)
    if cached is not None:
        return cached

    params = {
        "latitude": round(lat, 2),
        "longitude": round(lon, 2),
        "current": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "uv_index",
            "wind_speed_10m",
            "weather_code",
            "is_day",
        ]),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": 1,
        "wind_speed_unit": "kmh",
        "timezone": "auto",
    }
    data = await _open_meteo(FORECAST_URL, params, timeout=8.0)

    current = data.get("current", {})
    code = int(current.get("weather_code", 0))
    daily = data.get("daily") or {}

    def today(name: str) -> float | None:
        values = daily.get(name) or []
        return float(values[0]) if values and values[0] is not None else None

    temp_max, temp_min, rain = today("temperature_2m_max"), today("temperature_2m_min"), today("precipitation_probability_max")
    result = WeatherResponse(
        temperature=round(float(current.get("temperature_2m", 0)), 1),
        feels_like=round(float(current.get("apparent_temperature", 0)), 1),
        humidity=int(current.get("relative_humidity_2m", 0)),
        uv_index=round(float(current.get("uv_index", 0)), 1),
        wind_speed=round(float(current.get("wind_speed_10m", 0)), 1),
        weather_code=code,
        weather_description=_WMO_DESCRIPTIONS.get(code, "Condição desconhecida"),
        is_day=bool(current.get("is_day", 1)),
        temp_max=round(temp_max, 1) if temp_max is not None else None,
        temp_min=round(temp_min, 1) if temp_min is not None else None,
        precipitation_probability_max=int(rain) if rain is not None else None,
        fetched_at=datetime.now(timezone.utc),
        timezone=data.get("timezone"),
        utc_offset_seconds=data.get("utc_offset_seconds"),
    )
    _remember(_cache, key, result, _CACHE_TTL, _CACHE_MAX)
    return result


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/weather", response_model=WeatherResponse, tags=["weather"])
async def get_weather(
    lat: float | None = Query(None, ge=-90, le=90, description="Latitude (decimal)"),
    lon: float | None = Query(None, ge=-180, le=180, description="Longitude (decimal)"),
    grow_id: uuid.UUID | None = Query(None),
    owner: User | None = Depends(grow_weather_owner),
    db: AsyncSession = Depends(get_db),
):
    """
    Clima atual e máxima/mínima de hoje. Informe `lat` e `lon` **ou** `grow_id`.

    - **lat**, **lon**: coordenada em decimal (público, sem login).
    - **grow_id**: ambiente do usuário logado; o servidor lê o local salvo nele. Ambiente de
      outra conta dá 404; ambiente sem local dá 409. Em ambiente fechado a resposta pode trazer
      `weather_tip` (dica "lá fora × dentro" das regras do Bobby, com fontes).

    Fonte: [Open-Meteo](https://open-meteo.com) — gratuito, sem API key.
    Cache local de 10 minutos por localização (~1 km de precisão).
    """
    if grow_id is None:
        if lat is None or lon is None:
            raise HTTPException(status_code=422, detail="Informe lat e lon, ou grow_id.")
        return await _forecast(lat, lon)
    if lat is not None or lon is not None:
        raise HTTPException(status_code=422, detail="Use grow_id ou lat/lon, não os dois.")

    grow = (await db.execute(
        select(Grow).where(Grow.id == grow_id, Grow.user_id == owner.id)
    )).scalar_one_or_none()
    if grow is None:
        raise HTTPException(status_code=404, detail="Ambiente não encontrado")
    if grow.lat is None or grow.lon is None:
        raise HTTPException(status_code=409, detail="Defina o local do ambiente para ver o clima.")

    weather = await _forecast(grow.lat, grow.lon)
    if grow.grow_type == "outdoor":
        return weather  # o ambiente é o próprio lado de fora: não há "dentro" para comparar
    indoor = await latest_grow_measurements(db, grow.id)
    plants = (await db.execute(
        select(Plant.id, Plant.current_phase).where(Plant.grow_id == grow.id, Plant.is_active == True)  # noqa: E712
    )).all()
    phase, phase_plant_id = most_advanced_phase([(str(plant_id), phase) for plant_id, phase in plants])
    tip = weather_tip(
        grow=grow, weather=weather, indoor=indoor, phase=phase, phase_plant_id=phase_plant_id,
        now=datetime.now(timezone.utc),
        weather_src=weather_source(lat=grow.lat, lon=grow.lon, label=grow.location_label,
                                   fetched_at=weather.fetched_at, grow_id=str(grow.id)),
    )
    return weather.model_copy(update={"weather_tip": WeatherTip(**tip) if tip else None})


@router.get("/weather/places", response_model=list[Place], tags=["weather"])
async def search_places(
    q: str = Query(..., min_length=2, max_length=80, description="Nome da cidade"),
    current_user: User = Depends(get_current_user),
):
    """
    Busca de cidade para definir o local do ambiente (até 8 resultados, em português).

    Proxy do geocoding do Open-Meteo (sem chave). O app grava `lat`, `lon` e `label`
    no ambiente com `PATCH /grows/{id}` (`location_label`).
    """
    text = " ".join(q.split())
    if len(text) < 2:
        raise HTTPException(status_code=422, detail="Digite ao menos 2 letras.")
    key = text.casefold()
    cached = _cached(_places_cache, key, _PLACES_TTL)
    if cached is not None:
        return cached

    data = await _open_meteo(
        GEOCODING_URL, {"name": text, "count": 8, "language": "pt", "format": "json"}, timeout=5.0,
    )
    places = []
    for item in data.get("results") or []:
        try:
            lat, lon = round(float(item["latitude"]), 2), round(float(item["longitude"]), 2)
            name = str(item["name"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not name or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        admin1, country = item.get("admin1"), item.get("country")
        places.append(Place(
            name=name, admin1=admin1, country=country, lat=lat, lon=lon,
            label=_place_label(name, admin1, country, item.get("country_code")),
        ))
    _remember(_places_cache, key, places, _PLACES_TTL, _PLACES_MAX)
    return places
