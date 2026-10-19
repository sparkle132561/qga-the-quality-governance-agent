"""材料、来源映射与解析状态。"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "materials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=False),
        sa.Column("filename", sa.String(200), nullable=False),
        sa.Column("stored_name", sa.String(80), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("case_revision", sa.Integer(), nullable=False),
        sa.Column("parsed", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_materials_case_id", "materials", ["case_id"])


def downgrade():
    op.drop_table("materials")
