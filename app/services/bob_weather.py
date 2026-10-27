"""Dica do Bobby "la fora x dentro" do card de clima do Inicio (UX item 11b).

Regras deterministicas, como as de bob_rules: nao chama LLM, nao consulta o banco e nao
gasta consulta do plano. So usa dado real (clima do Open-Meteo, ultima leitura de dentro,
faixa definida, fase das plantas e programacao de luz); se falta um dado, a regra nao
dispara. Mostra no maximo uma dica: a primeira regra de WEATHER_TIP_RULES que responder
(a lista esta em ordem de prioridade). As fontes seguem o formato de `sources` do chat
(app/services/chat_sources.py).

Portoes, antes de qualquer regra: ambiente outdoor (nao ha "dentro"), clima com mais de 1 h
ou nenhuma leitura de dentro com ate 6 h. Uma leitura com mais de 6 h conta como ausente.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable, Literal

from app.schemas.grow import EnvironmentMeasurement
from app.services.chat_sources import _number

# Limites ajustaveis sem deploy do app (so do backend)
MAX_WEATHER_AGE = timedelta(hours=1)
MAX_INDOOR_AGE = timedelta(hours=6)

FLOWER_PHASES = frozenset({"flower", "late_flower"})
YOUNG_PHASES = frozenset({"germination", "seedling"})
# Fase mais avancada entre as plantas ativas; colheita em diante nao conta (a planta saiu do cultivo)
PHASE_RANK = {"germination": 1, "seedling": 2, "veg": 3, "flower": 4, "late_flower": 5}
PHASE_LABELS = {"germination": "Germinação", "seedling": "Muda", "veg": "Vegetativa", "flower": "Floração",
                "late_flower": "Floração final"}

DEW_A, DEW_B = 17.62, 243.12  # Magnus

R5_IN_RH, R5_IN_RH_FLOWER, R5_DEW_MARGIN_C, R5_HOURS_TO_DARK = 65, 60, 1.0, 6
# R1, R2 e R7: a umidade de fora entra como a que esse ar teria na temperatura de dentro
# (outdoor_rh_at_indoor_temp, pelo ponto de orvalho), nunca a umidade relativa crua de fora.
R2_DELTA_RH, R2_OUT_RH, R2_OUT_RH_FLOWER = 15, 75, 70
R1_DELTA_RH, R1_ABOVE_TARGET_RH, R1_IN_RH, R1_IN_RH_FLOWER = 20, 3, 60, 55
R7_OUT_RH, R7_IN_RH = 40, 65
R3_ABOVE_TARGET_C, R3_OUT_T, R3_IN_BELOW_TARGET_C = 4, 30, 1
R4_BELOW_TARGET_C, R4_OUT_T, R4_HOURS_TO_DARK = 8, 15, 3
R6_TEMP_C, R6_RH = 2, 10

Uses = Literal["weather", "indoor_temp", "indoor_humidity", "target_temp", "target_humidity", "phase", "light"]


@dataclass(frozen=True)
class Light:
    """Programacao de luz no relogio do local do ambiente."""

    on: bool
    off_at: time | None          # None = 24/0, nunca apaga
    minutes_to_off: int | None   # None se ja esta apagada ou nunca apaga


@dataclass(frozen=True)
class OutdoorIndoor:
    """O que as regras comparam. Leituras de dentro com mais de 6 h chegam como None."""

    grow_name: str
    outdoor_temp_c: float
    outdoor_humidity_rh: float
    outdoor_temp_max_c: float | None
    outdoor_temp_min_c: float | None
    indoor_temp_c: float | None
    indoor_humidity_rh: float | None
    target_temp_c: float | None
    target_humidity_rh: float | None
    phase: str | None            # fase mais avancada entre as plantas ativas
    light: Light | None          # None = sem programacao ou sem fuso do local

    @property
    def flowering(self) -> bool:
        return self.phase in FLOWER_PHASES


@dataclass(frozen=True)
class Tip:
    rule_id: str
    title: str
    body: str
    severity: Literal["info", "attention"]
    cta_prompt: str
    uses: tuple[Uses, ...] = field(default=())


WeatherTipRule = Callable[[OutdoorIndoor], "Tip | None"]


# ── Auxiliares ───────────────────────────────────────────────────────────────

def _t(value: float) -> str:
    return _number(value)


def _rh(value: float) -> str:
    return str(round(value))


def dew_point_c(temp_c: float, humidity_rh: float) -> float | None:
    if humidity_rh <= 0:
        return None
    gamma = math.log(humidity_rh / 100) + DEW_A * temp_c / (DEW_B + temp_c)
    return DEW_B * gamma / (DEW_A - gamma)


def light_state(on_at: time | None, light_minutes: int | None, local_now: datetime | None) -> Light | None:
    if light_minutes is None or local_now is None:
        return None
    if light_minutes >= 1440:
        return Light(on=True, off_at=None, minutes_to_off=None)
    if on_at is None:
        return None
    start = on_at.hour * 60 + on_at.minute
    now = local_now.hour * 60 + local_now.minute
    elapsed = (now - start) % 1440
    off = (start + light_minutes) % 1440
    off_at = time(off // 60, off % 60)
    if elapsed < light_minutes:
        return Light(on=True, off_at=off_at, minutes_to_off=light_minutes - elapsed)
    return Light(on=False, off_at=off_at, minutes_to_off=None)


def _dark_within(light: Light | None, hours: int) -> bool:
    if light is None or (light.on and light.off_at is None):
        return False
    return not light.on or light.minutes_to_off <= hours * 60


def _vapor_pressure_hpa(temp_c: float) -> float:
    return 6.112 * math.exp(DEW_A * temp_c / (DEW_B + temp_c))


def outdoor_rh_at_indoor_temp(out_t: float, out_rh: float, in_t: float) -> float | None:
    """Umidade que o ar de fora teria na temperatura de dentro (mesmo ponto de orvalho).

    E a comparacao fisicamente correta: umidade relativa de temperaturas diferentes nao se
    compara (15 °C e 75 % la fora viram ~40 % a 25 °C dentro). Pode passar de 100 %.
    """
    dew = dew_point_c(out_t, out_rh)
    if dew is None:
        return None
    return 100 * _vapor_pressure_hpa(dew) / _vapor_pressure_hpa(in_t)


def _cta(title: str, ctx: OutdoorIndoor) -> str:
    return f"{title}. Como usar isso na exaustão de {ctx.grow_name} hoje?"


def _outdoor_air(ctx: OutdoorIndoor, equivalent: float) -> str:
    return (f"Fora: {_rh(ctx.outdoor_humidity_rh)} % a {_t(ctx.outdoor_temp_c)} °C, o que dá cerca de "
            f"{_rh(min(equivalent, 100))} % na temperatura de dentro ({_t(ctx.indoor_temp_c)} °C).")


# ── Regras (em ordem de prioridade) ──────────────────────────────────────────

def r5_night_condensation(ctx: OutdoorIndoor) -> Tip | None:
    t_in, rh_in, t_min = ctx.indoor_temp_c, ctx.indoor_humidity_rh, ctx.outdoor_temp_min_c
    if t_in is None or rh_in is None or t_min is None:
        return None
    if rh_in < (R5_IN_RH_FLOWER if ctx.flowering else R5_IN_RH) or not _dark_within(ctx.light, R5_HOURS_TO_DARK):
        return None
    dew = dew_point_c(t_in, rh_in)
    if dew is None or dew < t_min - R5_DEW_MARGIN_C:
        return None
    # Compara como o texto mostra (1 casa): "abaixo" so quando a diferenca aparece
    relation = "abaixo do" if round(t_min, 1) < round(dew, 1) else "perto do"
    when = (f"quando a luz apagar às {ctx.light.off_at:%H:%M}" if ctx.light.on else "com a luz apagada")
    title = "Risco de condensação nesta noite"
    return Tip(
        "R5", title,
        f"A mínima de hoje lá fora é {_t(t_min)} °C, {relation} ponto de orvalho em {ctx.grow_name} "
        f"({_t(dew)} °C). Superfícies frias podem molhar {when}.",
        "attention", _cta(title, ctx), ("weather", "indoor_temp", "indoor_humidity", "light"),
    )


def r2_more_humid_outside(ctx: OutdoorIndoor) -> Tip | None:
    t_in, rh_in = ctx.indoor_temp_c, ctx.indoor_humidity_rh
    if t_in is None or rh_in is None:
        return None
    equivalent = outdoor_rh_at_indoor_temp(ctx.outdoor_temp_c, ctx.outdoor_humidity_rh, t_in)
    if equivalent is None or equivalent < rh_in + R2_DELTA_RH:
        return None
    if equivalent < R2_OUT_RH and not (ctx.flowering and equivalent >= R2_OUT_RH_FLOWER):
        return None
    lead = "Na floração, puxar" if ctx.flowering else "Puxar"
    title = f"Lá fora está mais úmido que em {ctx.grow_name}"
    return Tip(
        "R2", title,
        f"{_outdoor_air(ctx, equivalent)} Dentro: {_rh(rh_in)} %. {lead} esse ar agora pode subir a umidade "
        "dentro. Acompanhe a leitura na próxima hora.",
        "attention", _cta(title, ctx),
        ("weather", "indoor_temp", "indoor_humidity") + (("phase",) if ctx.flowering else ()),
    )


def r1_drier_outside(ctx: OutdoorIndoor) -> Tip | None:
    t_in, rh_in, target = ctx.indoor_temp_c, ctx.indoor_humidity_rh, ctx.target_humidity_rh
    if t_in is None or rh_in is None:
        return None
    equivalent = outdoor_rh_at_indoor_temp(ctx.outdoor_temp_c, ctx.outdoor_humidity_rh, t_in)
    if equivalent is None or equivalent > rh_in - R1_DELTA_RH:
        return None
    limit = target + R1_ABOVE_TARGET_RH if target is not None else (R1_IN_RH_FLOWER if ctx.flowering else R1_IN_RH)
    if rh_in <= limit:
        return None
    above = f", acima da sua faixa de {_rh(target)} %" if target is not None else ""
    title = "Lá fora está bem mais seco"
    uses = (("weather", "indoor_temp", "indoor_humidity")
            + (("target_humidity",) if target is not None else ("phase",) if ctx.flowering else ()))
    return Tip(
        "R1", title,
        f"{_outdoor_air(ctx, equivalent)} Em {ctx.grow_name}: {_rh(rh_in)} %{above}. "
        "Renovar o ar pela exaustão tende a baixar a umidade dentro.",
        "info", _cta(title, ctx), uses,
    )


def r7_dry_air_for_seedlings(ctx: OutdoorIndoor) -> Tip | None:
    t_in, rh_in, target = ctx.indoor_temp_c, ctx.indoor_humidity_rh, ctx.target_humidity_rh
    if ctx.phase not in YOUNG_PHASES or t_in is None or rh_in is None:
        return None
    equivalent = outdoor_rh_at_indoor_temp(ctx.outdoor_temp_c, ctx.outdoor_humidity_rh, t_in)
    # Seco depois de chegar a temperatura de dentro, e mais seco que o ar de dentro
    if equivalent is None or equivalent >= R7_OUT_RH or equivalent >= rh_in:
        return None
    if rh_in >= (target if target is not None else R7_IN_RH):
        return None
    title = "Ar de fora está seco para mudas"
    return Tip(
        "R7", title,
        f"{_outdoor_air(ctx, equivalent)} Mudas costumam pedir umidade mais alta; trazer esse ar pode secar "
        f"ainda mais o ar em {ctx.grow_name} ({_rh(rh_in)} %).",
        "info", _cta(title, ctx),
        ("weather", "indoor_temp", "indoor_humidity", "phase") + (("target_humidity",) if target is not None else ()),
    )


def r3_hotter_outside(ctx: OutdoorIndoor) -> Tip | None:
    t_out, t_in, target = ctx.outdoor_temp_c, ctx.indoor_temp_c, ctx.target_temp_c
    # Fora precisa estar mais quente que dentro: senao o ar de fora esfria, nao aquece
    if t_in is None or t_out <= t_in:
        return None
    # Sem faixa, o limite de fora (30 °C) equivale a uma faixa de 26 °C; dentro usa a mesma referencia.
    reference = target if target is not None else R3_OUT_T - R3_ABOVE_TARGET_C
    if t_out < reference + R3_ABOVE_TARGET_C or t_in < reference - R3_IN_BELOW_TARGET_C:
        return None
    later = (f"a troca rende mais quando esfriar (mínima de hoje {_t(ctx.outdoor_temp_min_c)} °C)"
             if ctx.outdoor_temp_min_c is not None else "a troca rende mais quando esfriar")
    if target is not None:
        title, band = "Lá fora está mais quente que a sua faixa", f" Sua faixa: {_t(target)} °C."
    else:
        title, band = "Lá fora está quente", ""
    return Tip(
        "R3", title,
        f"Fora: {_t(t_out)} °C.{band} Dentro: {_t(t_in)} °C. Puxar ar de fora agora tende a aquecer o ar em "
        f"{ctx.grow_name}; {later}.",
        "info", _cta(title, ctx),
        ("weather", "indoor_temp") + (("target_temp",) if target is not None else ()),
    )


def r4_cold_night_outside(ctx: OutdoorIndoor) -> Tip | None:
    t_out, target = ctx.outdoor_temp_c, ctx.target_temp_c
    limit = target - R4_BELOW_TARGET_C if target is not None else R4_OUT_T
    if t_out > limit or not _dark_within(ctx.light, R4_HOURS_TO_DARK):
        return None
    if target is not None:
        title = "A noite lá fora está fria para a sua faixa"
        body = (f"Fora: {_t(t_out)} °C. Sua faixa: {_t(target)} °C. Com a luz apagada, a troca de ar pode deixar "
                f"o ar em {ctx.grow_name} abaixo da faixa.")
    else:
        title = "A noite lá fora está fria"
        body = f"Fora: {_t(t_out)} °C. Com a luz apagada, a troca de ar pode esfriar o ar em {ctx.grow_name}."
    return Tip("R4", title, body, "info", _cta(title, ctx),
               ("weather", "light") + (("target_temp",) if target is not None else ()))


def r6_good_time_to_exchange(ctx: OutdoorIndoor) -> Tip | None:
    tt, trh = ctx.target_temp_c, ctx.target_humidity_rh
    if tt is None or trh is None:
        return None
    if abs(ctx.outdoor_temp_c - tt) > R6_TEMP_C or abs(ctx.outdoor_humidity_rh - trh) > R6_RH:
        return None
    title = "Bom momento para renovar o ar"
    return Tip(
        "R6", title,
        f"Lá fora está {_t(ctx.outdoor_temp_c)} °C e {_rh(ctx.outdoor_humidity_rh)} %, dentro da sua faixa. "
        f"Trocar o ar agora mexe pouco na temperatura e na umidade em {ctx.grow_name}.",
        "info", _cta(title, ctx), ("weather", "target_temp", "target_humidity"),
    )


# Prioridade: R5 > R2 > R1 = R7 > R3 = R4 > R6 (empates na ordem da lista)
WEATHER_TIP_RULES: list[WeatherTipRule] = [
    r5_night_condensation, r2_more_humid_outside, r1_drier_outside, r7_dry_air_for_seedlings,
    r3_hotter_outside, r4_cold_night_outside, r6_good_time_to_exchange,
]


# ── Fontes ───────────────────────────────────────────────────────────────────

def weather_source(*, lat: float, lon: float, label: str | None, fetched_at: datetime, grow_id: str) -> dict:
    return {
        "kind": "weather", "id": f"open-meteo:{lat:.2f},{lon:.2f}",
        "title": f"Open-Meteo · {label or 'Local do ambiente'}", "date": fetched_at.isoformat(),
        "scope": "focus", "plant_id": None, "grow_id": grow_id,
    }


def _indoor_sources(metrics: list[tuple[str, EnvironmentMeasurement]], grow_id: str) -> list[dict]:
    by_record: dict[tuple[str, str], dict] = {}
    for metric, m in metrics:
        if m.value is None or m.source_id is None:
            continue
        value = f"{_number(m.value)} °C" if metric == "temperature" else f"{_number(m.value)}%"
        key = (m.source_kind or "environment_reading", str(m.source_id))
        if key in by_record:
            by_record[key]["title"] += f" · {value}"
            continue
        label = "Sensor" if m.source == "sensor" else "Registro do ambiente"
        by_record[key] = {
            "kind": key[0], "id": key[1], "title": f"{label} · {value}",
            "date": m.observed_at.isoformat() if m.observed_at else None, "scope": "focus",
            "plant_id": None, "grow_id": grow_id,
        }
    return list(by_record.values())


def _sources(tip: Tip, ctx: OutdoorIndoor, *, weather: dict, indoor: dict[str, EnvironmentMeasurement],
             grow_id: str, phase_plant_id: str | None, light_on_at: time | None) -> list[dict]:
    sources = [weather] if "weather" in tip.uses else []
    metrics = [(m, indoor[m]) for m, use in (("temperature", "indoor_temp"), ("humidity", "indoor_humidity"))
               if use in tip.uses]
    sources += _indoor_sources(metrics, grow_id)
    base = {"date": None, "scope": "focus", "plant_id": None, "grow_id": grow_id}
    if "target_temp" in tip.uses:
        sources.append({**base, "kind": "target", "id": f"{grow_id}:target_temp_c",
                        "title": f"Faixa definida · {_t(ctx.target_temp_c)} °C"})
    if "target_humidity" in tip.uses:
        sources.append({**base, "kind": "target", "id": f"{grow_id}:target_humidity_rh",
                        "title": f"Faixa definida · {_rh(ctx.target_humidity_rh)}%"})
    if "phase" in tip.uses and phase_plant_id:
        sources.append({**base, "kind": "plant", "id": phase_plant_id, "plant_id": phase_plant_id,
                        "title": f"Fase · {PHASE_LABELS.get(ctx.phase, ctx.phase)}"})
    if "light" in tip.uses and ctx.light is not None:
        span = (f"{light_on_at:%H:%M} → {ctx.light.off_at:%H:%M}" if light_on_at and ctx.light.off_at
                else "24 h de luz")
        sources.append({**base, "kind": "light_schedule", "id": f"{grow_id}:photoperiod",
                        "title": f"Programação de luz · {span}"})
    return sources


# ── Entrada ──────────────────────────────────────────────────────────────────

def weather_tip(
    *,
    grow,
    weather,
    indoor: dict[str, EnvironmentMeasurement],
    phase: str | None,
    phase_plant_id: str | None,
    now: datetime,
    weather_src: dict,
) -> dict | None:
    """`{rule_id, title, body, severity, sources[], cta_prompt}` da regra de maior prioridade, ou None.

    `weather` e o WeatherResponse (fetched_at, utc_offset_seconds); `indoor` vem de
    latest_grow_measurements; `phase` e a fase mais avancada entre as plantas ativas.
    """
    if grow.grow_type == "outdoor" or now - weather.fetched_at > MAX_WEATHER_AGE:
        return None
    fresh = {metric: m for metric, m in indoor.items()
             if m.value is not None and m.observed_at is not None and now - m.observed_at <= MAX_INDOOR_AGE}
    if "temperature" not in fresh and "humidity" not in fresh:
        return None
    local_now = (now + timedelta(seconds=weather.utc_offset_seconds)
                 if weather.utc_offset_seconds is not None else None)
    ctx = OutdoorIndoor(
        grow_name=grow.name,
        outdoor_temp_c=weather.temperature, outdoor_humidity_rh=weather.humidity,
        outdoor_temp_max_c=weather.temp_max, outdoor_temp_min_c=weather.temp_min,
        indoor_temp_c=fresh["temperature"].value if "temperature" in fresh else None,
        indoor_humidity_rh=fresh["humidity"].value if "humidity" in fresh else None,
        target_temp_c=grow.target_temp_c, target_humidity_rh=grow.target_humidity_rh,
        phase=phase,
        light=light_state(grow.photoperiod_on_at, grow.photoperiod_light_minutes, local_now),
    )
    for rule in WEATHER_TIP_RULES:
        tip = rule(ctx)
        if tip is None:
            continue
        return {
            "rule_id": tip.rule_id, "title": tip.title, "body": tip.body, "severity": tip.severity,
            "sources": _sources(tip, ctx, weather=weather_src, indoor=indoor, grow_id=str(grow.id),
                                phase_plant_id=phase_plant_id, light_on_at=grow.photoperiod_on_at),
            "cta_prompt": tip.cta_prompt,
        }
    return None


def most_advanced_phase(plants: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """(fase, plant_id) da planta ativa mais avancada; fases fora de PHASE_RANK nao contam."""
    ranked = [(PHASE_RANK[phase], phase, plant_id) for plant_id, phase in plants if phase in PHASE_RANK]
    if not ranked:
        return None, None
    _, phase, plant_id = max(ranked, key=lambda r: r[0])
    return phase, plant_id
