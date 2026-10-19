"""商品处置版本防止跨案件旧批准覆盖新状态。"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "product_listings", sa.Column("version", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade():
    op.drop_column("product_listings", "version")
