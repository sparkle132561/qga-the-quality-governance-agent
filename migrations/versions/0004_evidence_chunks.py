"""有来源和快照哈希的检索分块，BGE-M3向量。"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "evidence_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("document_id", sa.String(100), nullable=False),
        sa.Column("rule_id", sa.String(100), sa.ForeignKey("rule_versions.id"), nullable=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=True),
        sa.Column("material_id", sa.String(36), sa.ForeignKey("materials.id"), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("location", sa.JSON(), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
    )
    for name in ("document_id", "rule_id", "case_id"):
        op.create_index("ix_evidence_chunks_" + name, "evidence_chunks", [name])


def downgrade():
    op.drop_table("evidence_chunks")
