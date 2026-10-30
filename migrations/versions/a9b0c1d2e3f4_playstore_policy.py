"""Google Play policy: "offensive" feedback reason and the 18+ confirmation.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3

- bob_feedback.reason accepts "offensive" (report of offensive or inappropriate content,
  required by the Play AI-generated content policy).
- users gains age_confirmed_at: when the person confirmed being 18 or older. Existing
  accounts stay null until they confirm in the app.
"""
from alembic import op
import sqlalchemy as sa

revision = "a9b0c1d2e3f4"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None

_REASONS = "'wrong_record', 'missing_context', 'not_helpful', 'other'"


def upgrade() -> None:
    op.drop_constraint("ck_bob_feedback_reason", "bob_feedback", type_="check")
    op.create_check_constraint(
        "ck_bob_feedback_reason", "bob_feedback", f"reason IS NULL OR reason IN ({_REASONS}, 'offensive')",
    )
    op.add_column("users", sa.Column("age_confirmed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "age_confirmed_at")
    op.execute("UPDATE bob_feedback SET reason = 'other' WHERE reason = 'offensive'")
    op.drop_constraint("ck_bob_feedback_reason", "bob_feedback", type_="check")
    op.create_check_constraint(
        "ck_bob_feedback_reason", "bob_feedback", f"reason IS NULL OR reason IN ({_REASONS})",
    )
