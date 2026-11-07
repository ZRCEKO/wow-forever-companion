"""Fictional inputs verify item filtering without turning objectives into drops."""

from copy import deepcopy

import pytest

from goldmaking.farming_options import find_options


def catalog() -> dict:
    variants = []
    for identifier, geometry, status, item in (
        ("a", "g", "passed_known_checks", 999999),
        ("b", "g", "unknown", 999999),
        ("c", "g2", "excluded", 999999),
        ("d", "g3", "passed_known_checks", 999998),
    ):
        variants.append(
            {
                "variant_id": identifier,
                "geometry_id": geometry,
                "map_resolutions": [],
                "assessment": {"status": status},
                "provenance": [{"file": "fictional.lua", "sha256": "fictional"}],
                "route": {
                    "target_item_ids": [item],
                    "guide_title_literal": "Synthetic",
                    "provider_map_labels": ["Fictional Zone"],
                    "points": [{"x_basis_points": 100, "y_basis_points": 200}],
                    "source_step_lines": [{"line": 10, "text": "Kill fictional enemies"}],
                },
            }
        )
    return {
        "variants": variants,
        "dataset": "synthetic",
        "profile": {"game_type": "forever"},
        "inspected_at": "2026-01-01T00:00:00+00:00",
    }


def test_filter_group_and_preserve_unknown_alternative() -> None:
    source = catalog()
    original = deepcopy(source)
    result = find_options(source, 999999)
    assert source == original
    assert result["candidate_count"] == 1
    assert result["excluded_variant_count"] == 1
    candidate = result["candidates"][0]
    assert len(candidate["alternatives"]) == 2
    assert candidate["drop_rate"] is None
    assert candidate["yield_per_hour"] is None
    assert candidate["eligible_for_recommendation"] is False
    assert candidate["alternatives"][1]["assessment"]["status"] == "unknown"


def test_no_match_does_not_claim_no_acquisition_method_exists() -> None:
    result = find_options(catalog(), 888888)
    assert result["status"] == "no_matching_guide_candidate"
    assert result["candidates"] == []


def test_missing_geometry_is_not_merged_and_limit_is_explicit() -> None:
    source = catalog()
    source["variants"][0]["geometry_id"] = None
    source["variants"][1]["geometry_id"] = None
    result = find_options(source, 999999, 1)
    assert result["candidate_count"] == 2
    assert len(result["candidates"]) == 1
    assert result["truncated"] is True


@pytest.mark.parametrize("item,limit", [(0, 20), (-1, 20), (True, 20), (999999, 0), (999999, 101)])
def test_bad_query_refused(item: int, limit: int) -> None:
    with pytest.raises(ValueError):
        find_options(catalog(), item, limit)
