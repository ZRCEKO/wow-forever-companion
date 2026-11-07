"""Lecture bornée de l'historique et archivage atomique des analyses via CLI."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import Engine, func, or_, select
from sqlalchemy.orm import Session, defer

from goldmaking.detector import (
    LIMITATIONS,
    VERSION,
    Copper,
    CostProfile,
    DetectorParameters,
    Evaluation,
    SnapshotInput,
    evaluate,
    utc,
)
from goldmaking.models import (
    AnalysisEvaluation,
    AnalysisRun,
    AnalysisSnapshot,
    DataSource,
    IngestionRun,
    Market,
    MarketObservation,
    MarketSnapshot,
)
from goldmaking.tsm import FeedSpec

MAX_SNAPSHOTS = 4096
MAX_ITEMS = 20000


def primary_source_id() -> str:
    return FeedSpec(
        scope="realm",
        realm_slug="normal-alliance",
        ruleset="normal",
        faction="alliance",
        transport="http",
    ).source_id


def load_snapshots(
    session: Session, source_id: str, as_of: datetime, parameters: DetectorParameters
) -> list[tuple[MarketSnapshot, datetime]]:
    known = (
        select(
            IngestionRun.snapshot_id,
            func.min(IngestionRun.finished_at).label("known_at"),
        )
        .where(
            IngestionRun.source_id == source_id,
            IngestionRun.status == "imported",
            IngestionRun.finished_at.is_not(None),
        )
        .group_by(IngestionRun.snapshot_id)
        .subquery()
    )
    stmt = (
        select(MarketSnapshot, known.c.known_at)
        .join(known, known.c.snapshot_id == MarketSnapshot.id)
        .options(defer(MarketSnapshot.raw_gzip))
        .where(
            MarketSnapshot.source_id == source_id,
            MarketSnapshot.fetched_at <= as_of,
            known.c.known_at <= as_of,
        )
    )
    current = session.execute(
        stmt.where(
            MarketSnapshot.observed_at.is_not(None),
            MarketSnapshot.observed_at <= MarketSnapshot.fetched_at,
            MarketSnapshot.observed_at <= as_of,
        )
        .order_by(
            MarketSnapshot.observed_at.desc(),
            known.c.known_at.desc(),
            MarketSnapshot.fetched_at.desc(),
            MarketSnapshot.id,
        )
        .limit(1)
    ).first()
    anchor = as_of if current is None else current[0].observed_at
    cutoff = anchor - timedelta(hours=parameters.window_hours)
    rows = session.execute(
        stmt.where(
            or_(
                func.coalesce(MarketSnapshot.observed_at, MarketSnapshot.fetched_at) >= cutoff,
                # Un téléchargement ancien mais terminé tard peut rendre l'ordre inconnu.
                (MarketSnapshot.observed_at.is_(None))
                & (known.c.known_at >= (as_of if current is None else current[1])),
            )
        )
        .order_by(MarketSnapshot.id)
        .limit(MAX_SNAPSHOTS + 1)
    ).all()
    if len(rows) > MAX_SNAPSHOTS:
        raise ValueError(
            "Plus de 4096 snapshots pertinents : réduire la fenêtre, aucun calcul tronqué"
        )
    return [(row[0], utc(row[1])) for row in rows]


def validated_source(
    session: Session, source_id: str, *, allow_synthetic: bool = False
) -> FeedSpec:
    source = session.get(DataSource, source_id)
    if source is None:
        raise ValueError("Source absente ; vérifier /v1/sources")
    spec = FeedSpec.model_validate(source.spec)
    market = session.get(Market, source.market_id)
    dimensions = ("game_type", "region", "scope", "realm_slug", "ruleset", "faction")
    if (
        source.provider != "tsm"
        or spec.source_id != source.id
        or spec.market_id != source.market_id
        or market is None
        or any(getattr(market, key) != getattr(spec, key) for key in dimensions)
        or source.dataset != spec.dataset
        or source.transport != spec.transport
    ):
        raise ValueError("Contrat de source incohérent")
    if spec.dataset == "synthetic" and not allow_synthetic:
        raise ValueError("Fixture fictive : --synthetic requis explicitement")
    if spec.scope != "realm" or spec.ruleset is None or spec.faction in (None, "unknown", "shared"):
        raise ValueError("Le détecteur exige un marché, un ruleset et une faction explicites")
    return spec


def analyze_market(
    engine: Engine,
    source_id: str | None = None,
    as_of: datetime | None = None,
    parameters: DetectorParameters | None = None,
    profile: CostProfile | None = None,
    budget: int | None = None,
    item_id: int | None = None,
    allow_synthetic: bool = False,
) -> str:
    as_of = utc(as_of or datetime.now(UTC))
    p = parameters or DetectorParameters()
    source_id = source_id or primary_source_id()
    # Une erreur laisse la BDD sans analyse partielle.
    with Session(engine) as session, session.begin():
        spec = validated_source(session, source_id, allow_synthetic=allow_synthetic)
        if budget is not None:
            budget = TypeAdapter(Copper).validate_python(budget)
        if item_id is not None and (type(item_id) is not int or not 0 < item_id <= 2**63 - 1):
            raise ValueError("item_id invalide")
        snapshots = load_snapshots(session, source_id, as_of, p)
        ids = [s.id for s, _ in snapshots]
        item_stmt = (
            select(MarketObservation.item_id)
            .where(MarketObservation.snapshot_id.in_(ids))
            .distinct()
            .order_by(MarketObservation.item_id)
        )
        if item_id is None:
            items = list(session.scalars(item_stmt.limit(MAX_ITEMS + 1)))
            if len(items) > MAX_ITEMS:
                raise ValueError("Plus de 20000 objets : préciser --item-id, aucun calcul tronqué")
        else:
            # Un objet demandé absent rend une évaluation explicite, pas une ancienne observation.
            items = [item_id]
        analysis_id = str(uuid4())
        run = AnalysisRun(
            id=analysis_id,
            source_id=source_id,
            as_of=as_of,
            created_at=datetime.now(UTC),
            calculation_version=VERSION,
            contract={
                "source": spec.model_dump(mode="json"),
                "parameters": p.model_dump(mode="json"),
                "cost_profile": None if profile is None else profile.model_dump(mode="json"),
                "budget": budget,
                "item_id": item_id,
                "limitations": LIMITATIONS,
                "evidence_scope": "Snapshots connus pertinents pour la fenêtre du dernier scan.",
            },
            summary={},
        )
        session.add(run)
        session.flush()
        for s, known_at in snapshots:
            session.add(
                AnalysisSnapshot(
                    analysis_id=analysis_id,
                    snapshot_id=s.id,
                    evidence={
                        "snapshot_id": s.id,
                        "source_id": source_id,
                        "content_sha256": s.content_sha256,
                        "schema_sha256": s.schema_sha256,
                        "parser_version": s.parser_version,
                        "columns": s.columns,
                        "unknown_columns": s.unknown_columns,
                        "observed_at": None if s.observed_at is None else s.observed_at.isoformat(),
                        "fetched_at": s.fetched_at.isoformat(),
                        "known_at": known_at.isoformat(),
                    },
                )
            )
        counts: Counter[str] = Counter()
        candidates: list[Evaluation] = []
        # Lots bornés : pas de matrice complète objets × snapshots en mémoire.
        batch_size = max(1, min(100, 10000 // max(1, len(ids))))
        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            observations = {
                (o.snapshot_id, o.item_id): o
                for o in session.scalars(
                    select(MarketObservation).where(
                        MarketObservation.snapshot_id.in_(ids),
                        MarketObservation.item_id.in_(batch),
                    )
                )
            }
            for item in batch:
                points = []
                for s, known_at in snapshots:
                    obs = observations.get((s.id, item))
                    points.append(
                        SnapshotInput(
                            snapshot_id=s.id,
                            observed_at=s.observed_at,
                            fetched_at=s.fetched_at,
                            known_at=known_at,
                            present=obs is not None,
                            min_buyout=None if obs is None else obs.min_buyout,
                        )
                    )
                result = evaluate(item, spec, points, as_of, p, profile, budget)
                counts[result.price_state] += 1
                if result.price_state == "candidate_to_review":
                    candidates.append(result.model_copy(update={"evidence": []}))
                session.add(
                    AnalysisEvaluation(
                        analysis_id=analysis_id,
                        item_id=item,
                        price_state=result.price_state,
                        result=result.model_dump(mode="json", exclude={"evidence"}),
                        evidence_refs=[ref.model_dump(mode="json") for ref in result.evidence],
                    )
                )
            session.flush()
        # Classement exact par fraction, sans arrondi du Decimal affiché.
        candidates.sort(
            key=lambda e: (
                -Fraction(e.reference - e.current_price, e.reference)
                if e.reference is not None and e.current_price is not None
                else Fraction(0),
                e.item_id,
            )
        )
        for rank, candidate in enumerate(candidates, 1):
            stored = session.get(AnalysisEvaluation, (analysis_id, candidate.item_id))
            assert stored is not None
            stored.rank = rank
        run.summary = {
            "evaluated_items": len(items),
            "snapshots": len(snapshots),
            "price_states": dict(sorted(counts.items())),
            "status": "complete",
        }
    return analysis_id


def run_view(run: AnalysisRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "source_id": run.source_id,
        "as_of": run.as_of,
        "created_at": run.created_at,
        "calculation_version": run.calculation_version,
        "contract": run.contract,
        "summary": run.summary,
    }


def analyze_if_changed(
    engine: Engine, source_id: str | None = None, *, allow_synthetic: bool = False
) -> str | None:
    """Rattrapage par le producteur unique, sans archive à chaque poll/304.

    Une analyse complète aux paramètres par défaut et connue après le dernier
    import constitue le point de reprise. Une analyse partielle/personnalisée ne
    le remplace pas. La concurrence multi-producteur n'est pas prise en charge.
    """
    source_id = source_id or primary_source_id()
    now = datetime.now(UTC)
    with Session(engine) as session:
        latest = session.scalar(
            select(IngestionRun)
            .where(
                IngestionRun.source_id == source_id,
                IngestionRun.status == "imported",
                IngestionRun.snapshot_id.is_not(None),
                IngestionRun.finished_at.is_not(None),
                IngestionRun.finished_at <= now,
            )
            .order_by(IngestionRun.finished_at.desc(), IngestionRun.id)
            .limit(1)
        )
        if latest is None:
            return None
        assert latest.finished_at is not None
        expected = DetectorParameters().model_dump(mode="json")
        for run in session.scalars(
            select(AnalysisRun)
            .where(
                AnalysisRun.source_id == source_id,
                AnalysisRun.calculation_version == VERSION,
                AnalysisRun.as_of >= latest.finished_at,
                AnalysisRun.as_of <= now,
                AnalysisRun.created_at >= latest.finished_at,
            )
            .execution_options(yield_per=100)
        ):
            contract = run.contract
            if (
                run.summary.get("status") == "complete"
                and contract.get("parameters") == expected
                and contract.get("item_id") is None
                and contract.get("cost_profile") is None
                and contract.get("budget") is None
            ):
                return None
    return analyze_market(engine, source_id, as_of=now, allow_synthetic=allow_synthetic)
