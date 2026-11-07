import gzip
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, insert, select
from sqlalchemy.orm import Session

from goldmaking.config import Settings
from goldmaking.db import session_scope
from goldmaking.models import (
    DataSource,
    IngestionRun,
    Item,
    Market,
    MarketObservation,
    MarketSnapshot,
    Region,
)
from goldmaking.tsm import DownloadError, FeedError, FeedSpec, TSMClient, fingerprint, parse_csv

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ImportResult:
    status: str
    source_id: str
    run_id: str | None = None
    snapshot_id: str | None = None
    row_count: int | None = None


class IngestionError(FeedError):
    def __init__(self, message: str, run_id: str):
        super().__init__(message)
        self.run_id = run_id


class Ingestor:
    def __init__(self, engine: Engine, settings: Settings, client: TSMClient | None = None):
        self.engine = engine
        self.settings = settings
        self.client = client or TSMClient(settings)

    def ensure_source(self, spec: FeedSpec) -> None:
        # Un seul producteur planifié en phase 1 ; les contraintes protègent les doublons.
        with session_scope(self.engine) as session:
            if session.get(Region, spec.region) is None:
                session.add(Region(slug=spec.region))
                session.flush()
            if session.get(Market, spec.market_id) is None:
                session.add(
                    Market(id=spec.market_id, **spec.model_dump(exclude={"dataset", "transport"}))
                )
                session.flush()
            if session.get(DataSource, spec.source_id) is None:
                session.add(
                    DataSource(
                        id=spec.source_id,
                        market_id=spec.market_id,
                        dataset=spec.dataset,
                        transport=spec.transport,
                        spec=spec.model_dump(),
                        url=spec.url if spec.transport == "http" else None,
                    )
                )

    def import_file(self, path: Path, spec: FeedSpec) -> ImportResult:
        if spec.transport != "local":
            raise FeedError("L'import de fichier exige transport=local")
        return self._run(spec, path)

    def fetch(self, spec: FeedSpec) -> ImportResult:
        if spec.transport != "http":
            raise FeedError("La collecte exige transport=http")
        return self._run(spec, None)

    def _run(self, spec: FeedSpec, path: Path | None) -> ImportResult:
        self.ensure_source(spec)
        now = datetime.now(UTC)
        with session_scope(self.engine) as session:
            source = session.scalar(
                select(DataSource).where(DataSource.id == spec.source_id).with_for_update()
            )
            assert source is not None
            if path is None and source.last_checked_at and source.next_check_at:
                last_status = session.scalar(
                    select(IngestionRun.status)
                    .where(
                        IngestionRun.source_id == source.id,
                        IngestionRun.status.in_(
                            ["imported", "unchanged", "not_modified", "failed"]
                        ),
                    )
                    .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
                    .limit(1)
                )
                # Adapter les échéances existantes sans supprimer le cache ou les snapshots.
                # Un échec peut porter un Retry-After : son échéance ne doit pas être raccourcie.
                if last_status in ("imported", "unchanged", "not_modified"):
                    candidate = source.last_checked_at + timedelta(
                        seconds=spec.interval(self.settings)
                    )
                    if candidate < source.next_check_at:
                        source.next_check_at = candidate
            if path is None and source.next_check_at and source.next_check_at > now:
                return ImportResult("not_due", spec.source_id)
            run_id = str(uuid4())
            session.add(
                IngestionRun(
                    id=run_id,
                    source_id=spec.source_id,
                    started_at=now,
                    status="running",
                    origin=spec.url if path is None else path.name,
                )
            )
        try:
            with session_scope(self.engine) as session:
                source = session.scalar(
                    select(DataSource).where(DataSource.id == spec.source_id).with_for_update()
                )
                run = session.get(IngestionRun, run_id)
                assert source is not None and run is not None
                # Recontrôler après verrou : deux collectes concurrentes ne doublent pas l'appel.
                if path is None and source.next_check_at and source.next_check_at > now:
                    run.status = "not_due"
                    result = ImportResult("not_due", spec.source_id, run_id)
                else:
                    if path is None:
                        download = self.client.fetch(spec, source.etag, source.last_modified)
                        run.http_status = download.status
                        content = download.content
                        if download.status == 304:
                            exists = session.scalar(
                                select(MarketSnapshot.id)
                                .where(MarketSnapshot.source_id == source.id)
                                .limit(1)
                            )
                            if exists is None:
                                raise FeedError("304 reçu sans snapshot local ; cache incohérent")
                            source.etag = download.etag or source.etag
                            source.last_modified = download.last_modified or source.last_modified
                            run.status = "not_modified"
                            result = ImportResult("not_modified", source.id, run_id)
                        else:
                            result = self._store(session, spec, content, now, run)
                            # Valider le cache seulement après parsing et transaction réussis.
                            source.etag, source.last_modified = (
                                download.etag,
                                download.last_modified,
                            )
                        source.last_checked_at = datetime.now(UTC)
                        source.next_check_at = source.last_checked_at + timedelta(
                            seconds=spec.interval(self.settings)
                        )
                    else:
                        with path.open("rb") as file:
                            content = file.read(self.settings.max_download_bytes + 1)
                        result = self._store(session, spec, content, now, run)
                    run.byte_count = len(content)
                run.finished_at = datetime.now(UTC)
            logger.info(json.dumps({"event": "ingestion_finished", **asdict(result)}))
            return result
        except Exception as exc:
            # La transaction de données est annulée ; le journal d'échec est conservé séparément.
            message = str(exc) if isinstance(exc, FeedError) else type(exc).__name__
            with session_scope(self.engine) as session:
                run = session.get(IngestionRun, run_id)
                source = session.get(DataSource, spec.source_id)
                assert run is not None and source is not None
                run.status, run.error, run.finished_at = "failed", message[:1000], datetime.now(UTC)
                if path is None:
                    retry = exc.retry_after if isinstance(exc, DownloadError) else None
                    run.http_status = exc.status if isinstance(exc, DownloadError) else None
                    source.last_checked_at = datetime.now(UTC)
                    source.next_check_at = source.last_checked_at + timedelta(
                        seconds=max(spec.interval(self.settings), retry or 0)
                    )
            logger.error(
                json.dumps({"event": "ingestion_failed", "run_id": run_id, "error": message})
            )
            raise IngestionError(message, run_id) from exc

    def _store(
        self,
        session: Session,
        spec: FeedSpec,
        content: bytes,
        fetched_at: datetime,
        run: IngestionRun,
    ) -> ImportResult:
        if len(content) > self.settings.max_download_bytes:
            raise FeedError("Fichier trop volumineux")
        digest = hashlib.sha256(content).hexdigest()
        existing = session.scalar(
            select(MarketSnapshot).where(
                MarketSnapshot.source_id == spec.source_id, MarketSnapshot.content_sha256 == digest
            )
        )
        if existing is not None:
            run.status, run.snapshot_id = "unchanged", existing.id
            return ImportResult(
                "unchanged", spec.source_id, run.id, existing.id, existing.row_count
            )
        parsed = parse_csv(content, spec, self.settings)
        if parsed.observed_at and parsed.observed_at > fetched_at + timedelta(minutes=5):
            raise FeedError("updatedAt est dans le futur (tolérance 5 minutes)")
        snapshot_id = str(uuid4())
        snapshot = MarketSnapshot(
            id=snapshot_id,
            source_id=spec.source_id,
            content_sha256=digest,
            schema_sha256=fingerprint(parsed.columns),
            columns=parsed.columns,
            unknown_columns=parsed.unknown_columns,
            observed_at=parsed.observed_at,
            fetched_at=fetched_at,
            row_count=len(parsed.rows),
            raw_gzip=gzip.compress(content, mtime=0),
        )
        session.add(snapshot)
        session.flush()
        for start in range(0, len(parsed.rows), 500):
            batch = parsed.rows[start : start + 500]
            ids = [row["item_id"] for row in batch]
            known = set(
                session.scalars(
                    select(Item.item_id).where(
                        Item.game_type == spec.game_type, Item.item_id.in_(ids)
                    )
                )
            )
            new: list[dict[str, Any]] = [
                {"game_type": spec.game_type, "item_id": item_id}
                for item_id in ids
                if item_id not in known
            ]
            if new:
                session.execute(insert(Item), new)
            session.execute(
                insert(MarketObservation), [{"snapshot_id": snapshot_id, **row} for row in batch]
            )
        run.status, run.snapshot_id = "imported", snapshot_id
        return ImportResult("imported", spec.source_id, run.id, snapshot_id, len(parsed.rows))
