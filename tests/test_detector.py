"""Tous les prix et profils de ce module sont entièrement fictifs."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from goldmaking.detector import (
    CostProfile,
    DetectorParameters,
    SnapshotInput,
    evaluate,
    ratio,
    simulate,
)
from goldmaking.money import MAX_COPPER
from goldmaking.tsm import FeedSpec

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
SPEC = FeedSpec(
    scope="realm",
    realm_slug="fixture-normal-alliance",
    ruleset="normal",
    faction="alliance",
    dataset="synthetic",
)


def point(index, price=1000, **updates):
    time = NOW - timedelta(hours=3 * index)
    return SnapshotInput(
        snapshot_id=f"fixture-{index:04d}",
        observed_at=time,
        fetched_at=time + timedelta(seconds=1),
        known_at=time + timedelta(seconds=2),
        present=True,
        min_buyout=price,
    ).model_copy(update=updates)


def history(price=700):
    return [point(0, price), *(point(i) for i in range(1, 57))]


def profile(**updates):
    data = dict(
        status="hypothetical",
        profile_id="fixture-only",
        version="1",
        source_id=SPEC.source_id,
        game_type=SPEC.game_type,
        region=SPEC.region,
        realm_slug=SPEC.realm_slug,
        ruleset=SPEC.ruleset,
        faction=SPEC.faction,
        auction_house="fixture-faction",
        evidence="Hypothèses fictives de test",
        evidence_at=NOW,
        commission_rate="0.10",
        rounding="ceil",
        final_deposit=100,
        lost_deposits=0,
        other_costs=0,
        costs_justification="Scénario fictif sans annonce perdue ni coût supplémentaire",
        final_deposit_refunded=True,
    )
    return CostProfile.model_validate({**data, **updates})


def run(points=None, **kwargs):
    return evaluate(
        2447, SPEC, history() if points is None else points, NOW + timedelta(minutes=1), **kwargs
    )


def test_reference_manual_example_and_exact_threshold():
    r = run(profile=profile())
    assert r.price_state == "candidate_to_review"
    assert (r.current_price, r.reference, r.q25, r.q75, r.exit_price) == (
        700,
        1000,
        1000,
        1000,
        900,
    )
    assert r.valid_buckets == 56 and r.missing_buckets == []
    assert r.span_seconds == 165 * 3600
    assert r.discount == Decimal("0.3")
    assert r.economics.conditional_gain == 110
    assert r.economics.capital == 800 and r.economics.roi == Decimal("0.1375")
    assert r.economics.state == "scenario_below_thresholds"
    assert r.economics.affordable is None
    assert run(history(701)).price_state == "no_signal"
    assert run().economics.state == "not_calculable"
    assert "cost_profile_unknown" in run().economics.reasons


def test_coverage_count_span_and_bucket_boundaries():
    # 40 prix, étalés sur exactement 120 h : les deux bornes sont inclusives.
    points = [point(0, 700), *(point(i) for i in range(1, 40)), point(41)]
    assert run(points).price_state == "candidate_to_review"
    assert run(points[:-1]).reasons == ["insufficient_history"]
    assert run([point(0, 700), *(point(i) for i in range(1, 41))]).reasons == [
        "insufficient_history"
    ]
    # Le point à t0-168 h compte, celui d'avant n'entre pas dans la référence.
    r = run([*history(), point(57, 1)])
    assert r.valid_buckets == 56 and r.reference == 1000
    assert any(e.snapshot_id == "fixture-0057" and e.role == "outside_window" for e in r.evidence)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"present": False, "min_buyout": None}, "missing_in_latest_snapshot"),
        ({"min_buyout": None}, "current_price_missing"),
        ({"min_buyout": 0}, "current_price_zero"),
    ],
)
def test_missing_or_zero_current_never_falls_back(change, reason):
    r = run([point(0, 700, **change), *history()[1:]])
    assert r.price_state == "not_calculable" and reason in r.reasons
    assert r.current_price == change["min_buyout"]


def test_freshness_is_scan_time_and_inclusive():
    for age, expected in [
        (timedelta(hours=6), "candidate_to_review"),
        (timedelta(hours=6, microseconds=1), "not_calculable"),
    ]:
        r = evaluate(1, SPEC, history(), NOW + age)
        assert r.price_state == expected


def test_latest_snapshot_in_bucket_can_invalidate_it():
    old = point(20)
    newer = old.model_copy(
        update={
            "snapshot_id": "fixture-missing",
            "observed_at": old.observed_at + timedelta(minutes=1),
            "fetched_at": old.fetched_at + timedelta(minutes=1),
            "known_at": old.known_at + timedelta(minutes=1),
            "present": False,
            "min_buyout": None,
        }
    )
    r = run([*history(), newer])
    assert r.valid_buckets == 55 and len(r.missing_buckets) == 1
    assert (
        next(e for e in r.evidence if e.snapshot_id == newer.snapshot_id).role
        == "missing_in_bucket"
    )
    assert (
        next(e for e in r.evidence if e.snapshot_id == old.snapshot_id).role
        == "superseded_in_bucket"
    )


def test_corrections_ties_and_future_import_do_not_leak():
    original = history()
    later = point(0, 1, snapshot_id="fixture-later", known_at=NOW + timedelta(hours=1))
    r = run(original)
    assert run([later, *reversed(original)]) == r
    correction = point(
        0, 701, snapshot_id="fixture-correction", known_at=NOW + timedelta(seconds=3)
    )
    assert run([*original, correction]).price_state == "no_signal"
    # À dates identiques : UUID lexical croissant.
    tie = point(0, 701, snapshot_id="000-fixture")
    assert run([*original, tie]).current_snapshot_id == "000-fixture"
    delayed = point(1, 1, known_at=NOW + timedelta(hours=1))
    assert run([*original, delayed]) == r


def test_undated_later_blocks_and_invalid_future_time_is_excluded():
    undated = point(
        0, observed_at=None, snapshot_id="fixture-undated", known_at=NOW + timedelta(seconds=3)
    )
    assert run([*history(), undated]).reasons == ["unknown_current_scan"]
    assert run([undated]).reasons == ["unknown_current_scan"]
    invalid = point(0, observed_at=NOW + timedelta(minutes=5), snapshot_id="fixture-future")
    r = run([*history(), invalid])
    assert r.price_state == "candidate_to_review"
    assert (
        next(e for e in r.evidence if e.snapshot_id == invalid.snapshot_id).role
        == "invalid_scan_time"
    )
    assert run([point(0, known_at=None)]).reasons == ["no_known_snapshot"]


def test_quantile_nearest_rank_and_dispersion():
    p = DetectorParameters(window_hours=12, min_buckets=4, min_span_hours=9)
    r = run(
        [point(0, 700), point(1, 1000), point(2, 1100), point(3, 1200), point(4, 1300)],
        parameters=p,
    )
    assert (r.q25, r.reference, r.q75) == (1000, 1100, 1200)
    unstable = run([point(0, 700), *(point(i, 1000 if i < 29 else 4000) for i in range(1, 57))])
    assert unstable.reasons == ["unstable_reference"]


@pytest.mark.parametrize(
    "amount,rounding,expected",
    [
        (901, "ceil", 91),
        (901, "floor", 90),
        (905, "half_up", 91),
    ],
)
def test_commission_rounding(amount, rounding, expected):
    r = simulate(700, amount, profile(rounding=rounding), SPEC, DetectorParameters())
    assert r.commission == expected


def test_unknown_costs_explicit_zeros_negative_gain_and_budget():
    p = DetectorParameters()
    assert simulate(700, 900, profile(final_deposit=None), SPEC, p).conditional_gain is None
    assert simulate(700, 900, profile(final_deposit_refunded=None), SPEC, p).capital is None
    zeros = profile(final_deposit=0, commission_rate="0")
    r = simulate(700, 900, zeros, SPEC, p, budget=699)
    assert r.state == "hypothetical_scenario_passes" and r.conditional_gain == 200
    assert r.affordable is False and "budget_insufficient" in r.reasons
    assert simulate(700, 600, zeros, SPEC, p).conditional_gain == -100
    assert ratio(1, 0) is None
    assert simulate(0, 0, zeros, SPEC, p).roi is None
    assert simulate(0, MAX_COPPER, zeros, SPEC, p).conditional_gain == MAX_COPPER
    with pytest.raises(ValueError, match="Dépassement"):
        simulate(MAX_COPPER, 0, profile(final_deposit=1), SPEC, p)
    with pytest.raises(ValueError):
        simulate(1.5, 2, zeros, SPEC, p)


@pytest.mark.parametrize(
    "update",
    [
        {"status": "verified"},
        {"lost_deposits": 0.0},
        {"commission_rate": 0.1},
        {"final_deposit": -1},
        {"evidence_at": datetime(2026, 1, 1)},
    ],
)
def test_profiles_cannot_claim_verification_or_coerce_money(update):
    with pytest.raises(ValidationError):
        profile(**update)


def test_profile_market_isolation_and_utc_validation():
    for key, value in [
        ("faction", "horde"),
        ("region", "us"),
        ("game_type", "retail"),
        ("ruleset", "pvp"),
        ("source_id", "a" * 64),
    ]:
        assert (
            simulate(700, 900, profile(**{key: value}), SPEC, DetectorParameters()).reasons[0]
            == "cost_profile_source_mismatch"
        )
    with pytest.raises(ValueError):
        evaluate(1, SPEC, history(), NOW.replace(tzinfo=None))
    for params in [{"window_hours": 10}, {"min_buckets": 57}, {"min_span_hours": 168}]:
        with pytest.raises(ValidationError):
            DetectorParameters(**params)
    regional = FeedSpec(scope="region", dataset="synthetic")
    assert evaluate(1, regional, history(), NOW).reasons == ["incompatible_source_scope"]
