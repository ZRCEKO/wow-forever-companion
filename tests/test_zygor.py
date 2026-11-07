"""Synthetic guides only; no third-party guide is stored in test fixtures."""

from pathlib import Path

import pytest

from goldmaking.zygor import inspect_zygor, route_candidates

SYNTHETIC = """ZygorGuidesViewer:RegisterGuide("Synthetic Route",{
condition_suggested=function() return unknown_condition() end,
},[[
step
map Synthetic Zone/0
path follow smart; loop on
path 12.34,56.78 90,10
collect 65 Synthetic Herb##999999 |only if synthetic_condition()
step
map Another Synthetic Zone
path 1,2 3,4
collect Another Synthetic Herb##999998
]])
"""


def test_candidates_preserve_context_without_claiming_yield() -> None:
    count, routes, issues = route_candidates(SYNTHETIC)
    assert count == 1
    assert len(routes) == 2
    assert issues == []
    assert routes[0]["points"][0] == {
        "x_basis_points": 1234,
        "y_basis_points": 5678,
        "line": 7,
    }
    assert routes[0]["target_item_ids"] == [999999]
    assert routes[1]["target_item_ids"] == [999998]
    assert routes[0]["duration_seconds"] is None
    assert routes[0]["yield_per_hour"] is None
    assert routes[0]["eligible_for_recommendation"] is False
    assert "synthetic_condition" in routes[0]["source_step_lines"][-1]["text"]


def test_no_cross_guide_or_step_leakage() -> None:
    count, routes, _ = route_candidates(SYNTHETIC + SYNTHETIC.replace("Synthetic Route", "Other"))
    assert count == 2
    assert len(routes) == 4
    assert routes[2]["guide_title_literal"] == "Other"
    assert len(routes[2]["points"]) == 2


def test_invalid_coordinates_and_path_suffix_are_reported() -> None:
    _, routes, _ = route_candidates(SYNTHETIC.replace("12.34,56.78 90,10", "101,1 |only if test()"))
    assert "invalid_coordinate" in routes[0]["issues"]
    assert "unsupported_path_syntax" in routes[0]["issues"]
    assert routes[0]["points"] == []


def test_missing_map_is_not_inherited() -> None:
    _, routes, _ = route_candidates(SYNTHETIC.replace("map Another Synthetic Zone\n", ""))
    assert "ambiguous_or_missing_map" in routes[1]["issues"]
    assert routes[1]["provider_map_labels"] == []


def test_truncated_guide_refused() -> None:
    with pytest.raises(ValueError, match="tronqué"):
        route_candidates(SYNTHETIC.replace("]])", ""))


def test_arbitrary_lua_never_runs(tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    count, routes, _ = route_candidates(f'os.execute("touch {marker}")\n' + SYNTHETIC)
    assert count == 1
    assert len(routes) == 2
    assert not marker.exists()


def test_inventory_local_only_and_trial_separation(tmp_path: Path) -> None:
    (tmp_path / "ZygorGuidesViewerForever.toc").write_text(
        "## Interface: 16001\n## Version: synthetic-test\n", encoding="utf-8"
    )
    for category in ("Gold", "Professions"):
        (tmp_path / "Guides-Forever" / category).mkdir(parents=True)
    for name in ("Synthetic.lua", "SyntheticTrial.lua"):
        (tmp_path / "Guides-Forever" / "Gold" / name).write_text(SYNTHETIC, encoding="utf-8")
    report = inspect_zygor(tmp_path, "synthetic")
    assert report["guide_count"] == 2
    assert report["route_candidate_count"] == 4
    assert report["files"][1]["trial_filename"] is True
    assert report["faction"] is None
    assert report["scan_at"] is None
    assert report["inspected_at"].endswith("+00:00")
    assert len(report["toc_sha256"]) == 64
    with pytest.raises(ValueError, match="Dataset"):
        inspect_zygor(tmp_path, "invalid")


def test_unsupported_registration_is_visible() -> None:
    count, routes, issues = route_candidates("ZygorGuidesViewer:RegisterGuide(variable, {})")
    assert count == 0
    assert routes == []
    assert issues == ["unsupported_registration:1"]
