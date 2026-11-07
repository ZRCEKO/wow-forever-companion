"""Read the audited GatherMate2 data dialect; never execute addon code."""

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PARSER_VERSION = "gathermate-nodes-v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_NODES = 250_000
FILES = {
    "HerbalismData.lua": "GatherMateData2HerbDB",
    "MiningData.lua": "GatherMateData2MineDB",
    "FishData.lua": "GatherMateData2FishDB",
    "TreasureData.lua": "GatherMateData2TreasureDB",
}


def read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("Fichier addon trop volumineux")
    return content


def parse_nodes(content: bytes, variable: str) -> list[dict[str, int]]:
    """Accept only an assignment of nested numeric tables, with line comments."""
    source = content.decode("utf-8-sig")
    source = re.sub(r"--[^\r\n]*", "", source)
    token = re.compile(r"\s*([A-Za-z_][A-Za-z_0-9]*|[0-9]{1,12}|[={}\[\],;])")
    position = 0

    def take() -> str:
        nonlocal position
        match = token.match(source, position)
        if match is None:
            raise ValueError("Syntaxe Lua non prise en charge (aucun code exécuté)")
        position = match.end()
        return match[1]

    def expect(expected: str) -> None:
        if take() != expected:
            raise ValueError(f"Syntaxe de table invalide : attendu {expected}")

    def integer() -> int:
        value = take()
        if not value.isascii() or not value.isdecimal():
            raise ValueError("Entier attendu")
        return int(value)

    expect(variable)
    expect("=")
    expect("{")
    nodes: list[dict[str, int]] = []
    maps: set[int] = set()
    while (next_token := take()) != "}":
        if next_token != "[":
            raise ValueError("Clé de carte attendue")
        map_id = integer()
        if not 0 < map_id <= 2**31 - 1 or map_id in maps:
            raise ValueError("Carte invalide ou dupliquée")
        maps.add(map_id)
        expect("]")
        expect("=")
        expect("{")
        coordinates: set[int] = set()
        while (next_token := take()) != "}":
            if next_token != "[":
                raise ValueError("Coordonnée attendue")
            packed = integer()
            expect("]")
            expect("=")
            node_id = integer()
            x, remainder = divmod(packed, 1_000_000)
            y, precision = divmod(remainder, 100)
            if x > 10000 or y > 10000 or packed in coordinates or not 0 < node_id <= 2**31 - 1:
                raise ValueError("Coordonnée ou identifiant de nœud invalide/dupliqué")
            coordinates.add(packed)
            nodes.append(
                {
                    "provider_map_id": map_id,
                    "packed_coordinate": packed,
                    "x_basis_points": x,
                    "y_basis_points": y,
                    "provider_node_id": node_id,
                    "provider_collision_suffix": precision,
                }
            )
            if len(nodes) > MAX_NODES:
                raise ValueError("Trop de points de récolte")
            expect(",")
        expect(",")
    if source[position:].strip() not in ("", ";"):
        raise ValueError("Contenu supplémentaire après la table")
    return nodes


def inspect_gathermate(root: Path, dataset: str) -> dict[str, Any]:
    """Normalize the Forever directory selected by the verified TOC layout.

    This is a local staging report, not an economic or production database import.
    Provider node IDs are deliberately not interpreted as item IDs.
    """
    if dataset not in {"real", "synthetic"}:
        raise ValueError("Dataset explicite requis")
    toc = read_bounded(root / "GatherMate2_Data_Mainline.toc")
    lines = toc.decode("utf-8-sig").splitlines()
    metadata: dict[str, str] = {}
    entries: list[str] = []
    for line in lines:
        line = line.strip()
        if line.startswith("##") and ":" in line:
            key, value = line[2:].split(":", 1)
            if key.strip() in metadata:
                raise ValueError("Métadonnée TOC dupliquée")
            metadata[key.strip()] = value.strip()
        elif line and not line.startswith("#"):
            entries.append(line.replace("\\", "/"))
    expected = {f"Forever/{name}" for name in FILES} | {"Forever/GatherMateData.lua"}
    if metadata.get("Interface") != "16001" or not metadata.get("Version"):
        raise ValueError("Version/interface non homologuée pour ce lecteur Forever")
    if set(entries) != expected or len(entries) != len(expected):
        raise ValueError("Sélection de fichiers TOC non homologuée")
    reports = []
    total = 0
    for name, variable in FILES.items():
        path = root / "Forever" / name
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Fichier hors du dossier addon")
        content = read_bounded(path)
        nodes = parse_nodes(content, variable)
        total += len(nodes)
        if total > MAX_NODES:
            raise ValueError("Trop de points dans le paquet")
        reports.append(
            {
                "file": f"Forever/{name}",
                "variable": variable,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "node_count": len(nodes),
                "nodes": nodes,
            }
        )
    return {
        "schema_version": PARSER_VERSION,
        "dataset": dataset,
        "provider": "gathermate2_data_wowhead",
        "upstream_provider": "wowhead",
        "game_type": "forever",
        "region": None,
        "ruleset": None,
        "faction": None,
        "addon_version": metadata["Version"],
        "interface": metadata["Interface"],
        "toc_sha256": hashlib.sha256(toc).hexdigest(),
        "inspected_at": datetime.now(UTC).isoformat(),
        "scan_at": None,
        "status": "staging_only",
        "node_count": total,
        "compatibility": "provider_declared_forever_contains_era_sources_unverified",
        "limitations": [
            "provider_ids_unmapped",
            "no_drop_rates",
            "no_respawn_times",
            "no_routes",
            "no_yield_estimates",
            "no_local_market_observations",
        ],
        "files": reports,
    }
