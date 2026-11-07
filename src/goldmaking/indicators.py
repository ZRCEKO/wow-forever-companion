"""Indicateurs descriptifs locaux, indépendants de l'éligibilité d'achat-revente."""

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from goldmaking.analysis import load_snapshots, primary_source_id, validated_source
from goldmaking.detector import (
    Contract,
    DetectorParameters,
    SnapshotInput,
    evaluate,
    quartiles,
    ratio,
    utc,
)
from goldmaking.models import MarketObservation
from goldmaking.tsm import FeedSpec


class PriceChange(Contract):
    state: Literal["calculable", "not_calculable"] = "not_calculable"
    reason: str | None = None
    target_at: datetime | None = None
    baseline_snapshot_id: str | None = None
    baseline_price: int | None = None
    baseline_at: datetime | None = None
    actual_elapsed_seconds: int | None = None
    absolute_copper: int | None = None
    relative: Decimal | None = None


class PriceDistribution(Contract):
    state: Literal["calculable", "not_calculable"] = "not_calculable"
    reason: str | None = None
    sample_count: int | None = None
    minimum_samples: int = 3
    median: int | None = None
    q25: int | None = None
    q75: int | None = None
    minimum: int | None = None
    maximum: int | None = None
    relative_iqr: Decimal | None = None
    snapshot_ids: list[str] = []


class Coverage(Contract):
    state: Literal["evaluated", "not_evaluated"] = "not_evaluated"
    valid_buckets: int | None = None
    total_buckets: int = 56
    required_buckets: int = 40
    required_span_seconds: int = 120 * 3600
    span_seconds: int | None = None
    meets_history_requirements: bool | None = None


class IndicatorEvidence(SnapshotInput):
    content_sha256: str
    schema_sha256: str
    parser_version: str


class ReturnInterval(Contract):
    from_snapshot_id: str
    to_snapshot_id: str
    elapsed_seconds: int
    relative_return: Decimal


class TemporalVolatility(Contract):
    state: Literal["calculable", "not_calculable"] = "not_calculable"
    reason: str | None = None
    bucket_seconds: int = 3 * 3600
    minimum_return_count: int = 8
    minimum_span_seconds: int = 24 * 3600
    return_count: int | None = None
    span_seconds: int | None = None
    minimum_interval_seconds: int | None = None
    maximum_interval_seconds: int | None = None
    mean_relative_return: Decimal | None = None
    sample_variance: Decimal | None = None
    stddev_relative_return: Decimal | None = None
    snapshot_ids: list[str] = []
    intervals: list[ReturnInterval] = []


class ReferenceDeviation(Contract):
    state: Literal["calculable", "not_calculable"] = "not_calculable"
    reason: str | None = None
    median: int | None = None
    absolute_copper: int | None = None
    relative: Decimal | None = None
    quartile_position: Literal["below_q25", "within_quartiles", "above_q75"] | None = None


class PriceAnomaly(Contract):
    state: Literal["calculable", "not_calculable"] = "not_calculable"
    reason: str | None = None
    method: Literal["historical-iqr-fences-v1"] = "historical-iqr-fences-v1"
    fence_multiplier: Decimal = Decimal("1.5")
    minimum_samples: int = 40
    minimum_span_seconds: int = 120 * 3600
    sample_count: int | None = None
    span_seconds: int | None = None
    iqr_copper: int | None = None
    lower_inclusive_copper: int | None = None
    upper_inclusive_copper: int | None = None
    position: Literal["below_fence", "within_fences", "above_fence"] | None = None
    flagged: bool | None = None


class ItemIndicators(Contract):
    indicator_version: Literal["local-descriptive-v3"] = "local-descriptive-v3"
    item_id: int
    source_id: str
    source: FeedSpec
    as_of: datetime
    currency: Literal["copper"] = "copper"
    granularity: Literal["base_item"] = "base_item"
    current_snapshot_id: str | None = None
    current_price: int | None = None
    current_observed_at: datetime | None = None
    age_seconds: int | None = None
    signal_state: str
    signal_reasons: list[str]
    coverage: Coverage = Coverage()
    previous_scan_change: PriceChange = PriceChange()
    change_24h: PriceChange = PriceChange()
    distribution: PriceDistribution = PriceDistribution()
    volatility: TemporalVolatility = TemporalVolatility()
    reference_deviation: ReferenceDeviation = ReferenceDeviation()
    anomaly: PriceAnomaly = PriceAnomaly()
    exclusion_counts: dict[str, int] = {}
    evidence: list[IndicatorEvidence] = []
    limitations: list[str] = [
        "Indicateurs descriptifs, sans estimation de bénéfice ou de ventes locales.",
        "Dispersion interquartile, pas volatilité temporelle ni prévision.",
        "Volatilité descriptive : chaîne récente de créneaux voisins, sans annualisation.",
        "Intervalles de scans irréguliers : aucune normalisation à une durée constante.",
        "Référence 24 h : dernier scan à la cible ou avant, avec retard maximal de 3 h.",
        "Aucune interpolation ; les durées réelles sont indiquées.",
        "Dates de connaissance : convention de fin d'import avant commit.",
        "Écart et prix atypique : comparaisons descriptives, sans probabilité ni action de jeu.",
        "Bornes interquartiles non calibrées sur les ventes ; absence de drapeau non probante.",
    ]


def reference_comparison(
    price: int, distribution: PriceDistribution, span_seconds: int | None
) -> tuple[ReferenceDeviation, PriceAnomaly]:
    """Comparer au passé échantillonné ; le courant n'entre pas dans les quartiles."""
    anomaly = PriceAnomaly(sample_count=distribution.sample_count, span_seconds=span_seconds)
    if distribution.state != "calculable":
        return ReferenceDeviation(reason=distribution.reason), anomaly.model_copy(
            update={"reason": "insufficient_history"}
        )
    q25, median, q75 = distribution.q25, distribution.median, distribution.q75
    assert q25 is not None and median is not None and q75 is not None and median > 0
    delta = price - median
    deviation = ReferenceDeviation(
        state="calculable",
        median=median,
        absolute_copper=delta,
        relative=ratio(delta, median),
        quartile_position="below_q25"
        if price < q25
        else "above_q75"
        if price > q75
        else "within_quartiles",
    )
    assert distribution.sample_count is not None
    if distribution.sample_count < anomaly.minimum_samples:
        return deviation, anomaly.model_copy(update={"reason": "insufficient_history"})
    if span_seconds is None or span_seconds < anomaly.minimum_span_seconds:
        return deviation, anomaly.model_copy(update={"reason": "insufficient_time_span"})
    iqr = q75 - q25
    anomaly = anomaly.model_copy(update={"iqr_copper": iqr})
    if iqr == 0:
        return deviation, anomaly.model_copy(update={"reason": "zero_reference_iqr"})
    # Bornes entières inclusives : ceil(Q25 - 1,5 IQR), floor(Q75 + 1,5 IQR).
    # Les prix sont positifs ; borner le bas à zéro ne change aucun classement.
    width = 3 * iqr // 2
    lower = max(0, q25 - width)
    upper = q75 + width
    position = (
        "below_fence" if price < lower else "above_fence" if price > upper else "within_fences"
    )
    return deviation, anomaly.model_copy(
        update={
            "state": "calculable",
            "lower_inclusive_copper": lower,
            "upper_inclusive_copper": upper,
            "position": position,
            "flagged": position != "within_fences",
        }
    )


def temporal_volatility(
    current: SnapshotInput,
    buckets: dict[int, SnapshotInput],
    total_buckets: int,
) -> TemporalVolatility:
    """Écart-type échantillonnal des retours de la chaîne récente, sans franchir un trou."""
    chain = [current]
    for bucket in range(total_buckets - 1, -1, -1):
        previous = buckets.get(bucket)
        if previous is None:
            break
        chain.append(previous)
    chain.reverse()
    assert current.observed_at is not None and chain[0].observed_at is not None
    span = int((current.observed_at - chain[0].observed_at).total_seconds())
    intervals = []
    with localcontext() as context:
        context.prec = 50
        for earlier, later in zip(chain, chain[1:], strict=False):
            assert earlier.min_buyout is not None and earlier.min_buyout > 0
            assert later.min_buyout is not None and later.min_buyout > 0
            assert earlier.observed_at is not None and later.observed_at is not None
            intervals.append(
                ReturnInterval(
                    from_snapshot_id=earlier.snapshot_id,
                    to_snapshot_id=later.snapshot_id,
                    elapsed_seconds=int((later.observed_at - earlier.observed_at).total_seconds()),
                    relative_return=Decimal(later.min_buyout - earlier.min_buyout)
                    / Decimal(earlier.min_buyout),
                )
            )
        elapsed = [i.elapsed_seconds for i in intervals]
        result = TemporalVolatility(
            return_count=len(intervals),
            span_seconds=span,
            minimum_interval_seconds=min(elapsed) if elapsed else None,
            maximum_interval_seconds=max(elapsed) if elapsed else None,
            snapshot_ids=[p.snapshot_id for p in chain],
            intervals=intervals,
        )
        if len(intervals) < result.minimum_return_count:
            return result.model_copy(update={"reason": "insufficient_consecutive_history"})
        if span < result.minimum_span_seconds:
            return result.model_copy(update={"reason": "insufficient_time_span"})
        returns = [i.relative_return for i in intervals]
        mean = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = sum(((r - mean) ** 2 for r in returns), Decimal(0)) / Decimal(len(returns) - 1)
        return result.model_copy(
            update={
                "state": "calculable",
                "mean_relative_return": mean,
                "sample_variance": variance,
                "stddev_relative_return": variance.sqrt(),
            }
        )


def change(
    current: SnapshotInput,
    baseline: SnapshotInput | None,
    target: datetime | None = None,
) -> PriceChange:
    if baseline is None:
        return PriceChange(reason="no_baseline", target_at=target)
    assert current.observed_at is not None and baseline.observed_at is not None
    info = PriceChange(
        target_at=target,
        baseline_snapshot_id=baseline.snapshot_id,
        baseline_price=baseline.min_buyout,
        baseline_at=baseline.observed_at,
        actual_elapsed_seconds=int((current.observed_at - baseline.observed_at).total_seconds()),
    )
    reason = (
        "baseline_too_old"
        if target is not None and target - baseline.observed_at > timedelta(hours=3)
        else "baseline_item_missing"
        if not baseline.present
        else "baseline_price_missing"
        if baseline.min_buyout is None
        else "baseline_price_zero"
        if baseline.min_buyout == 0
        else None
    )
    if reason is not None:
        return info.model_copy(update={"reason": reason})
    assert current.min_buyout is not None and baseline.min_buyout is not None
    delta = current.min_buyout - baseline.min_buyout
    return info.model_copy(
        update={
            "state": "calculable",
            "absolute_copper": delta,
            "relative": ratio(delta, baseline.min_buyout),
        }
    )


def describe(
    item_id: int,
    source: FeedSpec,
    points: list[SnapshotInput],
    as_of: datetime,
) -> ItemIndicators:
    as_of = utc(as_of)
    evaluation = evaluate(item_id, source, points, as_of)
    indexed = {p.snapshot_id: p for p in points}
    current = indexed.get(evaluation.current_snapshot_id or "")
    report = ItemIndicators(
        item_id=item_id,
        source_id=source.source_id,
        source=source,
        as_of=as_of,
        current_snapshot_id=evaluation.current_snapshot_id,
        current_price=evaluation.current_price,
        current_observed_at=None if current is None else current.observed_at,
        age_seconds=None
        if current is None or current.observed_at is None
        else int((as_of - current.observed_at).total_seconds()),
        signal_state=evaluation.price_state,
        signal_reasons=evaluation.reasons,
        exclusion_counts=dict(
            sorted(
                Counter(
                    r.role for r in evaluation.evidence if r.role not in ("current", "reference")
                ).items()
            )
        ),
    )
    # Ces motifs arrêtent le détecteur avant l'échantillonnage de référence.
    blocking = [
        r
        for r in evaluation.reasons
        if r
        not in (
            "insufficient_history",
            "unstable_reference",
            "discount_threshold_not_met",
        )
    ]
    if blocking:
        return report.model_copy(
            update={
                "previous_scan_change": PriceChange(reason=blocking[0]),
                "change_24h": PriceChange(reason=blocking[0]),
                "distribution": PriceDistribution(reason=blocking[0]),
                "volatility": TemporalVolatility(reason=blocking[0]),
                "reference_deviation": ReferenceDeviation(reason=blocking[0]),
                "anomaly": PriceAnomaly(reason=blocking[0]),
            }
        )
    assert current is not None and current.observed_at is not None
    roles = {r.snapshot_id: r.role for r in evaluation.evidence}
    historical = sorted(
        [
            p
            for p in points
            if roles.get(p.snapshot_id)
            in (
                "reference",
                "missing_in_bucket",
                "bucket_price_missing",
                "bucket_price_zero",
                "superseded_in_bucket",
            )
        ],
        key=lambda p: p.observed_at or p.fetched_at,
    )
    # Sélectionner d'abord le scan, puis valider sa présence et son prix.
    previous = None if not historical else historical[-1]
    target = current.observed_at - timedelta(hours=24)
    before_target = [p for p in historical if p.observed_at is not None and p.observed_at <= target]
    baseline_24h = None if not before_target else before_target[-1]
    samples = [p for p in historical if roles[p.snapshot_id] == "reference"]
    volatility = temporal_volatility(
        current,
        {
            ref.bucket: indexed[ref.snapshot_id]
            for ref in evaluation.evidence
            if ref.role == "reference" and ref.bucket is not None
        },
        evaluation.total_buckets,
    )
    distribution = PriceDistribution(sample_count=len(samples), reason="insufficient_samples")
    if len(samples) >= 3:
        prices = [p.min_buyout for p in samples if p.min_buyout is not None]
        q25, median, q75 = quartiles(prices)
        distribution = PriceDistribution(
            state="calculable",
            sample_count=len(samples),
            median=median,
            q25=q25,
            q75=q75,
            minimum=min(prices),
            maximum=max(prices),
            relative_iqr=ratio(q75 - q25, median),
            snapshot_ids=[p.snapshot_id for p in samples],
        )
    assert current.min_buyout is not None
    deviation, anomaly = reference_comparison(
        current.min_buyout, distribution, evaluation.span_seconds
    )
    return report.model_copy(
        update={
            "coverage": Coverage(
                state="evaluated",
                valid_buckets=evaluation.valid_buckets,
                span_seconds=evaluation.span_seconds,
                meets_history_requirements=evaluation.valid_buckets >= 40
                and evaluation.span_seconds is not None
                and evaluation.span_seconds >= 120 * 3600,
            ),
            "previous_scan_change": change(current, previous),
            "change_24h": change(current, baseline_24h, target),
            "distribution": distribution,
            "volatility": volatility,
            "reference_deviation": deviation,
            "anomaly": anomaly,
        }
    )


def read_indicators(
    session: Session,
    item_id: int,
    as_of: datetime,
    source_id: str | None = None,
) -> ItemIndicators:
    explicit_source = source_id is not None
    source_id = source_id or primary_source_id()
    spec = validated_source(session, source_id, allow_synthetic=explicit_source)
    snapshots = load_snapshots(session, source_id, utc(as_of), DetectorParameters())
    observations = {
        o.snapshot_id: o
        for o in session.scalars(
            select(MarketObservation).where(
                MarketObservation.snapshot_id.in_([s.id for s, _ in snapshots]),
                MarketObservation.item_id == item_id,
            )
        )
    }
    evidence = {}
    for s, known_at in snapshots:
        obs = observations.get(s.id)
        evidence[s.id] = IndicatorEvidence(
            snapshot_id=s.id,
            observed_at=s.observed_at,
            fetched_at=s.fetched_at,
            known_at=known_at,
            present=obs is not None,
            min_buyout=None if obs is None else obs.min_buyout,
            content_sha256=s.content_sha256,
            schema_sha256=s.schema_sha256,
            parser_version=s.parser_version,
        )
    report = describe(item_id, spec, list(evidence.values()), as_of)
    used = {*report.distribution.snapshot_ids, *report.volatility.snapshot_ids}
    for sid in (
        report.current_snapshot_id,
        report.previous_scan_change.baseline_snapshot_id,
        report.change_24h.baseline_snapshot_id,
    ):
        if sid is not None:
            used.add(sid)
    return report.model_copy(update={"evidence": [evidence[sid] for sid in sorted(used)]})
