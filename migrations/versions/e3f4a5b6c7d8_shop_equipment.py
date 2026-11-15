"""Equipamentos e insumos: catálogo, lojas permitidas, dispensas, contagem de toques e interruptores.

Revision ID: e3f4a5b6c7d8
Revises: c1d2e3f4a5b6

- shop_categories com as 10 categorias permitidas (PLAYSTORE.md §2.3);
- shop_stores (lojas permitidas por domínio), shop_products (com price_from e
  price_checked_at reservados, nulos), shop_dismissals ("Já tenho") e
  shop_click_daily (só contagem por produto e dia, sem usuário);
- app_settings ganha o interruptor geral (desligado) e um por ponto de exibição;
  "patrocinado" começa desligado.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e3f4a5b6c7d8"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None

_CATEGORIES = [
    ("light", "Luz"), ("ventilation", "Ventilação"), ("tent", "Tenda"), ("measurement", "Medição"),
    ("irrigation", "Irrigação"), ("pots", "Vasos"), ("substrate", "Substrato"), ("fertilizer", "Fertilizante"),
    ("pest_control", "Controle de pragas"), ("tools_ppe", "Ferramentas e EPI"),
]
_SWITCHES = [
    ("shop_enabled", "false"), ("shop_page_enabled", "true"), ("shop_grow_block_enabled", "true"),
    ("shop_plant_block_enabled", "true"), ("shop_account_entry_enabled", "true"), ("shop_sponsored_enabled", "false"),
]


def upgrade() -> None:
    categories = op.create_table(
        "shop_categories",
        sa.Column("key", sa.String(30), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.bulk_insert(categories, [
        {"key": key, "name": name, "sort_order": index, "is_active": True}
        for index, (key, name) in enumerate(_CATEGORIES)
    ])
    op.create_table(
        "shop_stores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("domain", sa.String(120), nullable=False, unique=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('generalist', 'garden_agro')", name="ck_shop_store_kind"),
    )
    op.create_table(
        "shop_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("category_key", sa.String(30), sa.ForeignKey("shop_categories.key"), nullable=False, index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("spec_1", sa.String(120), nullable=True),
        sa.Column("spec_2", sa.String(120), nullable=True),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shop_stores.id"), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("label", sa.String(20), nullable=False, server_default="partner"),
        sa.Column("triggers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("price_from", sa.Numeric(10, 2), nullable=True),
        sa.Column("price_checked_at", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("label IN ('partner', 'sponsored', 'none')", name="ck_shop_product_label"),
    )
    op.create_table(
        "shop_dismissals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("trigger", sa.String(40), nullable=False),
        sa.Column("grow_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("grows.id", ondelete="CASCADE"), nullable=True),
        sa.Column("plant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("plants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shop_products.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "trigger", "grow_id", "plant_id", name="uq_shop_dismissal"),
    )
    op.create_table(
        "shop_click_daily",
        sa.Column("product_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shop_products.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
    )
    for column, default in _SWITCHES:
        op.add_column("app_settings", sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.text(default)))


def downgrade() -> None:
    for column, _ in reversed(_SWITCHES):
        op.drop_column("app_settings", column)
    op.drop_table("shop_click_daily")
    op.drop_table("shop_dismissals")
    op.drop_table("shop_products")
    op.drop_table("shop_stores")
    op.drop_table("shop_categories")
