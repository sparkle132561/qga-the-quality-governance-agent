"""版本绑定审批、幂等模拟处置与整改工单。"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "approval_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("analysis_run_id", sa.String(36), nullable=False),
        sa.Column("graph_thread_id", sa.String(160), nullable=False),
        sa.Column("graph_kind", sa.String(30), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("actor_id", sa.String(80), nullable=False),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("case_id", "analysis_run_id", name="uq_case_analysis_decision"),
    )
    op.create_index("ix_approval_records_case_id", "approval_records", ["case_id"])
    op.create_table(
        "simulated_effects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "approval_id", sa.String(36), sa.ForeignKey("approval_records.id"), nullable=False
        ),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("target_id", sa.String(100), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_simulated_effects_approval_id", "simulated_effects", ["approval_id"])
    op.create_table(
        "product_listings",
        sa.Column("product_id", sa.String(80), sa.ForeignKey("products.id"), primary_key=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column(
            "last_approval_id", sa.String(36), sa.ForeignKey("approval_records.id"), nullable=False
        ),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "work_orders",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=False),
        sa.Column(
            "approval_id",
            sa.String(36),
            sa.ForeignKey("approval_records.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("merchant_id", sa.String(80), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("assigned_to", sa.String(80), nullable=False),
        sa.Column("product_ids", sa.JSON(), nullable=False),
        sa.Column("batch_ids", sa.JSON(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_work_orders_case_id", "work_orders", ["case_id"])
    op.create_index("ix_work_orders_merchant_id", "work_orders", ["merchant_id"])


def downgrade():
    for table in ("work_orders", "product_listings", "simulated_effects", "approval_records"):
        op.drop_table(table)
