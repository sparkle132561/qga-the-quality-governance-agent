"""助手运行控制独立于案件业务状态。"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "assistant_run_controls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=False),
        sa.Column("actor_id", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("thread_id", sa.String(200), nullable=False),
        sa.Column("worker_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_assistant_run_controls_case_id", "assistant_run_controls", ["case_id"])


def downgrade():
    op.drop_table("assistant_run_controls")
