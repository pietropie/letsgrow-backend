"""add ai_config table — runtime-editable LLM/embedding provider config (admin panel)

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-07 21:00:00.000001

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# UUID fixo da linha de configuração seed — gerado uma única vez aqui para que
# upgrade/downgrade sejam determinísticos e idempotentes.
_SEED_ID = '7c1d9a2e-4b3f-4a5d-8e6f-1a2b3c4d5e6f'


def upgrade() -> None:
    op.create_table(
        'ai_config',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('provider', sa.String(length=20), nullable=False, server_default='gemini'),
        sa.Column('chat_model', sa.String(length=100), nullable=False),
        sa.Column('temperature', sa.Float(), nullable=False, server_default='0.3'),
        sa.Column('embedding_provider', sa.String(length=20), nullable=False, server_default='gemini'),
        sa.Column('embedding_model', sa.String(length=100), nullable=False),
        sa.Column('embedding_dimensions', sa.Integer(), nullable=False, server_default='768'),
        sa.Column('updated_by', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )

    # Seed com os valores que já estão em produção hoje (ver app/config.py),
    # para que o app continue funcionando exatamente igual no primeiro boot
    # após o deploy — a partir daí, tudo é editável via /admin/ai-panel sem
    # precisar de nova migration.
    op.execute(
        sa.text(
            """
            INSERT INTO ai_config
                (id, provider, chat_model, temperature,
                 embedding_provider, embedding_model, embedding_dimensions, updated_by)
            VALUES
                (:id, 'gemini', 'gemini-2.0-flash-exp', 0.3,
                 'gemini', 'models/gemini-embedding-001', 768, 'migration-seed')
            """
        ).bindparams(sa.bindparam('id', value=_SEED_ID, type_=UUID(as_uuid=True)))
    )


def downgrade() -> None:
    op.drop_table('ai_config')
