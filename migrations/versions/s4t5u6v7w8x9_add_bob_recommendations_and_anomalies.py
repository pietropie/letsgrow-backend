"""add bob_recommendations and bob_anomalies tables

Bob 2.0 — tabelas de estado persistente para o companion proativo.
bob_recommendations: recomendações pendentes de accountability (rega, topping, etc.)
bob_anomalies:       anomalias detectadas com TTL (expires_at = detected_at + 48h)

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-07-13 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "t5u6v7w8x9y0"
down_revision = "s4t5u6v7w8x9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # bob_recommendations
    # Ações sugeridas pelo Bob com prazo e flag de cumprimento.
    # action: "watering" | "topping" | "lst" | "desfolha" | "flush" | "outro"
    # ------------------------------------------------------------------
    op.create_table(
        "bob_recommendations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plant_name", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "recommended_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "followed_up",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plant_id"], ["plants.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_bob_recommendations_user_id",
        "bob_recommendations",
        ["user_id"],
    )
    op.create_index(
        "ix_bob_recommendations_plant_id",
        "bob_recommendations",
        ["plant_id"],
    )
    # Índice parcial — consulta rápida de recomendações pendentes por usuário
    op.create_index(
        "ix_bob_recommendations_followed_up",
        "bob_recommendations",
        ["user_id", "followed_up"],
        postgresql_where=sa.text("followed_up = false"),
    )

    # ------------------------------------------------------------------
    # bob_anomalies
    # Anomalias detectadas pelo Bob com TTL de 48h (expires_at).
    # anomaly_type: "watering_gap" | "ec_out_of_range" | "ph_out_of_range" | "phase_inconsistent"
    # details: JSONB livre para metadados da anomalia (valores medidos, limites, etc.)
    # ------------------------------------------------------------------
    op.create_table(
        "bob_anomalies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("anomaly_type", sa.Text(), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plant_id"], ["plants.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_bob_anomalies_user_id",
        "bob_anomalies",
        ["user_id"],
    )
    op.create_index(
        "ix_bob_anomalies_plant_id",
        "bob_anomalies",
        ["plant_id"],
    )
    # Índice parcial — consulta rápida de anomalias ativas (não resolvidas) por usuário
    op.create_index(
        "ix_bob_anomalies_active",
        "bob_anomalies",
        ["user_id", "expires_at"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    # Dropar anomalies primeiro (sem dependentes)
    op.drop_index("ix_bob_anomalies_active", table_name="bob_anomalies")
    op.drop_index("ix_bob_anomalies_plant_id", table_name="bob_anomalies")
    op.drop_index("ix_bob_anomalies_user_id", table_name="bob_anomalies")
    op.drop_table("bob_anomalies")

    # Depois dropar recommendations
    op.drop_index("ix_bob_recommendations_followed_up", table_name="bob_recommendations")
    op.drop_index("ix_bob_recommendations_plant_id", table_name="bob_recommendations")
    op.drop_index("ix_bob_recommendations_user_id", table_name="bob_recommendations")
    op.drop_table("bob_recommendations")
