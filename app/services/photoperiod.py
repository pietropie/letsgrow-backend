"""
Programacao de luz do ambiente (UX item 5): hora de ligar + duracao em minutos.

- `photoperiod_light_minutes` (1-1440) e a fonte da verdade; a hora de desligar nao e guardada.
- `photoperiod_on_at` tem precisao de minuto.
- `photoperiod_hours` e derivado pelo servidor ("18/6", "24/0" ou "17h45/6h15"): e o texto que o
  Bobby e o RAG leem. Clientes antigos que mandam so "N/M" continuam funcionando.
- "custom" (programacao antiga sem horarios) fica com duracao nula e o app pede para completar.
"""
from __future__ import annotations

import re
from datetime import time

from fastapi import HTTPException, status

from app.models.grow import Grow

DAY_MINUTES = 1440
_HOURS_TEXT = re.compile(r"^(\d{1,2})/(\d{1,2})$")
LEGACY_CUSTOM = "custom"


def _part(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours}h{rest:02d}"


def format_photoperiod(light_minutes: int) -> str:
    """Texto derivado: horas inteiras viram "N/M"; com minutos, "17h45/6h15" (no maximo 11 caracteres)."""
    dark = DAY_MINUTES - light_minutes
    if light_minutes % 60 == 0:
        return f"{light_minutes // 60}/{dark // 60}"
    return f"{_part(light_minutes)}/{_part(dark)}"


def minutes_from_hours_text(text: str | None) -> int | None:
    """ "N/M" com N+M=24 e N de 1 a 24 -> N*60; qualquer outro texto -> None."""
    match = _HOURS_TEXT.match((text or "").strip())
    if not match:
        return None
    light, dark = int(match.group(1)), int(match.group(2))
    if light + dark != 24 or not 1 <= light <= 24:
        return None
    return light * 60


def minute_precision(value: time | None) -> time | None:
    return value.replace(second=0, microsecond=0, tzinfo=None) if value is not None else None


def _invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def apply_photoperiod(grow: Grow, changes: dict) -> None:
    """Aplica os campos de programacao de `changes` (ja sem os ausentes) no grow.

    Remove de `changes` as chaves tratadas aqui. Regras:
    - `photoperiod_light_minutes` (int): exige `photoperiod_on_at` no corpo ou ja salvo, exceto 1440 (24/0);
      `photoperiod_hours` passa a ser derivado e o texto enviado e ignorado.
    - `photoperiod_light_minutes: null`: volta a "nao informado" (duracao e texto nulos; a hora de ligar fica).
    - so `photoperiod_hours` (cliente antigo): "N/M" grava a duracao; "custom" fica sem duracao; null limpa os dois.
    - apagar `photoperiod_on_at` com uma duracao que depende dela (nao 24/0) e recusado.
    """
    has_minutes = "photoperiod_light_minutes" in changes
    has_hours = "photoperiod_hours" in changes
    has_on_at = "photoperiod_on_at" in changes
    minutes = changes.pop("photoperiod_light_minutes", None)
    hours_text = changes.pop("photoperiod_hours", None)
    on_at = minute_precision(changes.pop("photoperiod_on_at", None))

    if has_on_at:
        grow.photoperiod_on_at = on_at

    if has_minutes:
        if minutes is None:
            grow.photoperiod_light_minutes = None
            grow.photoperiod_hours = None
        else:
            if minutes != DAY_MINUTES and grow.photoperiod_on_at is None:
                raise _invalid("Informe photoperiod_on_at (hora de ligar) junto com a duração da luz. Só 24/0 (1440) dispensa a hora.")
            grow.photoperiod_light_minutes = minutes
            grow.photoperiod_hours = format_photoperiod(minutes)
    elif has_hours:
        text = (hours_text or "").strip()
        if not text:
            grow.photoperiod_light_minutes = None
            grow.photoperiod_hours = None
        elif text == LEGACY_CUSTOM:
            grow.photoperiod_light_minutes = None
            grow.photoperiod_hours = LEGACY_CUSTOM
        else:
            legacy = minutes_from_hours_text(text)
            if legacy is None:
                raise _invalid('photoperiod_hours aceita "N/M" com N+M=24 (ex.: 18/6). Para minutos, use photoperiod_light_minutes.')
            grow.photoperiod_light_minutes = legacy
            grow.photoperiod_hours = format_photoperiod(legacy)

    if has_on_at and on_at is None and not has_minutes and grow.photoperiod_light_minutes not in (None, DAY_MINUTES):
        raise _invalid("A duração da luz depende da hora de ligar. Para apagar a hora, envie também photoperiod_light_minutes: null.")
