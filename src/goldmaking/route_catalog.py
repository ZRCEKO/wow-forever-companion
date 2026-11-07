"""Conservative source deduplication and partial profile matching of Zygor routes."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from goldmaking.gathering import read_bounded
from goldmaking.map_evidence import parse_map_table, resolve_map
from goldmaking.zygor import inspect_zygor


class CharacterProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    game_type: Literal["forever"] = "forever"
    region: Literal["eu"] = "eu"
    ruleset: Literal["normal"] = "normal"
    faction: Literal["alliance", "horde"] = "alliance"
    race: str | None = None
    character_class: str | None = None
    specialization: str | None = None
    level: int | None = Field(default=None, ge=1, le=100)
    skills: dict[str, int] = Field(default_factory=dict)
    planned_profession_change: str | None = None


def validate_profile(profile: CharacterProfile) -> None:
    if len({key.casefold() for key in profile.skills}) != len(profile.skills):
        raise ValueError("Compétence dupliquée")
    if any(
        not key or type(value) is not int or not 0 <= value <= 1000
        for key, value in profile.skills.items()
    ):
        raise ValueError("Compétence invalide")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def evaluate_expression(expression: str, profile: CharacterProfile) -> bool | None:
    """Small, closed grammar. Unknown expressions never execute or become true."""
    expression = expression.strip()
    skills = {key.casefold(): value for key, value in profile.skills.items()}
    match = re.fullmatch(
        r"""skill\((["'])([A-Za-z ]+)\1\)\s*(>=|<=|>|<|==)\s*([0-9]+)""", expression
    )
    if match:
        value = skills.get(match[2].casefold())
        if value is None:
            return None
        threshold = int(match[4])
        return {
            ">=": value >= threshold,
            "<=": value <= threshold,
            ">": value > threshold,
            "<": value < threshold,
            "==": value == threshold,
        }[match[3]]
    if expression in {"Gnome", "Human", "Dwarf", "NightElf"}:
        return None if profile.race is None else profile.race.casefold() == expression.casefold()
    if expression in {
        "Mage",
        "Warrior",
        "Rogue",
        "Priest",
        "Warlock",
        "Hunter",
        "Druid",
        "Paladin",
        "Shaman",
    }:
        return (
            None
            if profile.character_class is None
            else profile.character_class.casefold() == expression.casefold()
        )
    return None


def assess(route: dict[str, Any], faction: str | None, profile: CharacterProfile) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    unresolved: list[str] = []
    checks.append(
        {
            "kind": "faction",
            "required": faction,
            "result": None if faction is None else profile.faction == faction,
        }
    )
    skills = {key.casefold(): value for key, value in profile.skills.items()}
    for entry in route["source_header_lines"][1:]:
        text = entry["text"]
        if text.startswith("meta="):
            match = re.fullmatch(
                r'meta=\{goldtype="route",(?:skillreq=\{([a-z]+)=([0-9]+)\},)?levelreq=([0-9]+)\},',
                text,
            )
            if not match:
                unresolved.append(text)
                continue
            minimum = int(match[3])
            checks.append(
                {
                    "kind": "minimum_level",
                    "required": minimum,
                    "line": entry["line"],
                    "result": None if profile.level is None else profile.level >= minimum,
                }
            )
            if match[1]:
                value = skills.get(match[1])
                checks.append(
                    {
                        "kind": "minimum_skill",
                        "skill": match[1],
                        "required": int(match[2]),
                        "line": entry["line"],
                        "result": None if value is None else value >= int(match[2]),
                    }
                )
        elif "condition" in text:
            match = re.fullmatch(r"condition_suggested=function\(\) return (.+) end,", text)
            checks.append(
                {
                    "kind": "guide_suggestion",
                    "expression": text,
                    "line": entry["line"],
                    "result": None if not match else evaluate_expression(match[1], profile),
                }
            )
    for entry in route["source_step_lines"]:
        text = entry["text"]
        # Display and completion conditions are reported individually, never conjoined
        # into route prerequisites: `|or` and guide state have different semantics.
        for kind, marker in (("line_visibility", "|only if "), ("step_completion", "|complete ")):
            if marker in text:
                expression = text.split(marker, 1)[1].split("|", 1)[0].strip()
                checks.append(
                    {
                        "kind": kind,
                        "expression": expression,
                        "line": entry["line"],
                        "result": evaluate_expression(expression, profile),
                    }
                )
        if "include " in text or text.startswith("only "):
            unresolved.append(text)
    required = [
        c["result"] for c in checks if c["kind"] in {"faction", "minimum_level", "minimum_skill"}
    ]
    status = (
        "excluded"
        if False in required
        else "unknown"
        if None in required
        else "passed_known_checks"
    )
    return {
        "status": status,
        "checks": checks,
        "unresolved": unresolved,
        "eligible_for_recommendation": False,
        "limitations": [
            "partial_conditions_only",
            "guide_state_unresolved",
            "compatibility_unverified",
            "no_duration_or_yield",
        ],
    }


def build_catalog(root: Path, dataset: str, profile: CharacterProfile) -> dict[str, Any]:
    validate_profile(profile)
    report = inspect_zygor(root, dataset)
    map_path = root / "Libs-Forever" / "LibRover-1.0" / "data.lua"
    map_data = None
    if map_path.is_file():
        if not map_path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Table de cartes hors du dossier addon")
        map_data = parse_map_table(read_bounded(map_path))
    variants: dict[str, dict[str, Any]] = {}
    for file in report["files"]:
        for route in file["routes"]:
            # Exact texts and context are part of identity. Matching geometry alone
            # does not merge targets, conditions, factions, or different instructions.
            identity = {
                "dataset": dataset,
                "game_type": report["game_type"],
                "faction": file["declared_faction"],
                "header": [x["text"] for x in route["source_header_lines"]],
                "step": [x["text"] for x in route["source_step_lines"]],
                "preamble": file["source_preamble"],
                "guide_sha256": route["source_guide_sha256"],
                "step_relative_line": route["step_relative_line"],
            }
            key = digest(identity)
            geometry = {
                "game_type": report["game_type"],
                "dataset": dataset,
                "maps": route["provider_map_labels"],
                "points": [[p["x_basis_points"], p["y_basis_points"]] for p in route["points"]],
                "path_options": [
                    x["text"]
                    for x in route["source_step_lines"]
                    if x["text"].startswith("path follow")
                ],
            }
            evidence = {
                "file": file["file"],
                "sha256": file["sha256"],
                "guide_start_line": route["guide_start_line"],
                "step_start_line": route["step_start_line"],
                "trial": file["trial_filename"],
            }
            if key not in variants:
                valid_geometry = (
                    len(route["provider_map_labels"]) == 1
                    and len(route["points"]) >= 2
                    and not any(
                        i in route["issues"]
                        for i in ("invalid_coordinate", "unsupported_path_syntax")
                    )
                )
                variants[key] = {
                    "variant_id": key,
                    "geometry_id": digest(geometry) if valid_geometry else None,
                    "declared_faction": file["declared_faction"],
                    "route": route,
                    "map_resolutions": [
                        resolve_map(label, map_data) for label in route["provider_map_labels"]
                    ]
                    if map_data is not None
                    else [],
                    "assessment": assess(route, file["declared_faction"], profile),
                    "provenance": [],
                }
            variants[key]["provenance"].append(evidence)
    rows = sorted(variants.values(), key=lambda row: row["variant_id"])
    groups: dict[str, list[str]] = {}
    for row in rows:
        if row["geometry_id"] is not None:
            groups.setdefault(row["geometry_id"], []).append(row["variant_id"])
    return {
        "schema_version": "zygor-profile-catalog-v2",
        "map_source": None
        if map_data is None
        else {
            "file": map_path.relative_to(root).as_posix(),
            "sha256": map_data["sha256"],
            "unsupported_lines": map_data["unsupported_lines"],
        },
        "dataset": dataset,
        "status": "staging_only",
        "inspected_at": report["inspected_at"],
        "addon_version": report["addon_version"],
        "toc_sha256": report["toc_sha256"],
        "profile": profile.model_dump(),
        "candidate_count": report["route_candidate_count"],
        "variant_count": len(rows),
        "geometry_group_count": len(groups),
        "ungrouped_variant_count": sum(r["geometry_id"] is None for r in rows),
        "geometry_groups": [
            {"geometry_id": key, "variant_ids": values} for key, values in sorted(groups.items())
        ],
        "assessment_counts": {
            s: sum(r["assessment"]["status"] == s for r in rows)
            for s in ("excluded", "unknown", "passed_known_checks")
        },
        "variants": rows,
    }
