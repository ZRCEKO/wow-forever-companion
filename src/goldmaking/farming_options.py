"""Item-centric access to provider-declared farming candidates, not drop tables."""

from typing import Any


def find_options(catalog: dict[str, Any], item_id: int, limit: int = 20) -> dict[str, Any]:
    if type(item_id) is not int or item_id <= 0 or item_id > 2**31 - 1:
        raise ValueError("Identifiant d'objet invalide")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Limite attendue entre 1 et 100")
    groups: dict[str, dict[str, Any]] = {}
    excluded = 0
    for variant in catalog["variants"]:
        route = variant["route"]
        if item_id not in route["target_item_ids"]:
            continue
        if variant["assessment"]["status"] == "excluded":
            excluded += 1
            continue
        # Preserve ungroupable routes as independent entries; never merge missing maps.
        key = variant["geometry_id"] or variant["variant_id"]
        if key not in groups:
            groups[key] = {
                "candidate_id": key,
                "map_resolutions": variant["map_resolutions"],
                "provider_map_labels": route["provider_map_labels"],
                "points": [
                    {"x_basis_points": p["x_basis_points"], "y_basis_points": p["y_basis_points"]}
                    for p in route["points"]
                ],
                "alternatives": [],
                "eligible_for_recommendation": False,
                "drop_rate": None,
                "respawn_seconds": None,
                "duration_seconds": None,
                "yield_per_hour": None,
            }
        # Keep hints and checks on each variant: a shared path can serve different methods.
        hints = [
            entry
            for entry in route["source_step_lines"]
            if entry["text"]
            .lower()
            .startswith(("kill ", "click ", "collect ", "|tip farm", "|tip create"))
        ]
        groups[key]["alternatives"].append(
            {
                "variant_id": variant["variant_id"],
                "guide_title": route["guide_title_literal"],
                "assessment": variant["assessment"],
                "provider_hints": hints,
                "association_kind": "guide_step_objective_not_verified_loot_relation",
                "provenance": variant["provenance"],
            }
        )
    rows = sorted(groups.values(), key=lambda row: row["candidate_id"])
    return {
        "schema_version": "farming-options-v1",
        "dataset": catalog["dataset"],
        "game_type": catalog["profile"]["game_type"],
        "item_id": item_id,
        "profile": catalog["profile"],
        "inspected_at": catalog["inspected_at"],
        "status": "candidates_found" if rows else "no_matching_guide_candidate",
        "candidate_count": len(rows),
        "excluded_variant_count": excluded,
        "limit": limit,
        "truncated": len(rows) > limit,
        "candidates": rows[:limit],
        "limitations": [
            "provider_claims_not_game_verified",
            "partial_profile_checks",
            "unknown_loot_rates_and_durations",
            "not_an_exhaustive_acquisition_catalog",
            "no_economic_ranking",
        ],
    }
