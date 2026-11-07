from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from goldmaking.db import Base, UTCDateTime


class Region(Base):
    __tablename__ = "regions"
    slug: Mapped[str] = mapped_column(String(32), primary_key=True)


class FarmingCatalog(Base):
    __tablename__ = "farming_catalogs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("dataset IN ('real', 'synthetic')", name="farming_dataset"),)


class FarmingVariant(Base):
    __tablename__ = "farming_variants"
    catalog_id: Mapped[str] = mapped_column(ForeignKey("farming_catalogs.id"), primary_key=True)
    variant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class FarmingTarget(Base):
    __tablename__ = "farming_targets"
    catalog_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    variant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["catalog_id", "variant_id"],
            ["farming_variants.catalog_id", "farming_variants.variant_id"],
        ),
        CheckConstraint("item_id > 0", name="farming_positive_item"),
    )


class Market(Base):
    __tablename__ = "markets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    game_type: Mapped[str] = mapped_column(String(32))
    region: Mapped[str] = mapped_column(ForeignKey("regions.slug"))
    scope: Mapped[str] = mapped_column(String(16))
    realm_slug: Mapped[str | None] = mapped_column(String(100))
    ruleset: Mapped[str | None] = mapped_column(String(32))
    faction: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (
        CheckConstraint("scope IN ('realm', 'region', 'commodities')", name="market_scope"),
        CheckConstraint(
            "(scope = 'realm' AND realm_slug IS NOT NULL) OR "
            "(scope != 'realm' AND realm_slug IS NULL AND ruleset IS NULL AND faction IS NULL)",
            name="market_dimensions",
        ),
        Index("ix_markets_version_region", "game_type", "region"),
    )


class Item(Base):
    __tablename__ = "items"
    game_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    __table_args__ = (CheckConstraint("item_id > 0", name="positive_item_id"),)


class DataSource(Base):
    __tablename__ = "data_sources"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id"))
    provider: Mapped[str] = mapped_column(String(32), default="tsm")
    dataset: Mapped[str] = mapped_column(String(16))
    transport: Mapped[str] = mapped_column(String(16))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    url: Mapped[str | None] = mapped_column(String(500))
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(100))
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    __table_args__ = (
        CheckConstraint("dataset IN ('synthetic', 'real')", name="source_dataset"),
        CheckConstraint("transport IN ('local', 'http')", name="source_transport"),
    )


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"))
    content_sha256: Mapped[str] = mapped_column(String(64))
    schema_sha256: Mapped[str] = mapped_column(String(64))
    columns: Mapped[list[str]] = mapped_column(JSON)
    unknown_columns: Mapped[list[str]] = mapped_column(JSON)
    parser_version: Mapped[str] = mapped_column(String(16), default="1")
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime())
    row_count: Mapped[int] = mapped_column(Integer)
    raw_gzip: Mapped[bytes] = mapped_column(LargeBinary)
    __table_args__ = (
        UniqueConstraint("source_id", "content_sha256", name="uq_snapshot_source_content"),
        Index("ix_snapshots_source_time", "source_id", "observed_at", "fetched_at"),
    )


class MarketObservation(Base):
    """Agrégat TSM au niveau itemId, jamais une variante ni une annonce individuelle."""

    __tablename__ = "market_observations"
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("market_snapshots.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    game_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(500))
    market_value: Mapped[int | None] = mapped_column(BigInteger)
    min_buyout: Mapped[int | None] = mapped_column(BigInteger)
    recent: Mapped[int | None] = mapped_column(BigInteger)
    historical: Mapped[int | None] = mapped_column(BigInteger)
    avg_sale_price: Mapped[int | None] = mapped_column(BigInteger)
    sale_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    sold_per_day: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    extra: Mapped[dict[str, str | None]] = mapped_column(JSON)
    __table_args__ = (
        ForeignKeyConstraint(["game_type", "item_id"], ["items.game_type", "items.item_id"]),
        Index("ix_observations_item", "game_type", "item_id", "snapshot_id"),
        CheckConstraint("sale_rate IS NULL OR sale_rate BETWEEN 0 AND 1", name="valid_sale_rate"),
        CheckConstraint("sold_per_day IS NULL OR sold_per_day >= 0", name="valid_sold_per_day"),
        *(
            CheckConstraint(f"{column} IS NULL OR {column} >= 0", name=f"positive_{column}")
            for column in ("market_value", "min_buyout", "recent", "historical", "avg_sale_price")
        ),
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"))
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("market_snapshots.id"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(24))
    origin: Mapped[str] = mapped_column(String(500))
    http_status: Mapped[int | None] = mapped_column(Integer)
    byte_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(1000))
    __table_args__ = (Index("ix_runs_source_started", "source_id", "started_at"),)


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    calculation_version: Mapped[str] = mapped_column(String(32))
    contract: Mapped[dict[str, Any]] = mapped_column(JSON)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (Index("ix_analysis_source_time", "source_id", "as_of"),)


class AnalysisSnapshot(Base):
    """Métadonnées figées une fois par analyse, sans copie du CSV brut."""

    __tablename__ = "analysis_snapshots"
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("market_snapshots.id"), primary_key=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)


class AnalysisEvaluation(Base):
    __tablename__ = "analysis_evaluations"
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analysis_runs.id"), primary_key=True)
    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    price_state: Mapped[str] = mapped_column(String(32))
    rank: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[dict[str, Any]] = mapped_column(JSON)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, deferred=True)
    __table_args__ = (
        Index("ix_evaluations_rank", "analysis_id", "price_state", "rank", "item_id"),
        CheckConstraint("item_id > 0", name="analysis_positive_item"),
    )
