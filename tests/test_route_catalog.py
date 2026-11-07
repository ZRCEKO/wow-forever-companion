"""Fully fictional source data for profile checks and deduplication."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from goldmaking.route_catalog import (
    CharacterProfile,
    assess,
    build_catalog,
    evaluate_expression,
    validate_profile,
)
from goldmaking.zygor import route_candidates

SYNTHETIC = """ZygorGuidesViewer:RegisterGuide("Synthetic",{
meta={goldtype="route",skillreq={herbalism=10},levelreq=13},
condition_suggested=function() return skill('Tailoring') > 0 end,
},[[
step
map Fictional Zone/0
path follow smart; loop on
path 10,20 30,40
collect 70 Fictional Plant##999999 |only if Gnome
'|complete skill("Tailoring") >= 50 |or
]])
"""


def profile() -> CharacterProfile:
    return CharacterProfile(
        level=13, race="Gnome", character_class="Mage", skills={"herbalism": 10, "tailoring": 50}
    )


def test_minimum_thresholds_and_completion_are_separate() -> None:
    _, routes, _ = route_candidates(SYNTHETIC)
    result = assess(routes[0], "alliance", profile())
    assert result["status"] == "passed_known_checks"
    assert result["eligible_for_recommendation"] is False
    assert next(c for c in result["checks"] if c["kind"] == "step_completion")["result"] is True
    assert assess(routes[0], "horde", profile())["status"] == "excluded"
    assert assess(routes[0], None, profile())["status"] == "unknown"


@pytest.mark.parametrize(
    "skills,level,expected",
    [
        ({"herbalism": 9}, 13, "excluded"),
        ({"herbalism": 10}, 12, "excluded"),
        ({}, 13, "unknown"),
        ({"herbalism": 0}, 13, "excluded"),
        ({"herbalism": 10}, None, "unknown"),
    ],
)
def test_unknown_is_distinct_from_zero(
    skills: dict[str, int], level: int | None, expected: str
) -> None:
    _, routes, _ = route_candidates(SYNTHETIC)
    assert (
        assess(routes[0], "alliance", CharacterProfile(skills=skills, level=level))["status"]
        == expected
    )


def test_expression_is_closed_and_has_no_fallback_to_true() -> None:
    p = profile()
    assert evaluate_expression("Gnome", p) is True
    assert evaluate_expression("Human", p) is False
    assert evaluate_expression("Mage", p) is True
    assert evaluate_expression("skill('Enchanting') > 0", p) is None
    assert evaluate_expression("Gnome or Human", p) is None
    assert evaluate_expression("os.execute('anything')", p) is None
    assert evaluate_expression("Gnome", CharacterProfile()) is None


def test_planned_profession_does_not_replace_current() -> None:
    p = profile().model_copy(update={"planned_profession_change": "herbalism -> enchanting"})
    assert evaluate_expression("skill('Herbalism') >= 10", p) is True
    assert evaluate_expression("skill('Enchanting') >= 1", p) is None


def test_profile_validation() -> None:
    with pytest.raises(ValidationError):
        CharacterProfile(level=True)
    with pytest.raises(ValidationError):
        CharacterProfile(skills={"herbalism": "10"})
    with pytest.raises(ValueError):
        validate_profile(CharacterProfile(skills={"herbalism": -1}))
    with pytest.raises(ValueError):
        validate_profile(CharacterProfile(skills={"herbalism": 10, "Herbalism": 20}))


def addon(root: Path) -> Path:
    (root / "ZygorGuidesViewerForever.toc").write_text(
        "## Interface: 16001\n## Version: synthetic\n"
    )
    for category in ("Gold", "Professions"):
        (root / "Guides-Forever" / category).mkdir(parents=True)
    return root / "Guides-Forever" / "Professions"


def test_exact_copies_merge_with_both_provenances(tmp_path: Path) -> None:
    directory = addon(tmp_path)
    source = 'if UnitFactionGroup("player")~="Alliance" then return end\n' + SYNTHETIC
    (directory / "One.lua").write_text(source)
    (directory / "OneTrial.lua").write_text(source)
    r = build_catalog(tmp_path, "synthetic", profile())
    assert r["candidate_count"] == 2
    assert r["variant_count"] == 1
    assert len(r["variants"][0]["provenance"]) == 2
    assert r["geometry_group_count"] == 1
    assert r["variants"][0]["declared_faction"] == "alliance"


def test_geometry_groups_do_not_merge_different_factions_or_targets(tmp_path: Path) -> None:
    directory = addon(tmp_path)
    for faction, item in (("Alliance", "999999"), ("Horde", "999998")):
        (directory / f"{faction}.lua").write_text(
            f'if UnitFactionGroup("player")~="{faction}" then return end\n'
            + SYNTHETIC.replace("999999", item)
        )
    r = build_catalog(tmp_path, "synthetic", profile())
    assert r["variant_count"] == 2
    assert r["geometry_group_count"] == 1
    assert r["assessment_counts"]["excluded"] == 1


def test_no_faction_inferred_from_filename_and_no_ambiguous_geometry(tmp_path: Path) -> None:
    directory = addon(tmp_path)
    (directory / "Alliance.lua").write_text(SYNTHETIC.replace("map Fictional Zone/0\n", ""))
    r = build_catalog(tmp_path, "synthetic", profile())
    assert r["variants"][0]["declared_faction"] is None
    assert r["geometry_group_count"] == 0
    assert r["ungrouped_variant_count"] == 1


def test_identity_preserves_prior_guide_context(tmp_path: Path) -> None:
    directory = addon(tmp_path)
    (directory / "One.lua").write_text(SYNTHETIC)
    (directory / "Two.lua").write_text(SYNTHETIC.replace("},[[\n", "},[[\nstep\nonly if Mage\n"))
    r = build_catalog(tmp_path, "synthetic", profile())
    assert r["variant_count"] == 2
    assert r["geometry_group_count"] == 1
