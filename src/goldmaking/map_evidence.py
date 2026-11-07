"""Read explicit provider map assignments; do not infer world-map equivalence."""

import hashlib
import re
from typing import Any


def parse_map_table(content: bytes) -> dict[str, Any]:
    lines = content.decode("utf-8-sig").splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "data.MapIDsByName = {"]
    if len(starts) != 1:
        raise ValueError("Table de cartes absente ou ambiguë")
    rows: dict[str, list[dict[str, Any]]] = {}
    unsupported = []
    closed = False
    for index in range(starts[0] + 1, len(lines)):
        text = lines[index].strip()
        if text == "}":
            closed = True
            break
        if not text or text.startswith("--"):
            continue
        match = re.fullmatch(r'\["([^"\\]+)"\]\s*=\s*\{(.+)\},?', text)
        if not match:
            unsupported.append(index + 1)
            continue
        name, body = match.groups()
        # Instance records use provider-created maps, not public UIMap identifiers.
        if re.search(r"\binstance\s*=", body):
            rows.setdefault(name, []).append(
                {"status": "provider_synthetic_instance", "line": index + 1, "floors": {}}
            )
            continue
        floors: dict[str, int] = {}
        valid = True
        for entry in body.split(","):
            if not entry.strip():
                continue
            pair = re.fullmatch(r"\s*\[([0-9]+)\]\s*=\s*([1-9][0-9]*)\s*", entry)
            if pair is None or pair[1] in floors:
                valid = False
                break
            floors[pair[1]] = int(pair[2])
        rows.setdefault(name, []).append(
            {
                "status": "explicit_provider_mapping" if valid and floors else "unsupported",
                "line": index + 1,
                "floors": floors if valid else {},
            }
        )
    if not closed:
        raise ValueError("Table de cartes tronquée")
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "rows": rows,
        "unsupported_lines": unsupported,
    }


def resolve_map(label: str, evidence: dict[str, Any]) -> dict[str, Any]:
    match = re.fullmatch(r"(.+?)(?:/([0-9]+))?", label)
    if not match:
        return {"status": "unsupported_label", "provider_map_id": None}
    name, floor = match.groups()
    candidates = evidence["rows"].get(name, [])
    if len(candidates) != 1:
        return {"status": "ambiguous" if candidates else "unmapped", "provider_map_id": None}
    row = candidates[0]
    selected_floor = floor if floor is not None else "0"
    map_id = row["floors"].get(selected_floor)
    status = (
        row["status"]
        if map_id is not None
        else row["status"]
        if row["status"] != "explicit_provider_mapping"
        else "unmapped_floor"
    )
    return {
        "status": status,
        "provider_map_id": map_id,
        "provider_map_label": label,
        "floor": selected_floor,
        "source_line": row["line"],
        "source_sha256": evidence["sha256"],
        "compatibility": "provider_declared_not_game_verified",
    }
