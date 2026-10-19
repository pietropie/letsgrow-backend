"""
Fase da planta a partir dos registros FASE do diario.

GERMINACAO, MUDA, VEGETATIVA, FLIP 12/12 e COLHEITA mudam `current_phase`. A fase so avanca:
um registro antigo (por exemplo, uma germinacao lancada depois do flip) nao faz a planta voltar.
Para corrigir uma fase errada, o usuario edita "Fase" em "Informacoes da planta" (PATCH /plants).
"""
from datetime import datetime

from app.models.plant import Plant

# germination | seedling | veg | flower | late_flower | harvest | drying | curing | done
PHASE_ORDER = ["unknown", "germination", "seedling", "veg", "flower", "late_flower", "harvest", "drying", "curing", "done"]

# event_type do registro -> fase resultante
PHASE_EVENTS = {
    "germination": "germination",
    "seedling": "seedling",
    "veg": "veg",
    "flip": "flower",
    "harvest": "harvest",
}


def _rank(phase: str | None) -> int:
    return PHASE_ORDER.index(phase) if phase in PHASE_ORDER else 0


def apply_phase_event(plant: Plant, event_type: str, event_date: datetime) -> bool:
    """Atualiza fase e datas da planta para um registro FASE. Retorna True se a fase mudou."""
    target = PHASE_EVENTS.get(event_type)
    if target is None:
        return False
    day = event_date.date()
    if event_type == "germination" and plant.germination_date is None:
        plant.germination_date = day
    if _rank(target) < _rank(plant.current_phase):
        return False
    changed = plant.current_phase != target
    plant.current_phase = target
    if event_type == "flip" and (changed or plant.flip_date is None):
        plant.flip_date = day
    if event_type == "harvest":
        # Mantem o comportamento anterior: a colheita encerra o acompanhamento ativo.
        plant.harvest_date = day
        plant.is_active = False
    return changed
