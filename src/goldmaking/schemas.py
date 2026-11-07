from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from goldmaking.tsm import FeedSpec


class SourceView(BaseModel):
    id: str
    spec: FeedSpec
    url: str | None
    last_checked_at: datetime | None
    next_check_at: datetime | None


class Values(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    market_value: int | None
    min_buyout: int | None
    recent: int | None
    historical: int | None
    avg_sale_price: int | None
    sale_rate: Decimal | None
    sold_per_day: Decimal | None


class Evidence(BaseModel):
    snapshot_id: str
    content_sha256: str
    schema_sha256: str
    parser_version: str
    columns: list[str]
    unknown_columns: list[str]
    observed_at: datetime | None
    fetched_at: datetime
    age_seconds: int | None
    freshness: Literal["fresh", "stale", "unknown"]
    time_basis: Literal["provider_scan", "import_only"]


class MarketData(BaseModel):
    source: SourceView
    status: Literal["present", "missing_in_latest_snapshot", "no_snapshot"]
    name: str | None = None
    values: Values | None = None
    evidence: Evidence | None = None
    missing_metrics: list[str]
    extra: dict[str, str | None] = {}


class ItemView(BaseModel):
    item_id: int
    game_type: str
    region: str
    dataset: str
    currency: Literal["copper"] = "copper"
    granularity: Literal["base_item"] = "base_item"
    markets: list[MarketData]
    interpretation: str = (
        "Agrégats TSM. Les ventes et taux régionaux sont des estimations du fournisseur, "
        "pas des ventes observées sur ce marché. Valeurs absentes : null."
    )


class HistoryPoint(BaseModel):
    name: str | None
    values: Values
    evidence: Evidence


class HistoryView(BaseModel):
    item_id: int
    source: SourceView
    currency: Literal["copper"] = "copper"
    points: list[HistoryPoint]
    limit: int
    offset: int
    has_more: bool


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    source_id: str
    snapshot_id: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    origin: str
    http_status: int | None
    byte_count: int | None
    error: str | None
