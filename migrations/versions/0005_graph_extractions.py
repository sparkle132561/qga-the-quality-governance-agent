"""保留有来源快照的文档抽取与人工核验；业务图直接投影现有关系表。"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "graph_extractions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chunk_id", sa.String(36), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=True),
        sa.Column("case_revision", sa.Integer(), nullable=True),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("location", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("engine_mode", sa.String(30), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("reviewed_by", sa.String(80), nullable=True),
        sa.Column("review_note", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("chunk_id", "status"):
        op.create_index("ix_graph_extractions_" + column, "graph_extractions", [column])


def downgrade():
    op.drop_table("graph_extractions")
