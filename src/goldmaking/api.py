from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, defer

from goldmaking.analysis import primary_source_id, run_view
from goldmaking.config import Settings
from goldmaking.db import make_engine
from goldmaking.indicators import ItemIndicators, read_indicators
from goldmaking.models import (
    AnalysisEvaluation,
    AnalysisRun,
    AnalysisSnapshot,
    DataSource,
    FarmingCatalog,
    FarmingTarget,
    FarmingVariant,
    IngestionRun,
    Market,
    MarketObservation,
    MarketSnapshot,
)
from goldmaking.schemas import (
    Evidence,
    HistoryPoint,
    HistoryView,
    ItemView,
    MarketData,
    RunView,
    SourceView,
    Values,
)
from goldmaking.tsm import FeedSpec


def source_view(source: DataSource) -> SourceView:
    return SourceView(
        id=source.id,
        spec=FeedSpec.model_validate(source.spec),
        url=source.url,
        last_checked_at=source.last_checked_at,
        next_check_at=source.next_check_at,
    )


def evidence(snapshot: MarketSnapshot, spec: FeedSpec, settings: Settings) -> Evidence:
    age = (
        None
        if snapshot.observed_at is None
        else max(0, int((datetime.now(UTC) - snapshot.observed_at).total_seconds()))
    )
    return Evidence(
        snapshot_id=snapshot.id,
        content_sha256=snapshot.content_sha256,
        schema_sha256=snapshot.schema_sha256,
        parser_version=snapshot.parser_version,
        columns=snapshot.columns,
        unknown_columns=snapshot.unknown_columns,
        observed_at=snapshot.observed_at,
        fetched_at=snapshot.fetched_at,
        age_seconds=age,
        freshness="unknown"
        if age is None
        else ("fresh" if age <= 2 * spec.publication_interval(settings) else "stale"),
        time_basis="import_only" if age is None else "provider_scan",
    )


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings()
    owns_engine = engine is None
    engine = engine or make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_engine:
            engine.dispose()

    app = FastAPI(
        title="Goldmaking Intelligence",
        version="0.1.0",
        lifespan=lifespan,
        description="API locale en lecture seule. Monnaie : cuivre entier. "
        "Séparer données réelles/fictives et marchés/statistiques régionales.",
    )

    def db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    db_dep = Depends(db)

    @app.get("/v1/farming/catalogs", tags=["Farming"])
    def farming_catalogs(
        dataset: Literal["real", "synthetic"] = "real",
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
        session: Session = db_dep,
    ) -> dict[str, Any]:
        rows = list(
            session.scalars(
                select(FarmingCatalog)
                .where(FarmingCatalog.dataset == dataset)
                .order_by(FarmingCatalog.created_at.desc(), FarmingCatalog.id)
                .limit(limit + 1)
                .offset(offset)
            )
        )
        return {
            "dataset": dataset,
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
            "catalogs": [
                {
                    "catalog_id": row.id,
                    "created_at": row.created_at,
                    "profile": row.metadata_json["profile"],
                    "schema_version": row.metadata_json["schema_version"],
                    "variant_count": row.metadata_json["variant_count"],
                }
                for row in rows[:limit]
            ],
        }

    @app.get("/v1/farming/catalogs/{catalog_id}", tags=["Farming"])
    def farming_catalog(
        catalog_id: Annotated[str, Path(pattern="^[a-f0-9]{64}$")],
        dataset: Literal["real", "synthetic"] = "real",
        session: Session = db_dep,
    ) -> dict[str, Any]:
        row = session.get(FarmingCatalog, catalog_id)
        if row is None or row.dataset != dataset:
            raise HTTPException(404, "Catalogue absent dans ce dataset")
        return {"catalog_id": row.id, "created_at": row.created_at, "metadata": row.metadata_json}

    @app.get("/v1/farming/catalogs/{catalog_id}/items/{item_id}", tags=["Farming"])
    def farming_item(
        catalog_id: Annotated[str, Path(pattern="^[a-f0-9]{64}$")],
        item_id: Annotated[int, Path(gt=0, le=2147483647)],
        dataset: Literal["real", "synthetic"] = "real",
        limit: Annotated[int, Query(ge=1, le=20)] = 20,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
        session: Session = db_dep,
    ) -> dict[str, Any]:
        row = session.get(FarmingCatalog, catalog_id)
        if row is None or row.dataset != dataset:
            raise HTTPException(404, "Catalogue absent dans ce dataset")
        variants = list(
            session.scalars(
                select(FarmingVariant)
                .join(
                    FarmingTarget,
                    (FarmingTarget.catalog_id == FarmingVariant.catalog_id)
                    & (FarmingTarget.variant_id == FarmingVariant.variant_id),
                )
                .where(FarmingVariant.catalog_id == catalog_id, FarmingTarget.item_id == item_id)
                .order_by(FarmingVariant.variant_id)
                .limit(limit + 1)
                .offset(offset)
            )
        )
        return {
            "catalog_id": catalog_id,
            "dataset": dataset,
            "item_id": item_id,
            "limit": limit,
            "offset": offset,
            "has_more": len(variants) > limit,
            "pagination_unit": "documentary_variant",
            "variants": [v.payload for v in variants[:limit]],
        }

    @app.get("/health", tags=["Santé"])
    def health(session: Session = db_dep) -> dict[str, str]:
        try:
            session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            session.execute(select(DataSource.id).limit(1))
            session.execute(select(AnalysisRun.id).limit(1))
            session.execute(select(FarmingCatalog.id).limit(1))
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Base indisponible ou migrations absentes") from exc
        return {"status": "ok"}

    @app.get("/v1/sources", response_model=list[SourceView], tags=["Sources"])
    def sources(
        session: Session = db_dep,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    ) -> list[SourceView]:
        return [
            source_view(s)
            for s in session.scalars(
                select(DataSource).order_by(DataSource.id).limit(limit).offset(offset)
            )
        ]

    @app.get("/v1/ingestion-runs", response_model=list[RunView], tags=["Qualité"])
    def runs(
        session: Session = db_dep, limit: Annotated[int, Query(ge=1, le=100)] = 50
    ) -> list[RunView]:
        return [
            RunView.model_validate(r)
            for r in session.scalars(
                select(IngestionRun)
                .order_by(IngestionRun.started_at.desc(), IngestionRun.id)
                .limit(limit)
            )
        ]

    def require_analysis(analysis_id: str, session: Session) -> AnalysisRun:
        run = session.get(AnalysisRun, analysis_id)
        if run is None:
            raise HTTPException(404, "Analyse inconnue")
        return run

    @app.get("/v1/analyses", tags=["Analyses"])
    def analyses(
        session: Session = db_dep,
        source_id: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    ) -> dict[str, Any]:
        rows = list(
            session.scalars(
                select(AnalysisRun)
                .where(AnalysisRun.source_id == (source_id or primary_source_id()))
                .order_by(AnalysisRun.created_at.desc(), AnalysisRun.id)
                .limit(limit + 1)
                .offset(offset)
            )
        )
        return {
            "items": [run_view(r) for r in rows[:limit]],
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
        }

    @app.get("/v1/analyses/{analysis_id}", tags=["Analyses"])
    def analysis(analysis_id: str, session: Session = db_dep) -> dict[str, Any]:
        return run_view(require_analysis(analysis_id, session))

    @app.get("/v1/analyses/{analysis_id}/evaluations", tags=["Analyses"])
    def evaluations(
        analysis_id: str,
        session: Session = db_dep,
        price_state: Literal["not_calculable", "no_signal", "candidate_to_review"] | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    ) -> dict[str, Any]:
        require_analysis(analysis_id, session)
        stmt = select(AnalysisEvaluation).where(AnalysisEvaluation.analysis_id == analysis_id)
        if price_state is not None:
            stmt = stmt.where(AnalysisEvaluation.price_state == price_state)
        rows = list(
            session.scalars(
                stmt.order_by(
                    AnalysisEvaluation.rank.is_(None),
                    AnalysisEvaluation.rank,
                    AnalysisEvaluation.item_id,
                )
                .limit(limit + 1)
                .offset(offset)
            )
        )
        return {
            "items": [{**r.result, "rank": r.rank} for r in rows[:limit]],
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
        }

    @app.get("/v1/analyses/{analysis_id}/evaluations/{item_id}", tags=["Analyses"])
    def evaluation(
        analysis_id: str,
        item_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
        session: Session = db_dep,
    ) -> dict[str, Any]:
        row = session.get(AnalysisEvaluation, (analysis_id, item_id))
        if row is None:
            raise HTTPException(404, "Évaluation inconnue")
        return {**row.result, "rank": row.rank}

    @app.get("/v1/analyses/{analysis_id}/evaluations/{item_id}/evidence", tags=["Analyses"])
    def analysis_evidence(
        analysis_id: str,
        item_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
        session: Session = db_dep,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
    ) -> dict[str, Any]:
        row = session.get(AnalysisEvaluation, (analysis_id, item_id))
        if row is None:
            raise HTTPException(404, "Évaluation inconnue")
        refs = row.evidence_refs
        page = refs[offset : offset + limit]
        metadata = {
            r.snapshot_id: r.evidence
            for r in session.scalars(
                select(AnalysisSnapshot).where(
                    AnalysisSnapshot.analysis_id == analysis_id,
                    AnalysisSnapshot.snapshot_id.in_([ref["snapshot_id"] for ref in page]),
                )
            )
        }
        return {
            "items": [{**metadata[ref["snapshot_id"]], **ref} for ref in page],
            "limit": limit,
            "offset": offset,
            "total": len(refs),
            "has_more": offset + limit < len(refs),
        }

    @app.get("/v1/items/{item_id}/indicators", response_model=ItemIndicators, tags=["Indicateurs"])
    def item_indicators(
        item_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
        session: Session = db_dep,
        source_id: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
        as_of: datetime | None = None,
    ) -> ItemIndicators:
        if session.get(DataSource, source_id or primary_source_id()) is None:
            raise HTTPException(404, "Source inconnue")
        try:
            return read_indicators(session, item_id, as_of or datetime.now(UTC), source_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/items/{item_id}", response_model=ItemView, tags=["Objets"])
    def item(
        item_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
        session: Session = db_dep,
        game_type: Annotated[str, Query(pattern=r"^[a-z][a-z0-9-]{0,31}$")] = "forever",
        region: Annotated[
            str, Query(pattern=r"^(eu|us|kr|tw|cn)(-[a-z0-9-]+)?$", max_length=32)
        ] = "eu",
        dataset: Literal["real", "synthetic"] = "real",
    ) -> ItemView:
        feeds = list(
            session.scalars(
                select(DataSource)
                .join(Market)
                .where(
                    Market.game_type == game_type,
                    Market.region == region,
                    DataSource.dataset == dataset,
                )
                .order_by(DataSource.id)
                .limit(101)
            )
        )
        if len(feeds) > 100:
            raise HTTPException(422, "Plus de 100 sources : réduire le périmètre d'ingestion")
        output: list[MarketData] = []
        present = False
        for source in feeds:
            snapshot = session.scalar(
                select(MarketSnapshot)
                .options(defer(MarketSnapshot.raw_gzip))
                .where(MarketSnapshot.source_id == source.id)
                .order_by(
                    func.coalesce(MarketSnapshot.observed_at, MarketSnapshot.fetched_at).desc(),
                    MarketSnapshot.fetched_at.desc(),
                    MarketSnapshot.id,
                )
                .limit(1)
            )
            observation = (
                None if snapshot is None else session.get(MarketObservation, (snapshot.id, item_id))
            )
            values = None if observation is None else Values.model_validate(observation)
            present = present or observation is not None
            output.append(
                MarketData(
                    source=source_view(source),
                    status="no_snapshot"
                    if snapshot is None
                    else ("present" if observation else "missing_in_latest_snapshot"),
                    name=None if observation is None else observation.name,
                    values=values,
                    evidence=None
                    if snapshot is None
                    else evidence(snapshot, FeedSpec.model_validate(source.spec), settings),
                    missing_metrics=list(Values.model_fields)
                    if values is None
                    else [k for k, v in values.model_dump().items() if v is None],
                    extra={} if observation is None else observation.extra,
                )
            )
        if not present:
            # L'historique reste consultable si l'objet a disparu du dernier fichier.
            known = session.scalar(
                select(MarketObservation.item_id)
                .join(MarketSnapshot)
                .where(
                    MarketSnapshot.source_id.in_([s.id for s in feeds]),
                    MarketObservation.item_id == item_id,
                )
                .limit(1)
            )
            if known is None:
                raise HTTPException(404, "Objet inconnu dans ce périmètre")
        return ItemView(
            item_id=item_id, game_type=game_type, region=region, dataset=dataset, markets=output
        )

    @app.get("/v1/items/{item_id}/history", response_model=HistoryView, tags=["Objets"])
    def history(
        item_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
        source_id: Annotated[str, Query(pattern=r"^[a-f0-9]{64}$")],
        session: Session = db_dep,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> HistoryView:
        if any(t is not None and t.tzinfo is None for t in (since, until)):
            raise HTTPException(422, "Les bornes temporelles exigent un fuseau")
        if since and until and since > until:
            raise HTTPException(422, "since doit précéder until")
        source = session.get(DataSource, source_id)
        if source is None:
            raise HTTPException(404, "Source inconnue")
        stmt = (
            select(MarketObservation, MarketSnapshot)
            .join(MarketSnapshot)
            .options(defer(MarketSnapshot.raw_gzip))
            .where(MarketSnapshot.source_id == source_id, MarketObservation.item_id == item_id)
        )
        # Les bornes portent sur le scan fournisseur : dates inconnues exclues si filtrées.
        if since is not None:
            stmt = stmt.where(MarketSnapshot.observed_at >= since)
        if until is not None:
            stmt = stmt.where(MarketSnapshot.observed_at <= until)
        rows = session.execute(
            stmt.order_by(
                func.coalesce(MarketSnapshot.observed_at, MarketSnapshot.fetched_at).desc(),
                MarketSnapshot.fetched_at.desc(),
                MarketSnapshot.id,
            )
            .limit(limit + 1)
            .offset(offset)
        ).all()
        return HistoryView(
            item_id=item_id,
            source=source_view(source),
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
            points=[
                HistoryPoint(
                    name=o.name,
                    values=Values.model_validate(o),
                    evidence=evidence(s, FeedSpec.model_validate(source.spec), settings),
                )
                for o, s in rows[:limit]
            ],
        )

    return app
