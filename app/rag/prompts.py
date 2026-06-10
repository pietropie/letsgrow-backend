from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import GrowEvent
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading

SYSTEM_BASE = """Você é Bob, o consultor de cultivo do LetsGrow — especialista em cannabis indoor e outdoor para cultivadores brasileiros.

Seu papel é dar orientações práticas, precisas e seguras baseadas na situação real do grow do usuário.

Regras:
- Responda em português brasileiro, de forma direta e amigável
- Use os dados do grow e sensores para personalizar a resposta
- Baseie suas recomendações na base de conhecimento fornecida
- Se não tiver certeza, diga que não sabe e oriente onde buscar
- Nunca invente dados de sensores ou fatos sobre strains
- Mantenha respostas focadas e sem repetições
"""


def build_system_prompt(
    grow_context: str = "",
    rag_context: str = "",
    plant_context: str = "",
) -> str:
    parts = [SYSTEM_BASE]
    if plant_context:
        parts.append(f"## Planta em Foco\n{plant_context}")
    elif grow_context:
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


async def build_plant_context(db: AsyncSession, plant: Plant, grow: Grow | None = None) -> str:
    """Contexto detalhado de uma planta específica para o Bob.

    Inclui: genética, fase+dias, vaso/substrato, ambiente do grow (se vinculado)
    e os últimos 5 eventos com medições. Usado quando o usuário seleciona uma
    planta para fazer uma pergunta específica no chat do Bob.
    """
    today = date.today()
    lines = []

    # ── Planta ──────────────────────────────────────────────────────────────
    genetics_str = f" ({plant.genetics})" if plant.genetics else ""
    lines.append(f"Planta: {plant.strain_name}{genetics_str} | Tipo: {plant.strain_type}")

    phase_detail = plant.current_phase
    if plant.flip_date and plant.current_phase == "flower":
        flip_days = (today - plant.flip_date).days
        phase_detail = f"floração — {flip_days} dias de flora"
        if plant.expected_harvest_days:
            remaining = plant.expected_harvest_days - flip_days
            if remaining > 0:
                phase_detail += f" (~{remaining} dias para colheita)"
    elif plant.germination_date:
        germ_days = (today - plant.germination_date).days
        phase_detail = f"{plant.current_phase} — dia {germ_days} desde germinação"

    lines.append(f"Fase: {phase_detail}")

    if plant.pot_label or plant.pot_volume_liters:
        pot_parts = []
        if plant.pot_label:
            pot_parts.append(plant.pot_label)
        if plant.pot_volume_liters:
            pot_parts.append(f"{plant.pot_volume_liters}L")
        lines.append(f"Vaso: {' | '.join(pot_parts)}")

    if plant.substrate:
        lines.append(f"Substrato: {plant.substrate}")

    # ── Ambiente do grow vinculado ───────────────────────────────────────────
    if grow:
        env_parts = [f"Grow: {grow.name} ({grow.grow_type})"]
        if grow.tent_width_cm:
            env_parts.append(f"Tent {grow.tent_width_cm}×{grow.tent_depth_cm}×{grow.tent_height_cm} cm")
        if grow.lighting_watts:
            light_str = f"{grow.lighting_watts}W"
            if grow.light_type:
                light_str += f" {grow.light_type}"
            env_parts.append(light_str)
        if grow.photoperiod_hours:
            env_parts.append(f"Fotoperíodo {grow.photoperiod_hours}")
        if grow.substrate_type:
            env_parts.append(f"Substrato grow: {grow.substrate_type}")
        if grow.dehumidifier or grow.humidifier or grow.air_conditioning:
            equipment = []
            if grow.air_conditioning:
                equipment.append("ar-condicionado")
            if grow.dehumidifier:
                equipment.append("desumidificador")
            if grow.humidifier:
                equipment.append("umidificador")
            env_parts.append(f"Equipamentos: {', '.join(equipment)}")
        lines.append(" | ".join(env_parts))

    # ── Últimos 5 eventos ────────────────────────────────────────────────────
    ev_result = await db.execute(
        select(GrowEvent)
        .where(GrowEvent.plant_id == plant.id)
        .order_by(GrowEvent.event_date.desc())
        .limit(5)
    )
    events = ev_result.scalars().all()

    if events:
        lines.append("\nÚltimos registros:")
        for ev in events:
            ev_line = f"  {ev.event_date.strftime('%d/%m')} {ev.event_type}"
            measurements = []
            if ev.ppm:
                measurements.append(f"PPM {ev.ppm}")
            if ev.ph_in:
                measurements.append(f"pH entrada {ev.ph_in}")
            if ev.ph_out:
                measurements.append(f"pH saída {ev.ph_out}")
            if ev.water_volume_ml:
                measurements.append(f"{ev.water_volume_ml}ml")
            if ev.temperature_c:
                measurements.append(f"{ev.temperature_c}°C")
            if ev.humidity_rh:
                measurements.append(f"{ev.humidity_rh}% UR")
            if measurements:
                ev_line += f" | {', '.join(measurements)}"
            if ev.notes:
                ev_line += f" | {ev.notes[:80]}"
            lines.append(ev_line)

    return "\n".join(lines)
