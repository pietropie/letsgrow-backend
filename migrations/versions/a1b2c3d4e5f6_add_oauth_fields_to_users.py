"""add oauth fields to users

Revision ID: a1b2c3d4e5f6
Revises: 5c9d62057922
Create Date: 2026-05-07 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '5c9d62057922'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('users', 'hashed_password', nullable=True)
    op.add_column('users', sa.Column('oauth_provider', sa.String(length=20), nullable=True))
    op.add_column('users', sa.Column('oauth_id', sa.String(length=255), nullable=True))
    op.create_index('ix_users_oauth_id', 'users', ['oauth_id'])


def downgrade() -> None:
    op.drop_index('ix_users_oauth_id', table_name='users')
    op.drop_column('users', 'oauth_id')
    op.drop_column('users', 'oauth_provider')
    op.alter_column('users', 'hashed_password', nullable=False)
