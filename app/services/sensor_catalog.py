"""
Catálogo dos tipos de sensor (UX item 28): tipo legível, textos de tela, ícone e medidas.

O firmware diz o módulo em `sensors_config.module_type` (solo, ambiente, ppfd, nutricao,
automacao). A API devolve o tipo legível (`sensor_kind`): air, soil, light, solution, relay
ou hub. Nenhum texto daqui fala em placa, MAC ou modelo de microcontrolador.

PPM: os sensores medem EC (mS/cm). O PPM é calculado na escala 500 (ppm = EC × 500), a
mesma do TDS do Hub e dos medidores de caneta mais comuns no Brasil.
"""
from __future__ import annotations

from dataclasses import dataclass

PPM_PER_MS_CM = 500

# module_type do firmware -> tipo legível
MODULE_KIND = {
    "ambiente": "air",
    "solo": "soil",
    "ppfd": "light",
    "nutricao": "solution",
    "automacao": "relay",
    "hub": "hub",
}
SENSOR_KINDS = ("air", "soil", "light", "solution", "relay", "hub", "co2", "ph", "root")


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str
    decimals: int
    computed: bool = False
    note: str | None = None


@dataclass(frozen=True)
class Kind:
    key: str
    label: str          # nome curto ("Solo")
    tag: str            # marcador da tela ("UMIDADE DO SUBSTRATO", em caixa normal)
    description: str    # uma frase: o que mede e onde fica
    icon: str
    metrics: tuple[Metric, ...]
    default_name: str   # nome provisório do satélite recém-descoberto


METRICS = {
    "temp_air": Metric("temp_air", "Temperatura", "°C", 1),
    "humidity_air": Metric("humidity_air", "Umidade", "%", 0),
    "vpd_kpa": Metric("vpd_kpa", "VPD", "kPa", 1, computed=True, note="calculado pela temperatura e umidade"),
    "lux": Metric("lux", "Luz", "lx", 0),
    "soil_moisture_pct": Metric("soil_moisture_pct", "Umidade do substrato", "%", 0),
    "ppm": Metric("ppm", "PPM", "ppm", 0, computed=True, note="calculado do EC (escala 500)"),
    "ec_ms_cm": Metric("ec_ms_cm", "EC", "mS/cm", 1),
    "co2_ppm": Metric("co2_ppm", "CO₂", "ppm", 0),
    "ph_solution": Metric("ph_solution", "pH", "", 1),
    "temp_root": Metric("temp_root", "Temperatura", "°C", 1),
}

KINDS: dict[str, Kind] = {
    "air": Kind("air", "Ar", "Ar · temperatura e umidade",
                "Mede temperatura e umidade do ar. O VPD é calculado pelas duas.", "temperature",
                (METRICS["temp_air"], METRICS["humidity_air"], METRICS["vpd_kpa"]), "Sensor de ar"),
    "soil": Kind("soil", "Solo", "Umidade do substrato",
                 "Fica espetado no substrato. Mostra quanto de água ainda tem no vaso.", "watering",
                 (METRICS["soil_moisture_pct"],), "Sensor de solo"),
    "light": Kind("light", "Luz", "Luz", "Mede a luz que chega na copa, em lux.", "light",
                  (METRICS["lux"],), "Sensor de luz"),
    "solution": Kind("solution", "Solução", "PPM / EC da solução",
                     "Fica no reservatório. Mede os sais dissolvidos na solução.", "fert",
                     (METRICS["ppm"], METRICS["ec_ms_cm"]), "Sensor da solução"),
    "co2": Kind("co2", "CO₂", "CO₂", "Mede o gás carbônico do ar da tenda.", "fan",
                (METRICS["co2_ppm"],), "Sensor de CO₂"),
    "ph": Kind("ph", "pH", "pH da solução", "Fica no reservatório. Mede a acidez da solução.", "watering",
               (METRICS["ph_solution"],), "Sensor de pH"),
    "root": Kind("root", "Raiz", "Temperatura da água / raiz",
                 "Mede a temperatura na zona da raiz ou da água.", "temperature",
                 (METRICS["temp_root"],), "Sensor de raiz"),
    "relay": Kind("relay", "Relé", "Relé · liga e desliga", "Liga e desliga um aparelho na tomada.", "exhaust",
                  (), "Módulo de relé"),
    "hub": Kind("hub", "Hub", "A base · fica ligada no Wi-Fi",
                "A base do ambiente: recebe os satélites e manda as leituras.", "hub", (), "Hub"),
}

# Ordem dos cartões quando um mesmo aparelho mede mais de um tipo (o Hub mede ar, luz, solo e solução)
CARD_ORDER = ("air", "soil", "light", "solution", "co2", "ph", "root")
# Campo da leitura que indica que o tipo está presente (medida direta, não calculada)
KIND_FIELDS = {
    "air": ("temp_air", "humidity_air"),
    "soil": ("soil_moisture_pct",),
    "light": ("lux",),
    "solution": ("ec_ms_cm",),
    "co2": ("co2_ppm",),
    "ph": ("ph_solution",),
    "root": ("temp_root",),
}


def ppm_from_ec(ec_ms_cm: float | None) -> int | None:
    """PPM na escala 500 a partir do EC em mS/cm."""
    if ec_ms_cm is None:
        return None
    return round(ec_ms_cm * PPM_PER_MS_CM)


def kind_from_config(module_type: str | None, sensors_config: dict | None) -> str | None:
    """Tipo legível a partir do aparelho: hub, ou o module_type do firmware em sensors_config."""
    if module_type == "hub":
        return "hub"
    raw = (sensors_config or {}).get("module_type") if isinstance(sensors_config, dict) else None
    if isinstance(raw, str):
        raw = raw.strip().lower()
        if raw in MODULE_KIND:
            return MODULE_KIND[raw]
        if raw in SENSOR_KINDS:
            return raw
    return None


def device_kind(device) -> str | None:
    """`sensor_kind` gravado, senão o deduzido do sensors_config."""
    return getattr(device, "sensor_kind", None) or kind_from_config(device.module_type, device.sensors_config)


def kinds_in_reading(reading) -> list[str]:
    """Tipos com pelo menos uma medida direta na leitura, na ordem dos cartões."""
    if reading is None:
        return []
    return [k for k in CARD_ORDER if any(getattr(reading, f, None) is not None for f in KIND_FIELDS[k])]


def metric_value(reading, key: str):
    if reading is None:
        return None
    if key == "ppm":
        return ppm_from_ec(reading.ec_ms_cm)
    return getattr(reading, key, None)


def model_key(device) -> str:
    """Chave do modelo para a foto: sensors_config.model, senão o module_type do firmware."""
    config = device.sensors_config if isinstance(device.sensors_config, dict) else {}
    model = config.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip().lower()[:40]
    if device.module_type == "hub":
        return "hub"
    raw = config.get("module_type")
    return raw.strip().lower()[:40] if isinstance(raw, str) and raw.strip() else (device_kind(device) or "unknown")
