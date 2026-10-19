"""商家、案件与审计表。"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "merchants",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
    )
    op.create_table(
        "review_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("merchant_id", sa.String(80), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("workflow", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("latest_analysis", sa.JSON(), nullable=True),
        sa.Column("active_run", sa.JSON(), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_review_cases_merchant_id", "review_cases", ["merchant_id"])
    op.create_index("ix_review_cases_status", "review_cases", ["status"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=False),
        sa.Column("actor_id", sa.String(80), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_events_case_id", "audit_events", ["case_id"])


def downgrade():
    op.drop_table("audit_events")
    op.drop_table("review_cases")
    op.drop_table("merchants")
