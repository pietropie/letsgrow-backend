"""Sensores (tipo legível, foto por modelo, faixas por ambiente) e relés do Hub.

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
"""
import json
import secrets

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
MODULE_KIND = {"ambiente": "air", "solo": "soil", "ppfd": "light", "nutricao": "solution", "automacao": "relay"}
SEED_MODELS = [
    ("hub", "Hub", "hub"),
    ("ambiente", "Satélite de ar", "air"),
    ("solo", "Satélite de solo", "soil"),
    ("ppfd", "Satélite de luz", "light"),
    ("nutricao", "Satélite da solução", "solution"),
    ("automacao", "Satélite de relé", "relay"),
]


def upgrade() -> None:
    # --- sensor_devices ------------------------------------------------------
    op.add_column("sensor_devices", sa.Column("sensor_kind", sa.String(20), nullable=True))
    op.add_column("sensor_devices", sa.Column("mains_voltage", sa.SmallInteger(), nullable=True))
    op.add_column("sensor_devices", sa.Column("relay_rev", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sensor_devices", sa.Column("relay_synced_rev", sa.Integer(), nullable=True))
    op.add_column("sensor_devices", sa.Column("relay_sync_error", sa.String(40), nullable=True))
    op.add_column("sensor_devices", sa.Column("clock_source", sa.String(8), nullable=True))
    op.create_check_constraint("ck_sensor_devices_mains_voltage", "sensor_devices",
                               "mains_voltage IS NULL OR mains_voltage IN (127, 220)")

    # --- grows ---------------------------------------------------------------
    op.add_column("grows", sa.Column("exhaust_watts", sa.Integer(), nullable=True))

    # --- tabelas novas ---------------------------------------------------------
    op.create_table(
        "sensor_models",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("sensor_kind", sa.String(20), nullable=True),
        sa.Column("photo_url", sa.String(500), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "grow_ideal_ranges",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("grow_id", UUID, sa.ForeignKey("grows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric", sa.String(30), nullable=False),
        sa.Column("min_value", sa.Float(), nullable=False),
        sa.Column("max_value", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("grow_id", "metric", name="uq_grow_ideal_ranges_grow_metric"),
    )
    op.create_index("ix_grow_ideal_ranges_grow_id", "grow_ideal_ranges", ["grow_id"])

    op.create_table(
        "hub_relays",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("hub_device_id", UUID, sa.ForeignKey("sensor_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("grow_id", UUID, sa.ForeignKey("grows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("n", sa.SmallInteger(), nullable=False),
        sa.Column("equip_kind", sa.String(10), nullable=True),
        sa.Column("equip_key", sa.String(40), nullable=True),
        sa.Column("mode", sa.String(10), nullable=False, server_default="manual"),
        sa.Column("schedule_on", sa.String(5), nullable=True),
        sa.Column("schedule_off", sa.String(5), nullable=True),
        sa.Column("hum_on_above", sa.Float(), nullable=True),
        sa.Column("hum_off_below", sa.Float(), nullable=True),
        sa.Column("min_on_s", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("min_off_s", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("failsafe", sa.String(3), nullable=True),
        sa.Column("state", sa.SmallInteger(), nullable=True),
        sa.Column("override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state_source", sa.String(20), nullable=True),
        sa.Column("state_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("hub_device_id", "n", name="uq_hub_relays_hub_n"),
        sa.CheckConstraint("n BETWEEN 1 AND 4", name="ck_hub_relays_n"),
    )
    op.create_index("ix_hub_relays_hub_device_id", "hub_relays", ["hub_device_id"])
    op.create_index("ix_hub_relays_grow_id", "hub_relays", ["grow_id"])
    op.create_index("uq_hub_relays_grow_equip", "hub_relays", ["grow_id", "equip_kind", "equip_key"], unique=True,
                    postgresql_where=sa.text("equip_key IS NOT NULL"))

    op.create_table(
        "relay_commands",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("req_id", UUID, nullable=False),
        sa.Column("hub_device_id", UUID, sa.ForeignKey("sensor_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("n", sa.SmallInteger(), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("requested_state", sa.SmallInteger(), nullable=True),
        sa.Column("duration_s", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("err", sa.String(30), nullable=True),
        sa.Column("confirmed_state", sa.SmallInteger(), nullable=True),
        sa.Column("override", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("req_id", name="uq_relay_commands_req_id"),
    )
    op.create_index("ix_relay_commands_hub_device_id", "relay_commands", ["hub_device_id"])

    op.create_table(
        "relay_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("hub_device_id", UUID, sa.ForeignKey("sensor_devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("grow_id", UUID, sa.ForeignKey("grows.id", ondelete="CASCADE"), nullable=True),
        sa.Column("n", sa.SmallInteger(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("hub_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hum", sa.Float(), nullable=True),
        sa.Column("equip_ref", sa.String(40), nullable=True),
        sa.Column("req_id", UUID, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("hub_device_id", "seq", name="uq_relay_events_hub_seq"),
    )
    op.create_index("ix_relay_events_hub_device_id", "relay_events", ["hub_device_id"])
    op.create_index("ix_relay_events_grow_received", "relay_events", ["grow_id", "received_at"])

    # --- dados -----------------------------------------------------------------
    bind = op.get_bind()
    models = sa.table("sensor_models", sa.column("key"), sa.column("name"), sa.column("sensor_kind"))
    op.bulk_insert(models, [{"key": k, "name": n, "sensor_kind": kind} for k, n, kind in SEED_MODELS])

    # Tipo legível dos aparelhos que já existem (Hub e module_type do firmware)
    rows = bind.execute(sa.text("SELECT id, module_type, sensors_config FROM sensor_devices")).fetchall()
    for device_id, module_type, config in rows:
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                config = {}
        raw = (config or {}).get("module_type") if isinstance(config, dict) else None
        kind = "hub" if module_type == "hub" else MODULE_KIND.get(str(raw or "").lower())
        if kind:
            bind.execute(sa.text("UPDATE sensor_devices SET sensor_kind = :k WHERE id = :i"), {"k": kind, "i": device_id})

    # id estável em cada ventilador (vínculo com a posição do relé)
    rows = bind.execute(sa.text("SELECT id, fans FROM grows WHERE fans IS NOT NULL")).fetchall()
    for grow_id, fans in rows:
        if isinstance(fans, str):
            fans = json.loads(fans)
        if not isinstance(fans, list):
            continue
        changed, used = False, set()
        for fan in fans:
            if isinstance(fan, dict) and not fan.get("id"):
                fid = secrets.token_hex(4)
                while fid in used:
                    fid = secrets.token_hex(4)
                fan["id"] = fid
                changed = True
            if isinstance(fan, dict):
                used.add(fan["id"])
        if changed:
            bind.execute(sa.text("UPDATE grows SET fans = CAST(:f AS JSONB) WHERE id = :i"),
                         {"f": json.dumps(fans), "i": grow_id})


def downgrade() -> None:
    op.drop_table("relay_events")
    op.drop_table("relay_commands")
    op.drop_index("uq_hub_relays_grow_equip", table_name="hub_relays")
    op.drop_table("hub_relays")
    op.drop_table("grow_ideal_ranges")
    op.drop_table("sensor_models")
    op.drop_column("grows", "exhaust_watts")
    op.drop_constraint("ck_sensor_devices_mains_voltage", "sensor_devices", type_="check")
    for column in ("clock_source", "relay_sync_error", "relay_synced_rev", "relay_rev", "mains_voltage", "sensor_kind"):
        op.drop_column("sensor_devices", column)
    # Os ids dos ventiladores ficam: não atrapalham o formato antigo.
