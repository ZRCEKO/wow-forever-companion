"""Static inventory and route candidates from local Zygor guide files.

This is deliberately not a Lua interpreter or a Zygor condition evaluator.
All extracted routes remain ineligible for recommendations pending review.
"""

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from goldmaking.gathering import read_bounded

MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 200
REGISTER = re.compile(r'^ZygorGuidesViewer:RegisterGuide\("((?:[^"\\]|\\.)*)",\{\s*$')
PAIR = re.compile(r"([0-9]{1,3}(?:\.[0-9]{1,2})?),([0-9]{1,3}(?:\.[0-9]{1,2})?)")


def route_candidates(source: str) -> tuple[int, list[dict[str, Any]], list[str]]:
    """Inventory the observed literal guide dialect, retaining source line numbers."""
    guides = 0
    candidates: list[dict[str, Any]] = []
    issues: list[str] = []
    title: str | None = None
    in_body = False
    start = 0
    step: list[tuple[int, str]] = []
    header: list[dict[str, Any]] = []
    source_lines = source.splitlines()
    guide_candidate_start = 0

    def finish_step() -> None:
        if not any(re.match(r"path\s+[0-9]", line) for _, line in step):
            return
        maps = [line[4:].strip() for _, line in step if line.startswith("map ")]
        points = []
        problems = ["conditions_not_evaluated", "provider_compatibility_unverified"]
        for number, line in step:
            if not re.match(r"path\s+[0-9]", line):
                continue
            text = line[4:].strip()
            pairs = list(PAIR.finditer(text))
            if PAIR.sub("", text).strip():
                problems.append("unsupported_path_syntax")
            for pair in pairs:
                x, y = Decimal(pair[1]), Decimal(pair[2])
                if not (0 <= x <= 100 and 0 <= y <= 100):
                    problems.append("invalid_coordinate")
                    continue
                points.append(
                    {"x_basis_points": int(x * 100), "y_basis_points": int(y * 100), "line": number}
                )
        if len(maps) != 1:
            problems.append("ambiguous_or_missing_map")
        item_ids = sorted(
            {
                int(match[1])
                for _, line in step
                if line.startswith("collect ")
                for match in re.finditer(r"##([1-9][0-9]*)\b", line)
            }
        )
        candidates.append(
            {
                "guide_title_literal": title,
                "guide_start_line": start,
                "source_header_lines": list(header),
                "step_start_line": step[0][0],
                "provider_map_labels": maps,
                "points": points,
                "target_item_ids": item_ids,
                "collect_semantics": "guide_objective_not_measured_yield",
                "source_step_lines": [{"line": n, "text": text} for n, text in step],
                "eligible_for_recommendation": False,
                "issues": sorted(set(problems)),
                "duration_seconds": None,
                "yield_per_hour": None,
            }
        )

    for number, raw in enumerate(source_lines, 1):
        line = raw.strip()
        if not in_body:
            match = REGISTER.fullmatch(line)
            if match:
                guides += 1
                if title is not None:
                    issues.append(f"guide_without_supported_body:{start}")
                title, start = match[1], number
                guide_candidate_start = len(candidates)
                header = [{"line": number, "text": line}]
            elif "RegisterGuide(" in line and not line.startswith("--"):
                issues.append(f"unsupported_registration:{number}")
            elif line == "},[[" and title is not None:
                in_body = True
                step = []
            elif title is not None:
                header.append({"line": number, "text": line})
        elif line == "]])":
            finish_step()
            guide_hash = hashlib.sha256(
                "\n".join(source_lines[start - 1 : number]).encode()
            ).hexdigest()
            for candidate in candidates[guide_candidate_start:]:
                candidate["source_guide_sha256"] = guide_hash
                candidate["step_relative_line"] = candidate["step_start_line"] - start
            in_body = False
            title = None
            step = []
        elif line == "step":
            finish_step()
            step = [(number, line)]
        else:
            step.append((number, line))
    if in_body or title is not None:
        raise ValueError("Guide tronqué ou délimitation non prise en charge")
    return guides, candidates, issues


def inspect_zygor(root: Path, dataset: str) -> dict[str, Any]:
    if dataset not in {"real", "synthetic"}:
        raise ValueError("Dataset explicite requis")
    toc = read_bounded(root / "ZygorGuidesViewerForever.toc")
    metadata = dict(
        re.findall(r"^## (Interface|Version):\s*(.+)$", toc.decode("utf-8-sig"), re.MULTILINE)
    )
    if metadata.get("Interface", "").strip() != "16001":
        raise ValueError("Interface Zygor Forever non homologuée")
    if not metadata.get("Version", "").strip():
        raise ValueError("Version addon manquante")
    files: list[Path] = []
    for category in ("Gold", "Professions"):
        directory = root / "Guides-Forever" / category
        if not directory.is_dir():
            raise ValueError(f"Dossier {category} manquant")
        for path in directory.glob("*.lua"):
            files.append(path)
            if len(files) > MAX_FILES:
                raise ValueError("Trop de fichiers")
    reports = []
    total = 0
    for path in sorted(files):
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Fichier hors du dossier addon")
        content = read_bounded(path)
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Paquet trop volumineux")
        source = content.decode("utf-8-sig")
        count, candidates, issues = route_candidates(source)
        prefix = source.split("ZygorGuidesViewer:RegisterGuide", 1)[0]
        guards = re.findall(
            r'^if UnitFactionGroup\("player"\)~="(Alliance|Horde)" then return end\s*$',
            prefix,
            re.MULTILINE,
        )
        faction = guards[0].lower() if len(guards) == 1 else None
        reports.append(
            {
                "file": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "guide_count": count,
                "route_candidate_count": len(candidates),
                "trial_filename": "Trial" in path.name,
                "declared_faction": faction,
                "source_preamble": prefix,
                "issues": issues,
                "routes": candidates,
            }
        )
    return {
        "schema_version": "zygor-route-candidates-v2",
        "dataset": dataset,
        "provider": "zygor",
        "game_type": "forever",
        "status": "staging_only",
        "addon_version": metadata["Version"].strip(),
        "toc_sha256": hashlib.sha256(toc).hexdigest(),
        "inspected_at": datetime.now(UTC).isoformat(),
        "scan_at": None,
        "region": None,
        "ruleset": None,
        "faction": None,
        "guide_count": sum(f["guide_count"] for f in reports),
        "route_candidate_count": sum(f["route_candidate_count"] for f in reports),
        "limitations": [
            "lexical_candidates_only",
            "includes_not_expanded",
            "conditions_not_evaluated",
            "duplicates_not_merged",
            "autoload_not_evaluated",
            "no_yield_estimates",
        ],
        "files": reports,
    }
