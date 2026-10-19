"""有批准来源的候选与已核验跨会话记忆。"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "verified_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("merchant_id", sa.String(80), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("product_id", sa.String(80), sa.ForeignKey("products.id"), nullable=False),
        sa.Column(
            "source_case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=False
        ),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("analysis_run_id", sa.String(36), nullable=False),
        sa.Column(
            "approval_id", sa.String(36), sa.ForeignKey("approval_records.id"), nullable=False
        ),
        sa.Column("source_id", sa.String(100), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("quote", sa.String(500), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("expires_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("proposer_id", sa.String(80), nullable=False),
        sa.Column("verifier_id", sa.String(80), nullable=True),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("merchant_id", "product_id", "status"):
        op.create_index("ix_verified_memories_" + column, "verified_memories", [column])


def downgrade():
    op.drop_table("verified_memories")
