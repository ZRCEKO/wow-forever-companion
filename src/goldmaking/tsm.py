"""Contrat TSM, normalisation CSV et téléchargement borné, sans accès à la BDD."""

import csv
import hashlib
import io
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from goldmaking.config import Settings
from goldmaking.money import MAX_COPPER

TSM_ROOT = "https://public-data.tradeskillmaster.com"
MONEY_COLUMNS = {
    "marketValue": "market_value",
    "minBuyout": "min_buyout",
    "recent": "recent",
    "historical": "historical",
    "avgSalePrice": "avg_sale_price",
}
DECIMAL_COLUMNS = {"saleRate": "sale_rate", "soldPerDay": "sold_per_day"}
KNOWN_COLUMNS = {"itemId", "name", "updatedAt", *MONEY_COLUMNS, *DECIMAL_COLUMNS}


class FeedSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    game_type: str = Field(default="forever", pattern=r"^[a-z][a-z0-9-]{0,31}$")
    region: str = Field(default="eu", pattern=r"^(eu|us|kr|tw|cn)(-[a-z0-9-]+)?$", max_length=32)
    scope: Literal["realm", "region", "commodities"]
    realm_slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")
    ruleset: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,31}$")
    faction: Literal["alliance", "horde", "neutral", "shared", "unknown"] | None = None
    dataset: Literal["synthetic", "real"] = "real"
    transport: Literal["local", "http"] = "local"

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        if self.scope == "realm":
            if self.realm_slug is None:
                raise ValueError("Un flux de marché exige realm_slug")
            if self.game_type == "forever" and (self.ruleset is None or self.faction is None):
                raise ValueError("Forever exige ruleset et faction explicites")
        elif any(x is not None for x in (self.realm_slug, self.ruleset, self.faction)):
            raise ValueError(
                "Un flux régional ne peut pas être attribué à un marché ou une faction"
            )
        if self.scope == "commodities" and self.game_type != "retail":
            raise ValueError("TSM ne publie les commodities que pour Retail")
        if self.transport == "http" and self.dataset == "synthetic":
            raise ValueError("Un téléchargement TSM ne peut pas être une fixture")
        return self

    @property
    def url(self) -> str:
        prefix = f"{TSM_ROOT}/{self.game_type}/{self.region}"
        if self.scope == "realm":
            return f"{prefix}/realm/{self.realm_slug}/items.csv"
        if self.scope == "region":
            return f"{prefix}/region/items.csv"
        return f"{prefix}/commodities.csv"

    @property
    def market_id(self) -> str:
        values = self.model_dump(exclude={"dataset", "transport"})
        return fingerprint(values)

    @property
    def source_id(self) -> str:
        return fingerprint(self.model_dump())

    def interval(self, settings: Settings) -> int:
        if self.scope == "region":
            return settings.tsm_region_interval_seconds
        if (
            self.scope == "realm"
            and self.game_type == settings.tsm_primary_game_type
            and self.region == settings.tsm_primary_region
            and self.realm_slug == settings.tsm_primary_realm_slug
        ):
            return settings.tsm_primary_interval_seconds
        return settings.tsm_market_interval_seconds

    def publication_interval(self, settings: Settings) -> int:
        """Cadence amont approximative utilisée pour la fraîcheur, pas pour le polling."""
        return (
            settings.tsm_region_publication_seconds
            if self.scope == "region"
            else settings.tsm_market_publication_seconds
        )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class FeedError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCSV:
    columns: list[str]
    unknown_columns: list[str]
    observed_at: datetime | None
    rows: list[dict[str, Any]]


def parse_integer(value: str, column: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise FeedError(f"{column}: entier positif ou nul requis")
    result = int(value)
    if result > MAX_COPPER:
        raise FeedError(f"{column}: dépassement BIGINT")
    return result


def parse_decimal(value: str, column: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise FeedError(f"{column}: décimal invalide") from exc
    if not number.is_finite() or number < 0:
        raise FeedError(f"{column}: décimal positif et fini requis")
    if (column == "saleRate" and number > 1) or number >= Decimal("1e20"):
        raise FeedError(f"{column}: hors limites")
    exponent = number.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -10:
        raise FeedError(f"{column}: précision supérieure à 10 décimales non prise en charge")
    return number


def parse_timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedError("updatedAt: ISO 8601 requis") from exc
    if result.tzinfo is None:
        raise FeedError("updatedAt: fuseau horaire obligatoire")
    return result.astimezone(UTC)


def parse_csv(content: bytes, spec: FeedSpec, settings: Settings) -> ParsedCSV:
    if len(content) > settings.max_download_bytes:
        raise FeedError("Fichier trop volumineux")
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FeedError("CSV UTF-8 attendu") from exc
    try:
        reader = csv.DictReader(io.StringIO(decoded, newline=""), strict=True)
        columns = reader.fieldnames
        if not columns or "itemId" not in columns:
            raise FeedError("Colonne itemId absente (pets et variantes non pris en charge)")
        if len(columns) > 100 or len(set(columns)) != len(columns) or any(not c for c in columns):
            raise FeedError("En-têtes invalides ou dupliqués")
        metrics = set(MONEY_COLUMNS) | set(DECIMAL_COLUMNS)
        if not metrics.intersection(columns):
            raise FeedError("Aucune métrique économique reconnue")
        wrong_scope = (
            set(DECIMAL_COLUMNS) | {"avgSalePrice"}
            if spec.scope != "region"
            else {"minBuyout", "recent"}
        )
        if wrong_scope.intersection(columns):
            raise FeedError("Colonnes incompatibles avec la portée déclarée ; vérifier le schéma")
        unknown = sorted(set(columns) - KNOWN_COLUMNS)
        seen: set[int] = set()
        times: set[datetime | None] = set()
        rows: list[dict[str, Any]] = []
        for line, raw in enumerate(reader, start=2):
            if len(rows) >= settings.max_csv_rows:
                raise FeedError("Nombre maximal de lignes dépassé")
            if None in raw or any(v is None for v in raw.values()):
                raise FeedError(f"Ligne {line}: nombre de champs incohérent")
            try:
                values = {k: v.strip() or None for k, v in raw.items()}
                item_id = parse_integer(values.get("itemId") or "", "itemId")
                if item_id == 0 or item_id in seen:
                    raise FeedError("itemId nul ou dupliqué ; aucune fusion de variantes implicite")
                seen.add(item_id)
                row: dict[str, Any] = {
                    "item_id": item_id,
                    "game_type": spec.game_type,
                    "name": values.get("name"),
                }
                if row["name"] is not None and len(row["name"]) > 500:
                    raise FeedError("Nom trop long")
                for external, internal in MONEY_COLUMNS.items():
                    value = values.get(external)
                    row[internal] = None if value is None else parse_integer(value, external)
                for external, internal in DECIMAL_COLUMNS.items():
                    value = values.get(external)
                    row[internal] = None if value is None else parse_decimal(value, external)
                stamp = values.get("updatedAt")
                times.add(None if stamp is None else parse_timestamp(stamp))
                row["extra"] = {key: values[key] for key in unknown}
                rows.append(row)
            except FeedError as exc:
                raise FeedError(f"Ligne {line}: {exc}") from exc
    except csv.Error as exc:
        raise FeedError("CSV mal formé") from exc
    if not rows:
        raise FeedError("CSV vide : refus de remplacer un marché par un snapshot vide")
    if len(times) != 1:
        raise FeedError("updatedAt doit être homogène dans un fichier")
    return ParsedCSV(list(columns), unknown, times.pop(), rows)


@dataclass(frozen=True)
class Download:
    status: int
    content: bytes
    etag: str | None
    last_modified: str | None


class DownloadError(FeedError):
    def __init__(self, message: str, status: int | None = None, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def retry_seconds(value: str | None, now: datetime) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, int((parsedate_to_datetime(value) - now).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return None


class TSMClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.client = client
        self.sleep = sleep

    def fetch(
        self, spec: FeedSpec, etag: str | None = None, last_modified: str | None = None
    ) -> Download:
        if spec.transport != "http":
            raise FeedError("Téléchargement réservé aux sources HTTP")
        headers = {"Accept": "text/csv", "User-Agent": "GoldmakingIntelligence/0.1"}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if self.client is not None:
            return self._fetch(self.client, spec.url, headers)
        with httpx.Client(
            timeout=self.settings.http_timeout_seconds, follow_redirects=False
        ) as client:
            return self._fetch(client, spec.url, headers)

    def _fetch(self, client: httpx.Client, url: str, headers: dict[str, str]) -> Download:
        for attempt in range(self.settings.http_attempts):
            try:
                with client.stream("GET", url, headers=headers) as response:
                    status = response.status_code
                    retry = retry_seconds(response.headers.get("Retry-After"), datetime.now(UTC))
                    if status == 304:
                        return Download(
                            status,
                            b"",
                            response.headers.get("ETag"),
                            response.headers.get("Last-Modified"),
                        )
                    if status != 200:
                        raise DownloadError(f"TSM HTTP {status}", status, retry)
                    raw_length = response.headers.get("Content-Length")
                    if raw_length and (
                        not raw_length.isdigit()
                        or int(raw_length) > self.settings.max_download_bytes
                    ):
                        raise FeedError("Content-Length invalide ou trop volumineux")
                    data = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(data) + len(chunk) > self.settings.max_download_bytes:
                            raise FeedError("Téléchargement décompressé trop volumineux")
                        data.extend(chunk)
                    return Download(
                        status,
                        bytes(data),
                        response.headers.get("ETag"),
                        response.headers.get("Last-Modified"),
                    )
            except (httpx.TransportError, DownloadError) as exc:
                transient = not isinstance(exc, DownloadError) or exc.status in (
                    429,
                    500,
                    502,
                    503,
                    504,
                )
                retry = exc.retry_after if isinstance(exc, DownloadError) else None
                if not transient or attempt + 1 == self.settings.http_attempts or (retry or 0) > 60:
                    if isinstance(exc, DownloadError):
                        raise
                    raise DownloadError(f"Réseau TSM : {type(exc).__name__}") from exc
                self.sleep(max(retry or 0, 2**attempt + random.random()))
        raise AssertionError("Boucle de retry épuisée")
