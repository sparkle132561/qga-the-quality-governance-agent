"""人工确认的来源实体身份关联；节点失效后关联不参与路径。"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "graph_entity_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subject", sa.String(160), nullable=False),
        sa.Column("object", sa.String(160), nullable=False),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("review_cases.id"), nullable=True),
        sa.Column("reviewed_by", sa.String(80), nullable=False),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("graph_entity_links")
