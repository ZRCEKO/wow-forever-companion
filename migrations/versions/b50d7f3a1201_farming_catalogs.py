"""Archive provider-declared farming catalogues, separately from market data."""

import sqlalchemy as sa
from alembic import op

revision = "b50d7f3a1201"
down_revision = "a41c8e219f02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "farming_catalogs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("dataset IN ('real', 'synthetic')", name="farming_dataset"),
    )
    op.create_table(
        "farming_variants",
        sa.Column(
            "catalog_id", sa.String(64), sa.ForeignKey("farming_catalogs.id"), primary_key=True
        ),
        sa.Column("variant_id", sa.String(64), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "farming_targets",
        sa.Column("catalog_id", sa.String(64), primary_key=True),
        sa.Column("item_id", sa.BigInteger(), primary_key=True),
        sa.Column("variant_id", sa.String(64), primary_key=True),
        sa.ForeignKeyConstraint(
            ["catalog_id", "variant_id"],
            ["farming_variants.catalog_id", "farming_variants.variant_id"],
        ),
        sa.CheckConstraint("item_id > 0", name="farming_positive_item"),
    )


def downgrade() -> None:
    op.drop_table("farming_targets")
    op.drop_table("farming_variants")
    op.drop_table("farming_catalogs")
