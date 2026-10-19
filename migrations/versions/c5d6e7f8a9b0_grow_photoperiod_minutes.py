"""Grow: light duration in minutes (photoperiod_light_minutes).

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9

photoperiod_hours is widened from 10 to 16 characters: the derived text reaches 11
("10h05/13h55").

Backfill: "N/M" (N+M=24, N from 1 to 24) -> N*60 minutes. "custom" and any other text keep a null
duration and stay as they are; the app asks the user to complete them.
"""
import re

from alembic import op
import sqlalchemy as sa

revision = "c5d6e7f8a9b0"
down_revision = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None

_HOURS_TEXT = re.compile(r"^(\d{1,2})/(\d{1,2})$")


def minutes_from_hours_text(text):
    """Self-contained copy of the rule (migrations must not depend on app code that may change)."""
    match = _HOURS_TEXT.match((text or "").strip())
    if not match:
        return None
    light, dark = int(match.group(1)), int(match.group(2))
    if light + dark != 24 or not 1 <= light <= 24:
        return None
    return light * 60


def backfill(conn) -> int:
    grows = sa.table(
        "grows",
        sa.column("id"),
        sa.column("photoperiod_hours", sa.String),
        sa.column("photoperiod_light_minutes", sa.Integer),
    )
    rows = conn.execute(sa.select(grows.c.id, grows.c.photoperiod_hours).where(grows.c.photoperiod_hours.is_not(None))).all()
    updated = 0
    for grow_id, text in rows:
        minutes = minutes_from_hours_text(text)
        if minutes is not None:
            conn.execute(grows.update().where(grows.c.id == grow_id).values(photoperiod_light_minutes=minutes))
            updated += 1
    return updated


def upgrade():
    op.alter_column("grows", "photoperiod_hours", type_=sa.String(16), existing_type=sa.String(10), existing_nullable=True)
    op.add_column("grows", sa.Column("photoperiod_light_minutes", sa.Integer, nullable=True))
    op.create_check_constraint(
        "ck_grows_photoperiod_light_minutes", "grows",
        "photoperiod_light_minutes IS NULL OR photoperiod_light_minutes BETWEEN 1 AND 1440",
    )
    if not op.get_context().as_sql:
        backfill(op.get_bind())


def downgrade():
    op.drop_constraint("ck_grows_photoperiod_light_minutes", "grows", type_="check")
    op.drop_column("grows", "photoperiod_light_minutes")
    # Texts longer than 10 characters (with minutes) go back to "custom" before narrowing.
    op.execute("UPDATE grows SET photoperiod_hours = 'custom' WHERE length(photoperiod_hours) > 10")
    op.alter_column("grows", "photoperiod_hours", type_=sa.String(10), existing_type=sa.String(16), existing_nullable=True)
