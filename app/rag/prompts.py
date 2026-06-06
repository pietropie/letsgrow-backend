from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import GrowEvent
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading

SYSTEM_BASE = """Você é o consultor de cultivo do LetsGrow, um assistente especializado em cannabis indoor e outdoor para cultivadores brasileiros.

Seu papel é dar orientações práticas, precisas e seguras baseadas na situação real do grow do usuário.

Regras:
- Responda em português brasileiro, de forma direta e amigável
- Use os dados do grow e sensores para personalizar a resposta
- Baseie suas recomendações na base de conhecimento fornecida
- Se não tiver certeza, diga que não sabe e oriente onde buscar
- Nunca invente dados de sensores ou fatos sobre strains
- Mantenha respostas focadas e sem repetições
"""


def build_system_prompt(grow_context: str, rag_context: str) -> str:
    parts = [SYSTEM_BASE]
    if grow_context:
        parts.append(f"## Dados do Grow Atual\n{grow_context}")
    if rag_context:
        parts.append(f"## Base de Conhecimento Relevante\n{rag_context}")
    return "\n\n".join(parts)


async def build_grow_context(db: AsyncSession, grow: Grow) -> str:
    if not grow:
        return ""

    lines = [
        f"Grow: {grow.name} ({grow.grow_type})",
        f"Status: {grow.status}",
        f"Início: {grow.start_date}",
        f"Dias corridos: {(date.today() - grow.start_date).days}",
    ]

    if grow.tent_width_cm:
        lines.append(f"Tent: {grow.tent_width_cm}x{grow.tent_depth_cm}x{grow.tent_height_cm} cm")
    if grow.lighting_watts:
        lines.append(f"Iluminação: {grow.lighting_watts}W")

    # Buscar plantas do usuário vinculadas ao grow_label correspondente ao nome do grow
    result = await db.execute(
        select(Plant)
        .where(Plant.user_id == grow.user_id, Plant.is_active == True)
        .order_by(Plant.created_at.desc())
        .limit(10)
    )
    plants = result.scalars().all()

    for plant in plants:
        phase_days = ""
        if plant.flip_date and plant.current_phase == "flower":
            phase_days = f" ({(date.today() - plant.flip_date).days} dias de floração)"
        elif plant.germination_date:
            phase_days = f" ({(date.today() - plant.germination_date).days} dias desde germinação)"

        pot_info = f" | Vaso: {plant.pot_label}" if plant.pot_label else ""
        lines.append(
            f"\nPlanta: {plant.strain_name}{pot_info} | Fase: {plant.current_phase}{phase_days}"
        )

        # Last 3 events
        ev_result = await db.execute(
            select(GrowEvent)
            .where(GrowEvent.plant_id == plant.id)
            .order_by(GrowEvent.event_date.desc())
            .limit(3)
        )
        events = ev_result.scalars().all()
        for ev in events:
            ev_line = f"  - {ev.event_date.strftime('%d/%m')} {ev.event_type}"
            if ev.ppm:
                ev_line += f" | PPM: {ev.ppm}"
            if ev.ph_in:
                ev_line += f" | pH: {ev.ph_in}"
            if ev.notes:
                ev_line += f" | {ev.notes[:60]}"
            lines.append(ev_line)

    return "\n".join(lines)
