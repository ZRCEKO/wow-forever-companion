"""Détecteur déterministe local-discount-v1, sans BDD ni action dans le jeu."""

from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from goldmaking.money import MAX_COPPER
from goldmaking.tsm import FeedSpec

Copper = Annotated[int, Field(strict=True, ge=0, le=MAX_COPPER)]
VERSION = "local-discount-v1"
LIMITATIONS = [
    "Prix agrégés TSM : quantité achetable et variantes inconnues.",
    "Signal à examiner manuellement ; prix de revente et liquidation non garantis.",
    "known_at est la fin du premier import réussi, avant commit : convention de rejeu.",
    "Aucune probabilité de vente locale déduite des statistiques régionales.",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DetectorParameters(Contract):
    window_hours: Annotated[int, Field(strict=True, ge=3, le=720)] = 168
    bucket_hours: Annotated[int, Field(strict=True, ge=1, le=24)] = 3
    min_buckets: Annotated[int, Field(strict=True, ge=1, le=720)] = 40
    min_span_hours: Annotated[int, Field(strict=True, ge=0, le=720)] = 120
    max_age_hours: Annotated[int, Field(strict=True, ge=1, le=48)] = 6
    max_dispersion_percent: Annotated[int, Field(strict=True, ge=0, le=1000)] = 50
    max_price_percent: Annotated[int, Field(strict=True, ge=1, le=100)] = 70
    exit_percent: Annotated[int, Field(strict=True, ge=1, le=100)] = 90
    min_gain: Copper = 100
    min_roi_percent: Annotated[int, Field(strict=True, ge=0, le=10000)] = 20

    @model_validator(mode="after")
    def consistent_window(self) -> Self:
        if self.window_hours % self.bucket_hours:
            raise ValueError("La fenêtre doit être un multiple du pas")
        if self.min_buckets > self.window_hours // self.bucket_hours:
            raise ValueError("Nombre minimal de créneaux supérieur à la fenêtre")
        if self.min_span_hours >= self.window_hours:
            raise ValueError("La durée minimale doit être inférieure à la fenêtre")
        return self


class CostProfile(Contract):
    # Aucun registre de règles Forever homologuées n'existe encore.
    status: Literal["hypothetical"]
    profile_id: Annotated[str, Field(min_length=1, max_length=100)]
    version: Annotated[str, Field(min_length=1, max_length=32)]
    source_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    game_type: Annotated[str, Field(min_length=1, max_length=32)]
    region: Annotated[str, Field(min_length=1, max_length=32)]
    realm_slug: Annotated[str, Field(min_length=1, max_length=100)]
    ruleset: Annotated[str, Field(min_length=1, max_length=32)]
    faction: Literal["alliance", "horde", "neutral", "shared", "unknown"]
    auction_house: Annotated[str, Field(min_length=1, max_length=100)]
    evidence: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence_at: datetime
    commission_rate: Annotated[Decimal, Field(ge=0, le=1, max_digits=13, decimal_places=12)]
    rounding: Literal["ceil", "floor", "half_up"]
    final_deposit: Copper | None
    lost_deposits: Copper | None
    other_costs: Copper | None
    costs_justification: Annotated[str, Field(min_length=1, max_length=2000)]
    final_deposit_refunded: Annotated[bool, Field(strict=True)] | None

    @field_validator("commission_rate", mode="before")
    @classmethod
    def exact_rate(cls, value: object) -> object:
        if isinstance(value, (float, bool)):
            raise ValueError("Taux décimal exact requis, utiliser une chaîne JSON")
        return value

    @field_validator("evidence_at")
    @classmethod
    def dated_evidence(cls, value: datetime) -> datetime:
        return utc(value)


class SnapshotInput(Contract):
    snapshot_id: str
    observed_at: datetime | None
    fetched_at: datetime
    known_at: datetime | None
    present: bool
    min_buyout: Copper | None

    @field_validator("observed_at", "fetched_at", "known_at")
    @classmethod
    def dates(cls, value: datetime | None) -> datetime | None:
        return None if value is None else utc(value)


class EvidenceRef(Contract):
    snapshot_id: str
    role: str
    bucket: int | None = None
    present: bool
    min_buyout: int | None


class Economics(Contract):
    state: Literal[
        "not_calculable", "scenario_below_thresholds", "hypothetical_scenario_passes"
    ] = "not_calculable"
    reasons: list[str] = []
    commission: int | None = None
    conditional_gain: int | None = None
    capital: int | None = None
    roi: Decimal | None = None
    margin: Decimal | None = None
    affordable: bool | None = None


class Evaluation(Contract):
    item_id: int
    price_state: Literal["not_calculable", "no_signal", "candidate_to_review"] = "not_calculable"
    reasons: list[str] = []
    current_snapshot_id: str | None = None
    current_price: int | None = None
    reference: int | None = None
    q25: int | None = None
    q75: int | None = None
    exit_price: int | None = None
    discount: Decimal | None = None
    dispersion: Decimal | None = None
    valid_buckets: int = 0
    total_buckets: int
    missing_buckets: list[int] = []
    span_seconds: int | None = None
    economics: Economics = Economics()
    evidence: list[EvidenceRef] = []


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Horodatage avec fuseau requis")
    return value.astimezone(UTC)


def bounded(value: int) -> int:
    if not -MAX_COPPER <= value <= MAX_COPPER:
        raise ValueError("Dépassement de la plage de cuivre")
    return value


def ratio(numerator: int, denominator: int) -> Decimal | None:
    with localcontext() as context:
        context.prec = 40
        return None if denominator == 0 else Decimal(numerator) / Decimal(denominator)


def quartiles(prices: list[int]) -> tuple[int, int, int]:
    if not prices:
        raise ValueError("Quantiles non calculables sans prix")
    ordered = sorted(prices)
    n = len(ordered)
    q25, median, q75 = (ordered[(n * percent + 99) // 100 - 1] for percent in (25, 50, 75))
    return q25, median, q75


def simulate(
    purchase: int,
    sale: int,
    profile: CostProfile | None,
    source: FeedSpec,
    parameters: DetectorParameters,
    budget: int | None = None,
) -> Economics:
    for amount in (purchase, sale, budget):
        if amount is not None and (type(amount) is not int or not 0 <= amount <= MAX_COPPER):
            raise ValueError("Cuivre entier positif ou nul requis")
    reasons = ["budget_unknown"] if budget is None else []
    if profile is None:
        return Economics(reasons=["cost_profile_unknown", *reasons])
    dimensions = ("game_type", "region", "realm_slug", "ruleset", "faction")
    if profile.source_id != source.source_id or any(
        getattr(profile, key) != getattr(source, key) for key in dimensions
    ):
        return Economics(reasons=["cost_profile_source_mismatch", *reasons])
    if (
        any(
            amount is None
            for amount in (profile.final_deposit, profile.lost_deposits, profile.other_costs)
        )
        or profile.final_deposit_refunded is not True
    ):
        return Economics(reasons=["incomplete_costs_or_refund_rule", *reasons])
    assert profile.final_deposit is not None
    assert profile.lost_deposits is not None and profile.other_costs is not None
    rounding = {"ceil": ROUND_CEILING, "floor": ROUND_FLOOR, "half_up": ROUND_HALF_UP}
    with localcontext() as context:
        context.prec = 60
        commission = int(
            (Decimal(sale) * profile.commission_rate).to_integral_value(
                rounding=rounding[profile.rounding]
            )
        )
    gain = bounded(sale - commission - purchase - profile.lost_deposits - profile.other_costs)
    capital = bounded(
        purchase + profile.lost_deposits + profile.other_costs + profile.final_deposit
    )
    passes = gain >= parameters.min_gain and 100 * gain >= parameters.min_roi_percent * capital
    if not passes:
        reasons.append("economic_threshold_not_met")
    if budget is not None and budget < capital:
        reasons.append("budget_insufficient")
    return Economics(
        state="hypothetical_scenario_passes" if passes else "scenario_below_thresholds",
        reasons=reasons,
        commission=commission,
        conditional_gain=gain,
        capital=capital,
        roi=ratio(gain, capital),
        margin=ratio(gain, sale),
        affordable=None if budget is None else budget >= capital,
    )


def evaluate(
    item_id: int,
    source: FeedSpec,
    snapshots: list[SnapshotInput],
    as_of: datetime,
    parameters: DetectorParameters | None = None,
    profile: CostProfile | None = None,
    budget: int | None = None,
) -> Evaluation:
    """Ne compte que les données connues à as_of ; aucun accès réseau ou état global."""
    as_of = utc(as_of)
    p = parameters or DetectorParameters()
    total = p.window_hours // p.bucket_hours
    result = Evaluation(
        item_id=item_id,
        total_buckets=total,
        economics=Economics(
            reasons=[
                "price_not_calculable",
                *(["cost_profile_unknown"] if profile is None else []),
                *(["budget_unknown"] if budget is None else []),
            ]
        ),
    )
    refs: dict[str, EvidenceRef] = {}

    def record(s: SnapshotInput, role: str, bucket: int | None = None) -> None:
        refs[s.snapshot_id] = EvidenceRef(
            snapshot_id=s.snapshot_id,
            role=role,
            bucket=bucket,
            present=s.present,
            min_buyout=s.min_buyout,
        )

    def finish(**updates: object) -> Evaluation:
        # Trier les références permet le même résultat quel que soit l'ordre des entrées.
        return result.model_copy(update={"evidence": [refs[k] for k in sorted(refs)], **updates})

    if source.scope != "realm":
        return finish(reasons=["incompatible_source_scope"])
    if source.ruleset is None or source.faction in (None, "unknown", "shared"):
        return finish(reasons=["incomplete_market_dimensions"])
    known = [
        s
        for s in snapshots
        if s.known_at is not None and s.known_at <= as_of and s.fetched_at <= as_of
    ]
    dated: list[SnapshotInput] = []
    undated: list[SnapshotInput] = []
    for s in known:
        if s.observed_at is None:
            undated.append(s)
            record(s, "unknown_scan")
        elif s.observed_at > as_of or s.observed_at > s.fetched_at:
            record(s, "invalid_scan_time")
        else:
            dated.append(s)
    if not dated:
        return finish(reasons=["unknown_current_scan" if undated else "no_known_snapshot"])
    # Date descendante, correction la plus récemment connue, puis UUID croissant.
    dated.sort(key=lambda s: s.snapshot_id)
    dated.sort(key=lambda s: (s.known_at or s.fetched_at, s.fetched_at), reverse=True)
    dated.sort(key=lambda s: s.observed_at or s.fetched_at, reverse=True)
    current = dated[0]
    assert current.observed_at is not None and current.known_at is not None
    for s in dated:
        record(s, "outside_window")
    record(current, "current")
    result = result.model_copy(
        update={"current_snapshot_id": current.snapshot_id, "current_price": current.min_buyout}
    )
    if any(s.known_at > current.known_at for s in undated if s.known_at is not None):
        return finish(reasons=["unknown_current_scan"])
    current_reasons = []
    if not current.present:
        current_reasons.append("missing_in_latest_snapshot")
    elif current.min_buyout is None:
        current_reasons.append("current_price_missing")
    elif current.min_buyout == 0:
        current_reasons.append("current_price_zero")
    if as_of - current.observed_at > timedelta(hours=p.max_age_hours):
        current_reasons.append("stale_scan")
    if current_reasons:
        return finish(reasons=current_reasons)
    by_time: dict[datetime, SnapshotInput] = {}
    for s in dated:
        assert s.observed_at is not None
        if s.observed_at in by_time:
            record(s, "superseded_correction")
        else:
            by_time[s.observed_at] = s
    start = current.observed_at - timedelta(hours=p.window_hours)
    step = p.bucket_hours * 3600
    buckets: dict[int, SnapshotInput] = {}
    for time, s in by_time.items():
        if start <= time < current.observed_at:
            bucket = int((time - start).total_seconds()) // step
            # dated est déjà trié : le premier dans un créneau est le plus récent.
            if bucket in buckets:
                record(s, "superseded_in_bucket", bucket)
            else:
                buckets[bucket] = s
    prices: list[int] = []
    times: list[datetime] = []
    missing = []
    for bucket in range(total):
        point = buckets.get(bucket)
        if point is None:
            missing.append(bucket)
            continue
        role = (
            "missing_in_bucket"
            if not point.present
            else "bucket_price_missing"
            if point.min_buyout is None
            else "bucket_price_zero"
            if point.min_buyout == 0
            else "reference"
        )
        record(point, role, bucket)
        if role != "reference":
            missing.append(bucket)
        else:
            assert point.min_buyout is not None and point.observed_at is not None
            prices.append(point.min_buyout)
            times.append(point.observed_at)
    span = None if not times else int((max(times) - min(times)).total_seconds())
    result = result.model_copy(
        update={
            "valid_buckets": len(prices),
            "missing_buckets": missing,
            "span_seconds": span,
        }
    )
    if len(prices) < p.min_buckets or span is None or span < p.min_span_hours * 3600:
        return finish(reasons=["insufficient_history"])
    q25, reference, q75 = quartiles(prices)
    assert current.min_buyout is not None
    result = result.model_copy(
        update={
            "reference": reference,
            "q25": q25,
            "q75": q75,
            "dispersion": ratio(q75 - q25, reference),
        }
    )
    if 100 * (q75 - q25) > p.max_dispersion_percent * reference:
        return finish(reasons=["unstable_reference"])
    exit_price = p.exit_percent * q25 // 100
    economics = simulate(current.min_buyout, exit_price, profile, source, p, budget)
    candidate = 100 * current.min_buyout <= p.max_price_percent * reference
    return finish(
        price_state="candidate_to_review" if candidate else "no_signal",
        reasons=[] if candidate else ["discount_threshold_not_met"],
        exit_price=exit_price,
        discount=ratio(reference - current.min_buyout, reference),
        economics=economics,
    )
