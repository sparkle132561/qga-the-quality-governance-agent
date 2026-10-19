"""整改/申诉复核绑定工单资料版本和当前复核案件。"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "work_orders", sa.Column("revision", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("work_orders", sa.Column("active_review_case_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_work_order_active_review",
        "work_orders",
        "review_cases",
        ["active_review_case_id"],
        ["id"],
    )
    op.add_column("work_orders", sa.Column("closure_note", sa.String(500), nullable=True))


def downgrade():
    op.drop_column("work_orders", "closure_note")
    op.drop_constraint("fk_work_order_active_review", "work_orders", type_="foreignkey")
    op.drop_column("work_orders", "active_review_case_id")
    op.drop_column("work_orders", "revision")
