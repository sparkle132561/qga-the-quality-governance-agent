"""来源、规则版本与明确标注的演示商品目录。"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sources",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("publisher", sa.String(150), nullable=False),
        sa.Column("source_kind", sa.String(40), nullable=False),
        sa.Column("published_on", sa.Date(), nullable=True),
        sa.Column("verified_on", sa.Date(), nullable=False),
        sa.Column("license_status", sa.String(40), nullable=False),
        sa.Column("redistribution_allowed", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "rule_versions",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("rule_key", sa.String(100), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(100), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_until", sa.Date(), nullable=True),
        sa.Column("content", sa.String(5000), nullable=False),
        sa.Column("requirements", sa.JSON(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("rule_key", "version", name="uq_rule_key_version"),
    )
    op.create_index("ix_rule_versions_rule_key", "rule_versions", ["rule_key"])
    op.create_table(
        "suppliers",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("merchant_id", sa.String(80), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_suppliers_merchant_id", "suppliers", ["merchant_id"])
    op.create_table(
        "products",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("merchant_id", sa.String(80), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("supplier_id", sa.String(80), sa.ForeignKey("suppliers.id"), nullable=False),
        sa.Column("brand", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_products_merchant_id", "products", ["merchant_id"])
    op.create_index("ix_products_model", "products", ["model"])
    op.create_table(
        "batches",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("product_id", sa.String(80), sa.ForeignKey("products.id"), nullable=False),
        sa.Column("number", sa.String(100), nullable=False),
        sa.Column("production_date", sa.Date(), nullable=True),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_batches_product_id", "batches", ["product_id"])
    op.create_table(
        "demo_orders",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("batch_id", sa.String(100), sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("merchant_id", sa.String(80), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_demo_orders_batch_id", "demo_orders", ["batch_id"])
    op.create_index("ix_demo_orders_merchant_id", "demo_orders", ["merchant_id"])
    op.create_table(
        "quality_events",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("source_id", sa.String(100), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("title", sa.String(250), nullable=False),
        sa.Column("scope_json", sa.JSON(), nullable=False),
        sa.Column("is_demo", sa.Boolean(), nullable=False),
    )


def downgrade():
    for table in (
        "quality_events",
        "demo_orders",
        "batches",
        "products",
        "suppliers",
        "rule_versions",
        "sources",
    ):
        op.drop_table(table)
