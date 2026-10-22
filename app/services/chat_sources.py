"""
Fontes de uma resposta do Bobby: so o que foi de fato injetado no prompt.

Os construtores de contexto (app/rag/prompts.py) chamam `note_event` e
`note_environment_reading` ao formatar cada registro; o chat registra os
trechos da base (`note_knowledge`) e as fotos enviadas (`note_photo`). Fora de
um `collecting(...)` nada e registrado, entao o resto do app nao muda.

Cada fonte: {kind, id, title, date, scope, plant_id?, grow_id?}
- kind: event | environment_reading | knowledge | photo
- scope: focus (planta/ambiente da conversa), account (contexto geral da conta),
  knowledge (base de conhecimento) ou message (foto enviada nesta mensagem).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import PurePosixPath

_collector: ContextVar["SourceCollector | None"] = ContextVar("bobby_sources", default=None)
_scope: ContextVar[str] = ContextVar("bobby_sources_scope", default="account")

_SCOPE_RANK = {"message": 0, "focus": 1, "knowledge": 2, "account": 3}

EVENT_LABELS = {
    "watering": "Rega", "rega": "Rega", "feeding": "Nutrição", "nutrients": "Nutrição", "note": "Nota",
    "observation": "Observação", "environment": "Ambiente", "flip": "Virada para flora", "training": "Treino",
    "topping": "Topping", "lst": "LST", "defoliation": "Desfolha", "desfolha": "Desfolha",
    "trichome_reading": "Tricomas", "symptom": "Sintoma", "harvest": "Colheita", "transplant": "Transplante",
    "pruning": "Poda", "drying": "Secagem", "curing": "Cura", "germination": "Germinação",
    "seedling": "Muda", "veg": "Vegetativa", "flush": "Flush",
}


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _number(value: float) -> str:
    return (f"{value:.1f}".rstrip("0").rstrip(".")).replace(".", ",")


class SourceCollector:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def add(self, item: dict) -> None:
        key = (item["kind"], str(item["id"]))
        current = self._items.get(key)
        if current is None or _SCOPE_RANK[item["scope"]] < _SCOPE_RANK[current["scope"]]:
            self._items[key] = item

    @property
    def items(self) -> list[dict]:
        # Mais relevantes primeiro: foto enviada, foco da conversa, base, conta.
        return sorted(self._items.values(), key=lambda i: (_SCOPE_RANK[i["scope"]], -(_ts(i["date"]))))

    @property
    def record_count(self) -> int:
        return sum(1 for i in self._items.values() if i["kind"] in ("event", "environment_reading"))


def _ts(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp() if "T" in value else datetime.fromisoformat(value + "T00:00:00").timestamp()
    except ValueError:
        return 0.0


@contextmanager
def collecting(collector: SourceCollector):
    token = _collector.set(collector)
    try:
        yield collector
    finally:
        _collector.reset(token)


@contextmanager
def scope(name: str):
    token = _scope.set(name)
    try:
        yield
    finally:
        _scope.reset(token)


def note_event(ev) -> None:
    collector = _collector.get()
    if collector is None:
        return
    detail = None
    if getattr(ev, "ph_in", None) is not None:
        detail = f"pH {_number(ev.ph_in)}"
    elif getattr(ev, "ppm", None) is not None:
        detail = f"PPM {_number(ev.ppm)}"
    elif getattr(ev, "water_volume_ml", None) is not None:
        detail = f"{ev.water_volume_ml:.0f} ml"
    elif getattr(ev, "temperature_c", None) is not None:
        detail = f"{_number(ev.temperature_c)} °C"
    label = EVENT_LABELS.get(ev.event_type, "Registro do diário")
    collector.add({
        "kind": "event", "id": str(ev.id), "title": f"{label} · {detail}" if detail else label,
        "date": _iso(ev.event_date), "scope": _scope.get(), "plant_id": str(ev.plant_id), "grow_id": None,
    })


def note_environment_reading(reading) -> None:
    collector = _collector.get()
    if collector is None:
        return
    parts = []
    if reading.temperature_c is not None:
        parts.append(f"{_number(reading.temperature_c)} °C")
    if reading.humidity_rh is not None:
        parts.append(f"{_number(reading.humidity_rh)}%")
    if getattr(reading, "kind", "manual") == "exhaust_power":
        title = reading.notes or "Potência do exaustor"
    else:
        title = "Registro do ambiente" + (f" · {' · '.join(parts)}" if parts else "")
    collector.add({
        "kind": "environment_reading", "id": str(reading.id), "title": title, "date": _iso(reading.observed_at),
        "scope": _scope.get(), "plant_id": None, "grow_id": str(reading.grow_id),
    })


def knowledge_title(chunk) -> str:
    meta = chunk.chunk_metadata or {}
    for key in ("title", "heading", "section"):
        if isinstance(meta.get(key), str) and meta[key].strip():
            return meta[key].strip()[:120]
    stem = PurePosixPath(str(chunk.source_file).replace("\\", "/")).stem
    return stem.replace("-", " ").replace("_", " ").strip().capitalize() or "Base de conhecimento"


def note_knowledge(chunk) -> None:
    collector = _collector.get()
    if collector is None:
        return
    collector.add({
        "kind": "knowledge", "id": str(chunk.id), "title": f"Guia · {knowledge_title(chunk)}", "date": None,
        "scope": "knowledge", "plant_id": None, "grow_id": None,
    })


def note_photo(key: str | None, index: int) -> None:
    collector = _collector.get()
    if collector is None:
        return
    collector.add({
        "kind": "photo", "id": key or f"inline-{index}", "title": "Foto enviada", "date": None,
        "scope": "message", "plant_id": None, "grow_id": None,
    })
