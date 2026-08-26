"""Motor unificado de regras do Bob 2.0.

Fonte unica de verdade para thresholds e logica de recomendacoes do assistente Bob.
Consumido por:
  - app/rag/prompts.py  (build_customer_context / daily brief)
  - app/services/rag.py (generate_plant_tip / bob-tip endpoint)
  - app/api/v1/bob.py   (bob-alerts)
  - app/api/v1/ai.py    (persistencia de recomendacoes)

Nao faz queries ao banco, nao chama LLM.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.models.event import GrowEvent
    from app.models.plant import Plant

# ── Constantes ────────────────────────────────────────────────────────────────

_WATERING_TYPES: frozenset[str] = frozenset({"rega", "nutrients", "watering", "feeding"})

_TRAINING_TYPES: frozenset[str] = frozenset({
    "topping", "training", "lst", "defoliation", "desfolha",
    "pruning", "supercropping", "fim_veg", "flip",
})

_FLOWER_PHASES: frozenset[str] = frozenset({
    "flower", "flowering", "flora", "floração", "floracao",
})

_VEG_PHASES: frozenset[str] = frozenset({
    "veg", "vegetative", "seedling", "vegetação", "vegetacao",
})

SubstrateKind = Literal["coco", "solo", "hidro", "organico", "desconhecido"]

TEMP_CRITICAL_C = 31.0
HUMIDITY_CRITICAL_RH = 75.0


# ── Dataclass de saida ────────────────────────────────────────────────────────

@dataclass
class BobStep:
    """Representa uma recomendacao ou alerta gerado pelo motor de regras do Bob."""

    tier: Literal["critical", "anomaly", "attention", "info"]
    action: str  # "watering" | "topping" | "lst" | "flip" | "desfolha" | "flush" | "harvest" | "mkp" | "tricomas" | "temperature" | "humidity" | "outro"
    message: str  # texto direto para exibir / injetar no contexto do Bob
    plant_id: uuid.UUID
    reason: str   # string tecnica para log/debug (ex: "days_since=8 > avg_interval+2=6")


# ── Helpers ───────────────────────────────────────────────────────────────────

def classify_substrate(raw: str | None) -> SubstrateKind:
    """Classifica o substrato por palavras-chave no texto livre.

    Ordem importa: organico/solo vivo e checado antes de solo, porque
    "solo vivo" contem "solo".
    """
    if not raw:
        return "desconhecido"
    text = raw.lower()
    if any(kw in text for kw in ("coco", "fibra de coco", "coconut")):
        return "coco"
    if any(kw in text for kw in ("hidro", "hydro", "nft", "dwc", "rdwc")):
        return "hidro"
    if any(kw in text for kw in (
        "organico", "orgânico", "solo vivo", "living soil",
        "no-till", "notill", "no till", "supersolo", "super soil",
        "living-soil",
    )):
        return "organico"
    if any(kw in text for kw in ("terra", "solo", "soil", "turfa", "light mix", "carolina")):
        return "solo"
    return "desconhecido"


def _as_date(d: date | datetime) -> date:
    """Normaliza datetime para date; date passa sem alteracao."""
    return d.date() if isinstance(d, datetime) else d  # type: ignore[return-value]


def _compute_avg_interval(watering_evs: list[GrowEvent]) -> float | None:
    """Calcula o intervalo medio em dias entre regas consecutivas.

    Retorna None quando ha menos de 2 eventos de rega.
    """
    if len(watering_evs) < 2:
        return None
    intervals: list[float] = []
    for i in range(len(watering_evs) - 1):
        d0 = _as_date(watering_evs[i].event_date)
        d1 = _as_date(watering_evs[i + 1].event_date)
        diff = (d0 - d1).days
        if diff > 0:
            intervals.append(float(diff))
    return sum(intervals) / len(intervals) if intervals else None


def resolve_env_snapshot(
    events: list[GrowEvent],
    latest_temp: float | None = None,
    latest_humidity: float | None = None,
) -> tuple[float | None, float | None]:
    """Sensor ao vivo tem prioridade; eventos do diario preenchem o que faltar."""
    temp, hum = latest_temp, latest_humidity
    for ev in events:
        if temp is None and getattr(ev, "temperature_c", None) is not None:
            temp = ev.temperature_c
        if hum is None and getattr(ev, "humidity_rh", None) is not None:
            hum = ev.humidity_rh
        if temp is not None and hum is not None:
            break
    return temp, hum


# ── Motor principal ───────────────────────────────────────────────────────────

async def compute_bob_steps(
    plant: Plant,
    events: list[GrowEvent],
    today: date,
    *,
    latest_temp: float | None = None,
    latest_humidity: float | None = None,
) -> list[BobStep]:
    """Motor unificado de regras do Bob 2.0.

    Parametros
    ----------
    plant  : instancia Plant ja carregada do banco (sem lazy-load necessario)
    events : ate 15 ultimos GrowEvent da planta, ordenados por data DESC
    today  : data de referencia (normalmente date.today())
    latest_temp / latest_humidity : leitura ao vivo de sensor, se houver

    Retorno
    -------
    Lista de BobStep em ordem de descoberta (ambiente -> watering -> veg -> flora).
    Ordenar por tier fica a cargo do chamador se necessario.
    Nao faz I/O (sem banco, sem LLM).
    """
    steps: list[BobStep] = []
    plant_name = plant.strain_name
    phase = (plant.current_phase or "").lower()
    strain_type = (plant.strain_type or "").lower()
    substrate_kind = classify_substrate(plant.substrate)

    trainings_done: set[str] = {
        ev.event_type for ev in events if ev.event_type in _TRAINING_TYPES
    }

    # ── Ambiente (sensor ou diario) ───────────────────────────────────────────
    temp_c, humidity_rh = resolve_env_snapshot(events, latest_temp, latest_humidity)
    if temp_c is not None and temp_c > TEMP_CRITICAL_C:
        steps.append(BobStep(
            tier="critical",
            action="temperature",
            message=(
                f"\U0001f6a8 {plant_name}: {temp_c:.1f}°C — acima de 31°C. "
                "Abre o exhaust, liga o ar se tiver, e nao rega com vaso quente."
            ),
            plant_id=plant.id,
            reason=f"temp_c={temp_c:.1f} > {TEMP_CRITICAL_C}",
        ))
    if (
        humidity_rh is not None
        and humidity_rh > HUMIDITY_CRITICAL_RH
        and phase in _FLOWER_PHASES
    ):
        steps.append(BobStep(
            tier="critical",
            action="humidity",
            message=(
                f"\U0001f6a8 {plant_name}: umidade {humidity_rh:.0f}% na flora. "
                "Acima de 75% o risco de botrytis dispara — desumidifica agora."
            ),
            plant_id=plant.id,
            reason=f"humidity_rh={humidity_rh:.1f} > {HUMIDITY_CRITICAL_RH} (flower)",
        ))

    # ── Regras de rega ────────────────────────────────────────────────────────
    watering_evs = [ev for ev in events if ev.event_type in _WATERING_TYPES]

    if watering_evs:
        days_since: float = (today - _as_date(watering_evs[0].event_date)).days
        avg_interval: float | None = _compute_avg_interval(watering_evs)
        days_remaining: float | None = (
            avg_interval - days_since if avg_interval is not None else None
        )

        if substrate_kind == "coco":
            _append_coco_watering(steps, plant, plant_name, days_since, days_remaining)
        elif substrate_kind == "hidro":
            _append_hidro_watering(steps, plant, plant_name, days_since)
        elif substrate_kind == "desconhecido":
            _append_unknown_watering(steps, plant, days_since, avg_interval, days_remaining)
        else:
            # solo mineral ou organico — historico manda; sem corte fixo de 7d
            _append_soil_watering(
                steps, plant, plant_name, days_since, avg_interval, days_remaining,
            )

    # ── Regras de fase vegetativa ─────────────────────────────────────────────
    if phase in _VEG_PHASES:
        germ_days: int = (
            (today - plant.germination_date).days if plant.germination_date else 0
        )

        if strain_type != "auto":
            if 14 <= germ_days <= 21 and "topping" not in trainings_done:
                steps.append(BobStep(
                    tier="attention",
                    action="topping",
                    message=(
                        f"Janela de topping aberta! ({germ_days}d de veg). "
                        "Corte acima do 5º nó — ela vai se dividir em 2 galhos, "
                        "dobrando suas colas no final."
                    ),
                    plant_id=plant.id,
                    reason=(
                        f"14 <= germ_days={germ_days} <= 21, "
                        "topping not in trainings_done"
                    ),
                ))
            elif germ_days < 14 and "topping" not in trainings_done:
                days_left = 14 - germ_days
                steps.append(BobStep(
                    tier="info",
                    action="topping",
                    message=(
                        f"Topping disponivel em ~{days_left}d (dia 14). "
                        "Aguarda ela desenvolver o 5º nó."
                    ),
                    plant_id=plant.id,
                    reason=(
                        f"germ_days={germ_days} < 14, "
                        f"topping not done, days_left={days_left}"
                    ),
                ))
            elif germ_days > 21 and "topping" not in trainings_done:
                steps.append(BobStep(
                    tier="attention",
                    action="topping",
                    message=(
                        "Janela ideal de topping já passou (dia 14-21), "
                        "mas ainda é possivel. Quer fazer agora?"
                    ),
                    plant_id=plant.id,
                    reason=(
                        f"germ_days={germ_days} > 21, "
                        "topping not done (janela vencida)"
                    ),
                ))

        if strain_type == "auto":
            if 10 <= germ_days <= 18 and "topping" not in trainings_done:
                steps.append(BobStep(
                    tier="info",
                    action="topping",
                    message=(
                        "Topping em auto é possivel agora SE ela tiver 5+ nós "
                        "E ainda sem pré-flores. Confere antes de cortar — "
                        "em auto o LST costuma ser mais seguro."
                    ),
                    plant_id=plant.id,
                    reason=(
                        f"auto: 10 <= germ_days={germ_days} <= 18, "
                        "topping not done"
                    ),
                ))

        if germ_days >= 10 and not (trainings_done & {"training", "lst"}):
            steps.append(BobStep(
                tier="attention",
                action="lst",
                message=(
                    f"LST pode comecar agora ({germ_days}d de veg). "
                    "Dobra o galho principal pra o lado com arame macio — "
                    "uniformiza a copa e aumenta producao."
                ),
                plant_id=plant.id,
                reason=(
                    f"germ_days={germ_days} >= 10, "
                    "lst/training not in trainings_done"
                ),
            ))

        if strain_type != "auto" and germ_days >= 45 and "flip" not in trainings_done:
            steps.append(BobStep(
                tier="attention",
                action="flip",
                message=(
                    f"Dia {germ_days} de veg — avalie o flip 12/12. "
                    "Como tá a estrutura? Se já tiver boa cobertura, "
                    "pode flipar quando quiser."
                ),
                plant_id=plant.id,
                reason=(
                    f"germ_days={germ_days} >= 45, "
                    "flip not done (photo/unknown strain)"
                ),
            ))

    # ── Regras de floracao ────────────────────────────────────────────────────
    if phase in _FLOWER_PHASES:
        flip_days: int = (
            (today - plant.flip_date).days if plant.flip_date else 0
        )
        harvest_days: int = plant.expected_harvest_days or 63

        if 14 <= flip_days <= 28 and not (trainings_done & {"defoliation", "desfolha"}):
            steps.append(BobStep(
                tier="attention",
                action="desfolha",
                message=(
                    f"Semana {flip_days // 7 + 1} de flora — hora da desfolha de transicao. "
                    "Retire as folhas grandes que bloqueiam luz das colas do meio."
                ),
                plant_id=plant.id,
                reason=(
                    f"14 <= flip_days={flip_days} <= 28, "
                    "defoliation not done"
                ),
            ))

        if 28 <= flip_days <= 42:
            steps.append(BobStep(
                tier="attention",
                action="mkp",
                message=(
                    f"Semana {flip_days // 7 + 1} de flora — adicione MKP e Sulfato de Magnesio. "
                    "Fosforo e potassio extra agora engordam as colas."
                ),
                plant_id=plant.id,
                reason=f"28 <= flip_days={flip_days} <= 42 (janela MKP/SulfMag)",
            ))

        if flip_days >= harvest_days - 21:
            steps.append(BobStep(
                tier="attention",
                action="tricomas",
                message=(
                    f"Dia {flip_days} de flora, previsao de colheita em {harvest_days}d. "
                    "Pega a lupa 30x — para efeito equilibrado colhe com 10-20% ambar, "
                    "para relaxar mais espera 20-30%."
                ),
                plant_id=plant.id,
                reason=(
                    f"flip_days={flip_days} >= harvest_days-21={harvest_days - 21}"
                ),
            ))

        if flip_days >= harvest_days - 10 and not any(
            ev.event_type == "flush" or getattr(ev, "is_flush", False) for ev in events
        ):
            days_left_harvest = harvest_days - flip_days
            if substrate_kind == "organico":
                steps.append(BobStep(
                    tier="info",
                    action="flush",
                    message=(
                        "Solo vivo/organico: sem flush. Deixa o microbioma trabalhar "
                        "ate a colheita — so agua se o vaso pedir."
                    ),
                    plant_id=plant.id,
                    reason=(
                        f"flip_days={flip_days} >= harvest_days-10={harvest_days - 10}, "
                        "substrate=organico — flush pulado"
                    ),
                ))
            elif substrate_kind == "desconhecido":
                steps.append(BobStep(
                    tier="attention",
                    action="flush",
                    message=(
                        "Proximo da colheita — se voce usa cultivo mineral, hora do flush "
                        "(agua pH 6.2). Em solo vivo/organico pode pular."
                    ),
                    plant_id=plant.id,
                    reason=(
                        f"flip_days={flip_days} >= harvest_days-10={harvest_days - 10}, "
                        "substrate=desconhecido, no flush event"
                    ),
                ))
            else:
                steps.append(BobStep(
                    tier="critical",
                    action="flush",
                    message=(
                        f"\U0001f6a8 Faltam ~{days_left_harvest}d para colheita — hora do flush! "
                        "Só agua limpa pH 6.2 até colher."
                    ),
                    plant_id=plant.id,
                    reason=(
                        f"flip_days={flip_days} >= harvest_days-10={harvest_days - 10}, "
                        f"substrate={substrate_kind}, no flush event"
                    ),
                ))

        if flip_days > harvest_days + 5:
            steps.append(BobStep(
                tier="critical",
                action="harvest",
                message=(
                    f"\U0001f6a8 Colheita {flip_days - harvest_days}d atrasada! "
                    "Verifique os tricomas urgente — tricomas degradam com o tempo."
                ),
                plant_id=plant.id,
                reason=(
                    f"flip_days={flip_days} > harvest_days+5={harvest_days + 5}"
                ),
            ))
        elif harvest_days - 5 <= flip_days <= harvest_days:
            steps.append(BobStep(
                tier="attention",
                action="harvest",
                message=(
                    f"Previsao de colheita em {harvest_days - flip_days}d. "
                    "Tricomas sao mais confiaveis que a data — "
                    "colhe quando os ambar chegarem no ponto."
                ),
                plant_id=plant.id,
                reason=(
                    f"harvest_days-5={harvest_days - 5} <= flip_days={flip_days} "
                    f"<= harvest_days={harvest_days}"
                ),
            ))

    return steps


# ── Ramos de rega ─────────────────────────────────────────────────────────────

def _append_coco_watering(
    steps: list[BobStep],
    plant: Plant,
    plant_name: str,
    days_since: float,
    days_remaining: float | None,
) -> None:
    if days_since > 5:
        steps.append(BobStep(
            tier="anomaly",
            action="watering",
            message=(
                "Mais de 5 dias sem rega registrada em coco — isso nao faz sentido. "
                "Voce esta regando sem anotar?"
            ),
            plant_id=plant.id,
            reason=f"days_since={int(days_since)} > 5 (coco anomaly threshold)",
        ))
    elif days_since > 3:
        steps.append(BobStep(
            tier="critical",
            action="watering",
            message=(
                f"\U0001f6a8 {plant_name}: {int(days_since)}d sem rega em coco. "
                "Coco seca em 1-2 dias — vai conferir agora."
            ),
            plant_id=plant.id,
            reason=f"days_since={int(days_since)} > 3 (coco critical threshold)",
        ))
    elif days_remaining is not None and days_remaining <= 1:
        steps.append(BobStep(
            tier="attention",
            action="watering",
            message="Rega prevista hoje pela media historica — prepare a calda.",
            plant_id=plant.id,
            reason=f"days_remaining={days_remaining:.2f} <= 1 (coco avg-based attention)",
        ))


def _append_hidro_watering(
    steps: list[BobStep],
    plant: Plant,
    plant_name: str,
    days_since: float,
) -> None:
    if days_since > 2:
        steps.append(BobStep(
            tier="anomaly",
            action="watering",
            message=(
                f"{plant_name} em hidro com {int(days_since)}d sem registro. "
                "Hidro nao fica sem solucao — voce anotou o ultimo refill?"
            ),
            plant_id=plant.id,
            reason=f"days_since={int(days_since)} > 2 (hidro anomaly)",
        ))
    elif days_since >= 1:
        steps.append(BobStep(
            tier="attention",
            action="watering",
            message=(
                "Hidro: confere nivel e EC do reservatorio hoje. "
                "Qualquer gap de registro aqui e suspeito."
            ),
            plant_id=plant.id,
            reason=f"days_since={int(days_since)} >= 1 (hidro attention)",
        ))


def _append_unknown_watering(
    steps: list[BobStep],
    plant: Plant,
    days_since: float,
    avg_interval: float | None,
    days_remaining: float | None,
) -> None:
    """Sem substrato classificado: so historico. Nunca corta em 3d/7d."""
    if days_remaining is not None and days_remaining <= 1:
        h = int(days_remaining * 24)
        steps.append(BobStep(
            tier="attention",
            action="watering",
            message=(
                f"Rega prevista em ~{h}h pela media historica — "
                "prepara a calda ja."
            ),
            plant_id=plant.id,
            reason=f"days_remaining={days_remaining:.2f} <= 1 (unknown, avg-only)",
        ))
    elif days_remaining is not None and days_remaining > 1:
        steps.append(BobStep(
            tier="info",
            action="watering",
            message=f"Proxima rega em ~{days_remaining:.1f}d pelo historico.",
            plant_id=plant.id,
            reason=f"days_remaining={days_remaining:.2f} > 1 (unknown, avg-only)",
        ))
    elif avg_interval is None and days_since >= 3:
        steps.append(BobStep(
            tier="attention",
            action="watering",
            message=(
                "3+ dias sem rega registrada. Testa o palito: "
                "se sair limpo, hora de regar."
            ),
            plant_id=plant.id,
            reason=(
                f"days_since={int(days_since)} >= 3, "
                "sem historico de rega (avg=None, substrate=desconhecido)"
            ),
        ))


def _append_soil_watering(
    steps: list[BobStep],
    plant: Plant,
    plant_name: str,
    days_since: float,
    avg_interval: float | None,
    days_remaining: float | None,
) -> None:
    if days_since > 10:
        steps.append(BobStep(
            tier="anomaly",
            action="watering",
            message=(
                "Mais de 10 dias sem rega em solo. Voce regou sem registrar? "
                "Me confirma pra eu nao ficar te enchendo de alerta."
            ),
            plant_id=plant.id,
            reason=f"days_since={int(days_since)} > 10 (solo anomaly threshold)",
        ))
    elif avg_interval is not None and days_since > avg_interval + 2:
        delay = int(days_since - avg_interval)
        steps.append(BobStep(
            tier="critical",
            action="watering",
            message=(
                f"\U0001f6a8 {plant_name}: rega atrasada {delay}d alem do historico — "
                "palito deve estar seco. Regue com pH 6.0."
            ),
            plant_id=plant.id,
            reason=(
                f"days_since={days_since:.1f} > avg_interval+2={avg_interval + 2:.1f}"
            ),
        ))
    elif days_remaining is not None and days_remaining <= 1:
        h = int(days_remaining * 24)
        steps.append(BobStep(
            tier="attention",
            action="watering",
            message=(
                f"Rega prevista em ~{h}h pela media historica — "
                "prepara a calda ja."
            ),
            plant_id=plant.id,
            reason=f"days_remaining={days_remaining:.2f} <= 1 (avg-based attention)",
        ))
    elif days_remaining is not None and days_remaining > 1:
        steps.append(BobStep(
            tier="info",
            action="watering",
            message=f"Proxima rega em ~{days_remaining:.1f}d pelo historico.",
            plant_id=plant.id,
            reason=f"days_remaining={days_remaining:.2f} > 1 (avg-based info)",
        ))
    elif avg_interval is None and days_since >= 3:
        steps.append(BobStep(
            tier="attention",
            action="watering",
            message=(
                "3+ dias sem rega registrada. Testa o palito: "
                "se sair limpo, hora de regar."
            ),
            plant_id=plant.id,
            reason=(
                f"days_since={int(days_since)} >= 3, "
                "sem historico de rega (avg=None)"
            ),
        ))


# ── Teste inline ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    import types
    from datetime import timedelta

    _today = date.today()

    def _plant(**kwargs):
        defaults = dict(
            id=uuid.uuid4(),
            strain_name="Gelato #33",
            strain_type="photo",
            current_phase="veg",
            germination_date=_today - timedelta(days=16),
            flip_date=None,
            expected_harvest_days=None,
            substrate=None,
        )
        defaults.update(kwargs)
        return types.SimpleNamespace(**defaults)

    def _ev(days_ago: int, event_type: str = "rega", **extra):
        return types.SimpleNamespace(
            event_type=event_type,
            event_date=_today - timedelta(days=days_ago),
            is_flush=False,
            temperature_c=None,
            humidity_rh=None,
            **extra,
        )

    async def _run(plant, events, **kw):
        return await compute_bob_steps(plant, events, _today, **kw)  # type: ignore[arg-type]

    # 1. Foto dia 16 veg → topping attention
    steps = asyncio.run(_run(_plant(), []))
    assert any(s.action == "topping" and s.tier == "attention" for s in steps), steps

    # 2. Substrato desconhecido + 8d sem rega, sem historico de intervalo
    #    NAO pode virar critical (corte agressivo de 7d aposentado)
    unknown = _plant(substrate="whatever")
    steps = asyncio.run(_run(unknown, [_ev(8)]))
    water = [s for s in steps if s.action == "watering"]
    assert water and all(s.tier != "critical" for s in water), water

    # 3. Solo vivo na flora proxima da colheita → flush NAO e critical
    organic = _plant(
        current_phase="flower",
        substrate="solo vivo no-till",
        germination_date=_today - timedelta(days=80),
        flip_date=_today - timedelta(days=55),
        expected_harvest_days=63,
    )
    steps = asyncio.run(_run(organic, []))
    flush = [s for s in steps if s.action == "flush"]
    assert flush and flush[0].tier == "info", flush

    # 4. Mineral na mesma janela → flush critical
    mineral = _plant(
        current_phase="flower",
        substrate="carolina soil + perlita",
        germination_date=_today - timedelta(days=80),
        flip_date=_today - timedelta(days=55),
        expected_harvest_days=63,
    )
    steps = asyncio.run(_run(mineral, []))
    flush = [s for s in steps if s.action == "flush"]
    assert flush and flush[0].tier == "critical", flush

    # 5. Temp critica
    hot = _plant()
    steps = asyncio.run(_run(hot, [], latest_temp=32.4))
    assert any(s.action == "temperature" and s.tier == "critical" for s in steps), steps

    # 6. Umidade critica so em flora
    veg_humid = asyncio.run(_run(_plant(), [], latest_humidity=80))
    assert not any(s.action == "humidity" for s in veg_humid), veg_humid
    flower_humid = asyncio.run(_run(
        _plant(current_phase="flower", flip_date=_today - timedelta(days=20)),
        [],
        latest_humidity=80,
    ))
    assert any(s.action == "humidity" and s.tier == "critical" for s in flower_humid)

    print("PASS — bob_rules self-check")
    for _s in steps[:6]:
        print(f"  [{_s.tier.upper():9}] {_s.action:<12} | {_s.message[:80]}")
