"""
Faixas ideais das leituras (UX item 28, decisão 4): o app sugere pela fase da planta e o
usuário ajusta no ambiente. A faixa ajustada no ambiente (`grow_ideal_ranges`) vale para
todas as fases e tem prioridade sobre a sugerida.

Fontes das faixas sugeridas (`needs_review=False` só quando o wiki do projeto dá o número):
- Temperatura, umidade e VPD por fase: Brain/wiki/concepts/vpd-deficit-pressao-vapor.md
  (tabela da Cultlight; umidade do fim da floração pelo GrowWeedEasy).
- Luz: o wiki dá PPFD por fase (iluminacao-led.md: mudas 200–300, vega 600–900, flora
  900–1200 µmol/m²/s). Lux não é PPFD: a conversão (~70 lx por µmol/m²/s em LED branco)
  depende do espectro, e o BH1750 satura em 65 535 lx. Por isso as faixas de luz estão
  marcadas para revisão.
- Umidade do substrato: o wiki avisa que o índice 0–100 do sensor capacitivo não é umidade
  volumétrica (tecnologia-sensores-esp32.md). Faixa do desenho do item 28, para revisão.
- PPM (escala 500) e EC: tabelas de fabricantes variam; valores usuais de cultivo, para revisão.

Sem fase conhecida (plantas sem registro de FASE), vale a sugestão do vegetativo
(`phase_basis="assumed"`). Colheita, secagem e cura não têm faixa sugerida.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from app.services.plant_phase import PHASE_ORDER
from app.services.sensor_catalog import PPM_PER_MS_CM

# Medidas que o usuário pode ajustar no ambiente. EC acompanha o PPM (EC = PPM / 500).
ADJUSTABLE = ("temp_air", "humidity_air", "vpd_kpa", "lux", "soil_moisture_pct", "ppm")
RANGE_METRICS = ADJUSTABLE + ("ec_ms_cm",)

# Limites aceitos ao ajustar (validação do PUT)
BOUNDS = {
    "temp_air": (0, 50),
    "humidity_air": (0, 100),
    "vpd_kpa": (0, 4),
    "lux": (0, 200_000),
    "soil_moisture_pct": (0, 100),
    "ppm": (0, 5000),
}

PHASE_GROUP = {
    "germination": "seedling",
    "seedling": "seedling",
    "veg": "veg",
    "flower": "flower",
    "late_flower": "late_flower",
    "unknown": "veg",
}
NEAR_EDGE = 0.10  # perto do limite: até 10 % da largura da faixa de uma borda

_VPD_SRC = "wiki: vpd-deficit-pressao-vapor (Cultlight)"
_GWE_SRC = "wiki: vpd-deficit-pressao-vapor (GrowWeedEasy)"
_LUX_SRC = "PPFD do wiki (iluminacao-led) × ~70 lx; depende do espectro"
_SOIL_SRC = "proposta do desenho do item 28; o índice do sensor não é umidade volumétrica"
_PPM_SRC = "valores usuais de cultivo (escala 500); variam por fabricante"


@dataclass(frozen=True)
class Band:
    min: float
    max: float
    source: str
    needs_review: bool = False


DEFAULTS: dict[str, dict[str, Band]] = {
    "seedling": {
        "temp_air": Band(24, 26, _VPD_SRC),
        # O mesmo wiki traz 60–70 % para mudas em outra fonte: a faixa precisa de revisão.
        "humidity_air": Band(75, 85, _VPD_SRC + "; outra fonte do wiki indica 60–70 %", True),
        "vpd_kpa": Band(0.4, 0.7, _VPD_SRC),
        "lux": Band(14_000, 21_000, _LUX_SRC, True),
        "soil_moisture_pct": Band(35, 60, _SOIL_SRC, True),
        "ppm": Band(100, 300, _PPM_SRC, True),
    },
    "veg": {
        "temp_air": Band(24, 28, _VPD_SRC),
        "humidity_air": Band(55, 70, _VPD_SRC),
        "vpd_kpa": Band(0.8, 1.2, _VPD_SRC),
        "lux": Band(42_000, 63_000, _LUX_SRC, True),
        "soil_moisture_pct": Band(35, 60, _SOIL_SRC, True),
        "ppm": Band(400, 800, _PPM_SRC, True),
    },
    "flower": {
        "temp_air": Band(22, 26, _VPD_SRC),
        "humidity_air": Band(45, 60, _VPD_SRC),
        "vpd_kpa": Band(1.2, 1.6, _VPD_SRC),
        "lux": Band(63_000, 84_000, _LUX_SRC + "; acima do limite de 65 535 lx do sensor", True),
        "soil_moisture_pct": Band(35, 60, _SOIL_SRC, True),
        "ppm": Band(800, 1100, _PPM_SRC, True),
    },
    "late_flower": {
        "temp_air": Band(22, 26, _VPD_SRC),
        "humidity_air": Band(40, 50, _GWE_SRC),
        "vpd_kpa": Band(1.2, 1.6, _VPD_SRC),
        "lux": Band(63_000, 84_000, _LUX_SRC + "; acima do limite de 65 535 lx do sensor", True),
        "soil_moisture_pct": Band(35, 60, _SOIL_SRC, True),
        "ppm": Band(600, 900, _PPM_SRC, True),
    },
}


@dataclass(frozen=True)
class Effective:
    metric: str
    min: float
    max: float
    source: str          # "custom" (ajustada no ambiente) | "phase" (sugerida pela fase)
    phase: str | None    # grupo de fase usado na sugestão
    needs_review: bool
    reference: str | None  # de onde veio o número sugerido


def phase_rank(phase: str | None) -> int:
    return PHASE_ORDER.index(phase) if phase in PHASE_ORDER else 0


def grow_phase(phases: list[str | None]) -> tuple[str | None, str]:
    """Fase do ambiente pela maioria das plantas ativas (empate: a mais avançada).

    Devolve (fase, base): base "plants" quando alguma planta tem fase conhecida,
    "assumed" quando nenhuma tem (vale o vegetativo) e "none" sem plantas.
    """
    known = [p for p in phases if p and p != "unknown"]
    if not known:
        return "unknown", "assumed" if phases else "none"
    counts = Counter(known)
    best = max(counts.items(), key=lambda kv: (kv[1], phase_rank(kv[0])))
    return best[0], "plants"


def suggested(metric: str, phase: str | None) -> Band | None:
    group = PHASE_GROUP.get(phase or "unknown")
    if group is None:
        return None
    if metric == "ec_ms_cm":
        band = DEFAULTS[group].get("ppm")
        if band is None:
            return None
        return Band(round(band.min / PPM_PER_MS_CM, 2), round(band.max / PPM_PER_MS_CM, 2), band.source, band.needs_review)
    return DEFAULTS[group].get(metric)


def effective(metric: str, phase: str | None, custom: dict[str, tuple[float, float]]) -> Effective | None:
    """Faixa que vale: a ajustada no ambiente, senão a sugerida pela fase."""
    group = PHASE_GROUP.get(phase or "unknown")
    key = "ppm" if metric == "ec_ms_cm" else metric
    if key in custom:
        lo, hi = custom[key]
        if metric == "ec_ms_cm":
            lo, hi = round(lo / PPM_PER_MS_CM, 2), round(hi / PPM_PER_MS_CM, 2)
        return Effective(metric, lo, hi, "custom", group, False, None)
    band = suggested(metric, phase)
    if band is None:
        return None
    return Effective(metric, band.min, band.max, "phase", group, band.needs_review, band.source)


def range_state(value: float | None, band: Effective | None) -> str:
    """ok | near | out; "none" sem valor ou sem faixa."""
    if value is None or band is None:
        return "none"
    if value < band.min or value > band.max:
        return "out"
    edge = (band.max - band.min) * NEAR_EDGE
    if value - band.min < edge or band.max - value < edge:
        return "near"
    return "ok"
