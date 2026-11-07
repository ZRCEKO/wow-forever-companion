"""Indicateurs vérifiés sur des prix et scans entièrement fictifs."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext

import pytest

from goldmaking.detector import SnapshotInput
from goldmaking.indicators import describe
from goldmaking.tsm import FeedSpec

T = datetime(2026, 11, 6, 12, tzinfo=UTC)
SOURCE = FeedSpec(
    scope="realm",
    realm_slug="fixture-normal-alliance",
    ruleset="normal",
    faction="alliance",
    dataset="synthetic",
)


def point(hours, price=100, *, sid=None, **updates):
    time = T - timedelta(hours=hours)
    return SnapshotInput(
        snapshot_id=sid or f"fixture-{hours}",
        observed_at=time,
        fetched_at=time + timedelta(seconds=1),
        known_at=time + timedelta(seconds=2),
        present=True,
        min_buyout=price,
    ).model_copy(update=updates)


def report(points):
    return describe(2447, SOURCE, points, T + timedelta(minutes=1))


def test_short_history_can_describe_change_but_not_an_opportunity():
    r = report([point(0, 50), point(3, 100), point(6, 200), point(9, 300)])
    assert r.signal_state == "not_calculable" and r.signal_reasons == ["insufficient_history"]
    assert r.previous_scan_change.absolute_copper == -50
    assert r.previous_scan_change.relative == Decimal("-0.5")
    assert r.previous_scan_change.actual_elapsed_seconds == 3 * 3600
    assert r.change_24h.reason == "no_baseline" and r.change_24h.relative is None
    assert r.distribution.state == "calculable"
    assert (r.distribution.q25, r.distribution.median, r.distribution.q75) == (100, 200, 300)
    assert r.distribution.relative_iqr == Decimal("1")
    assert r.distribution.sample_count == 3
    assert r.coverage.valid_buckets == 3 and r.coverage.meets_history_requirements is False
    assert r.reference_deviation.median == 200
    assert r.reference_deviation.absolute_copper == -150
    assert r.reference_deviation.relative == Decimal("-0.75")
    assert r.reference_deviation.quartile_position == "below_q25"
    assert r.anomaly.flagged is None and r.anomaly.reason == "insufficient_history"


@pytest.mark.parametrize(
    "hours,expected", [(24, "calculable"), (27, "calculable"), (27.001, "not_calculable")]
)
def test_24h_baseline_tolerance_and_actual_duration(hours, expected):
    r = report([point(0, 110), point(hours, 100)]).change_24h
    assert r.state == expected
    assert r.actual_elapsed_seconds == int(hours * 3600)
    if expected == "calculable":
        assert r.relative == Decimal("0.1") and r.absolute_copper == 10
    else:
        assert r.reason == "baseline_too_old" and r.relative is None


def test_scan_after_target_is_not_used_and_bucket_supersession_does_not_hide_baseline():
    r = report([point(0, 110), point(23, 1), point(24, 100)])
    assert r.change_24h.baseline_snapshot_id == "fixture-24"
    assert r.change_24h.relative == Decimal("0.1")
    assert r.previous_scan_change.baseline_snapshot_id == "fixture-23"


@pytest.mark.parametrize(
    "update,reason",
    [
        ({"present": False, "min_buyout": None}, "baseline_item_missing"),
        ({"min_buyout": None}, "baseline_price_missing"),
        ({"min_buyout": 0}, "baseline_price_zero"),
    ],
)
def test_nearest_invalid_baseline_is_never_replaced_by_an_older_price(update, reason):
    r = report([point(0, 110), point(24, **update), point(25, 100)])
    assert r.change_24h.reason == reason and r.previous_scan_change.reason == reason
    assert r.change_24h.relative is None
    assert r.change_24h.baseline_snapshot_id == "fixture-24"


@pytest.mark.parametrize(
    "update,reason",
    [
        ({"present": False, "min_buyout": None}, "missing_in_latest_snapshot"),
        ({"min_buyout": None}, "current_price_missing"),
        ({"min_buyout": 0}, "current_price_zero"),
        ({"observed_at": None}, "unknown_current_scan"),
    ],
)
def test_current_invalid_blocks_metrics_and_coverage_is_unknown(update, reason):
    current = point(0, **update)
    r = report([current, point(24, 100)])
    assert reason in r.signal_reasons
    assert r.previous_scan_change.relative is None
    assert r.distribution.sample_count is None
    assert r.coverage.valid_buckets is None and r.coverage.meets_history_requirements is None
    assert r.reference_deviation.reason == reason and r.reference_deviation.relative is None
    assert r.anomaly.reason == reason and r.anomaly.flagged is None
    assert r.anomaly.sample_count is None


def test_stale_scan_does_not_appear_fresh_after_recent_download():
    r = report([point(7, fetched_at=T, known_at=T), point(31, 90)])
    assert r.previous_scan_change.reason == "stale_scan"
    assert r.age_seconds == 7 * 3600 + 60


def test_later_imports_do_not_change_historical_result():
    points = [point(0, 110), point(24, 100)]
    expected = report(points)
    late = point(24, 1, sid="fixture-late", known_at=T + timedelta(hours=1))
    assert report([late, *reversed(points)]) == expected
    correction = point(24, 200, sid="fixture-correction", known_at=T - timedelta(hours=1))
    r = report([*points, correction])
    assert r.change_24h.relative == Decimal("-0.45")
    assert r.exclusion_counts["superseded_correction"] == 1


def test_distribution_counts_one_sample_per_bucket_and_excludes_current():
    r = report([point(0, 99999), point(1, 100), point(2, 1), point(4, 200), point(7, 300)])
    assert r.distribution.sample_count == 3
    assert r.distribution.median == 200 and r.distribution.maximum == 300
    assert r.previous_scan_change.baseline_snapshot_id == "fixture-1"
    short = report([point(0), point(3), point(6)]).distribution
    assert short.state == "not_calculable" and short.sample_count == 2
    assert short.median is None


def test_zero_change_is_a_real_zero_and_gap_is_not_normalized():
    r = report([point(0), point(5.5)])
    assert r.previous_scan_change.relative == Decimal(0)
    assert r.previous_scan_change.absolute_copper == 0
    assert r.previous_scan_change.actual_elapsed_seconds == 19800


def test_no_scan_and_naive_as_of():
    r = report([])
    assert r.current_price is None and r.previous_scan_change.reason == "no_known_snapshot"
    assert r.coverage.valid_buckets is None
    with pytest.raises(ValueError, match="fuseau"):
        describe(2447, SOURCE, [], T.replace(tzinfo=None))


def test_volatility_manual_alternating_returns_reference():
    # Prix fictifs 100→200→100 répétés : retours +1/-0,5 quatre fois.
    # Moyenne 0,25 ; somme des carrés centrés 4,5 ; variance = 4,5/7 = 9/14.
    r = report([point(i * 3, 100 if i % 2 == 0 else 200) for i in range(9)])
    v = r.volatility
    assert v.state == "calculable" and v.reason is None
    assert v.return_count == 8 and v.span_seconds == 86400
    assert v.mean_relative_return == Decimal("0.25")
    with localcontext() as context:
        context.prec = 50
        expected_variance = Decimal(9) / Decimal(14)
        assert v.sample_variance == expected_variance
        assert v.stddev_relative_return == expected_variance.sqrt()
    assert [i.relative_return for i in v.intervals] == [Decimal(1), Decimal("-0.5")] * 4
    assert v.snapshot_ids[0] == "fixture-24" and v.snapshot_ids[-1] == "fixture-0"


def test_volatility_constant_returns_are_real_zero_not_missing():
    prices = [100_000_000]
    for _ in range(8):
        prices.append(prices[-1] * 11 // 10)
    v = report([point((8 - i) * 3, price) for i, price in enumerate(prices)]).volatility
    assert v.state == "calculable"
    assert v.mean_relative_return == Decimal("0.1")
    assert v.sample_variance == 0 and v.stddev_relative_return == 0


def test_volatility_requires_count_and_real_span_separately():
    short = report([point(i * 3) for i in range(8)]).volatility
    assert short.return_count == 7 and short.reason == "insufficient_consecutive_history"
    compressed = report([*(point(i * 3) for i in range(8)), point(23.9)]).volatility
    assert compressed.return_count == 8 and compressed.span_seconds == 86040
    assert compressed.reason == "insufficient_time_span"
    assert compressed.stddev_relative_return is None


def test_volatility_does_not_bridge_gaps_or_use_an_older_long_chain():
    points = [point(0), point(3), *(point(i * 3) for i in range(3, 17))]
    v = report(points).volatility
    assert v.return_count == 1 and v.span_seconds == 10800
    assert v.snapshot_ids == ["fixture-3", "fixture-0"]
    assert v.stddev_relative_return is None


@pytest.mark.parametrize(
    "updates",
    [
        {"min_buyout": 0},
        {"min_buyout": None},
        {"present": False, "min_buyout": None},
    ],
)
def test_invalid_last_scan_in_bucket_breaks_volatility_chain(updates):
    points = [point(i * 3) for i in range(17)]
    points.append(point(5.5, **updates))
    v = report(points).volatility
    assert v.return_count == 1
    assert "fixture-6" not in v.snapshot_ids


def test_irregular_intervals_are_reported_without_normalization():
    points = [point(i * 3) for i in range(10) if i != 1] + [point(2.9)]
    v = report(points).volatility
    assert v.state == "calculable" and v.return_count == 9
    assert v.minimum_interval_seconds == 10440
    assert v.maximum_interval_seconds == 11160
    assert v.span_seconds == 27 * 3600 and v.stddev_relative_return == 0


def test_volatility_replay_is_stable_and_unknown_context_has_no_false_counts():
    points = [point(i * 3) for i in range(10)]
    first = report(points)
    late = point(3, 1, sid="fixture-late", known_at=T + timedelta(hours=1))
    assert report([*points, late]) == first
    corrected = point(3, 200, sid="fixture-correction", known_at=T + timedelta(seconds=3))
    assert report([*points, corrected]).volatility.stddev_relative_return > 0
    blocked = report([point(0, 0), *points[1:]]).volatility
    assert blocked.reason == "current_price_zero" and blocked.return_count is None
    assert report([]).volatility.stddev_relative_return is None


def reference_points(current_price, low=100, high=120, count=56):
    # Historique entièrement fictif, chaque prix apparaît dans la moitié des créneaux.
    return [point(0, current_price)] + [
        point(i * 3, low if i % 2 else high) for i in range(1, count + 1)
    ]


@pytest.mark.parametrize(
    "price,position,flagged,quartile_position",
    [
        (69, "below_fence", True, "below_q25"),
        (70, "within_fences", False, "below_q25"),
        (100, "within_fences", False, "within_quartiles"),
        (120, "within_fences", False, "within_quartiles"),
        (150, "within_fences", False, "above_q75"),
        (151, "above_fence", True, "above_q75"),
    ],
)
def test_reference_comparison_and_iqr_fences_manual_boundaries(
    price, position, flagged, quartile_position
):
    r = report(reference_points(price))
    d, a = r.reference_deviation, r.anomaly
    assert d.state == "calculable" and d.median == 100
    assert d.absolute_copper == price - 100
    assert d.relative == Decimal(price - 100) / Decimal(100)
    assert d.quartile_position == quartile_position
    assert a.state == "calculable" and a.reason is None
    assert a.iqr_copper == 20
    assert a.lower_inclusive_copper == 70 and a.upper_inclusive_copper == 150
    assert a.position == position and a.flagged is flagged
    assert a.sample_count == 56 and a.span_seconds == 165 * 3600
    assert r.distribution.minimum == 100 and r.distribution.maximum == 120
    if price == 100:
        assert d.absolute_copper == 0 and d.relative == 0


@pytest.mark.parametrize("price,flagged", [(98, True), (99, False), (102, False), (103, True)])
def test_odd_iqr_uses_exact_integer_inclusive_bounds(price, flagged):
    # Q25=100, Q75=101 : bornes théoriques 98,5 et 102,5, entiers inclusifs 99..102.
    a = report(reference_points(price, high=101)).anomaly
    assert a.lower_inclusive_copper == 99 and a.upper_inclusive_copper == 102
    assert a.flagged is flagged


def test_anomaly_requires_sample_count_and_span_and_does_not_fill_a_missing_bucket():
    points = reference_points(50, count=40)
    assert report(points).anomaly.reason == "insufficient_time_span"  # 117 h
    points[-1] = point(123, 120)  # 120 h, exactement le minimum
    assert report(points).anomaly.state == "calculable"
    points.append(point(5.5, 0))  # invalide le dernier scan du créneau de 6 h
    r = report(points)
    assert r.anomaly.sample_count == 39 and r.anomaly.reason == "insufficient_history"
    assert r.anomaly.flagged is None and r.anomaly.lower_inclusive_copper is None
    assert r.reference_deviation.state == "calculable"


def test_zero_iqr_is_inconclusive_and_positive_prices_can_have_zero_lower_bound():
    r = report(reference_points(50, high=100))
    assert r.anomaly.reason == "zero_reference_iqr" and r.anomaly.iqr_copper == 0
    assert r.anomaly.flagged is None and r.anomaly.lower_inclusive_copper is None
    assert r.reference_deviation.relative == Decimal("-0.5")
    broad = report(reference_points(1, low=10, high=100)).anomaly
    assert broad.lower_inclusive_copper == 0 and broad.flagged is False


def test_reference_comparison_uses_integer_copper_beyond_float_precision():
    low = 2**53 + 7
    r = report(reference_points(low - 2, low=low, high=low + 1))
    assert r.reference_deviation.median == low
    assert r.reference_deviation.absolute_copper == -2
    assert r.anomaly.lower_inclusive_copper == low - 1
    assert r.anomaly.flagged is True


def test_reference_comparison_replay_and_degenerate_history_are_explicit():
    points = reference_points(50)
    expected = report(points)
    correction = point(3, 1, sid="fixture-late-reference", known_at=T + timedelta(hours=1))
    assert report([*reversed(points), correction]) == expected
    short = report([point(0), point(3), point(6)])
    assert short.reference_deviation.reason == "insufficient_samples"
    assert short.reference_deviation.absolute_copper is None and short.anomaly.flagged is None
