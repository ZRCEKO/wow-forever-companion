"""Persist local-discount-v1 analyses and shared provenance.

Revision ID: a41c8e219f02
Revises: 82ce2415b883
"""

import sqlalchemy as sa
from alembic import op

revision = "a41c8e219f02"
down_revision = "82ce2415b883"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(64), sa.ForeignKey("data_sources.id"), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calculation_version", sa.String(32), nullable=False),
        sa.Column("contract", sa.JSON(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
    )
    op.create_index("ix_analysis_source_time", "analysis_runs", ["source_id", "as_of"])
    op.create_table(
        "analysis_snapshots",
        sa.Column(
            "analysis_id", sa.String(36), sa.ForeignKey("analysis_runs.id"), primary_key=True
        ),
        sa.Column(
            "snapshot_id", sa.String(36), sa.ForeignKey("market_snapshots.id"), primary_key=True
        ),
        sa.Column("evidence", sa.JSON(), nullable=False),
    )
    op.create_table(
        "analysis_evaluations",
        sa.Column(
            "analysis_id", sa.String(36), sa.ForeignKey("analysis_runs.id"), primary_key=True
        ),
        sa.Column("item_id", sa.BigInteger(), primary_key=True),
        sa.Column("price_state", sa.String(32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.CheckConstraint("item_id > 0", name="analysis_positive_item"),
    )
    op.create_index(
        "ix_evaluations_rank",
        "analysis_evaluations",
        ["analysis_id", "price_state", "rank", "item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluations_rank", table_name="analysis_evaluations")
    op.drop_table("analysis_evaluations")
    op.drop_table("analysis_snapshots")
    op.drop_index("ix_analysis_source_time", table_name="analysis_runs")
    op.drop_table("analysis_runs")
