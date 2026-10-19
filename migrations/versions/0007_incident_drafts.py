"""公告输入、模型提议与人工字段确认分离。"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "incident_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(100), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.String(60), nullable=True),
        sa.Column("engine_mode", sa.String(30), nullable=False),
        sa.Column("active_run", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("confirmed_by", sa.String(80), nullable=True),
        sa.Column("confirm_note", sa.String(500), nullable=True),
        sa.Column("event_id", sa.String(100), sa.ForeignKey("quality_events.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("incident_drafts")
