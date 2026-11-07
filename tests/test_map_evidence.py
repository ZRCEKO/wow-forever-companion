"""Map tables here are wholly fictional, not extracts from addon data."""

import pytest

from goldmaking.map_evidence import parse_map_table, resolve_map

TABLE = b"""data.MapIDsByName = {
["Synthetic Meadow"]={[0]=777001,[1]=777002},
["Synthetic Instance"]={[0]=9001,c=1, instance=999},
}
os.execute("not executed")
"""


def test_explicit_floor_and_evidence() -> None:
    evidence = parse_map_table(TABLE)
    result = resolve_map("Synthetic Meadow/1", evidence)
    assert result["provider_map_id"] == 777002
    assert result["source_line"] == 2
    assert len(result["source_sha256"]) == 64
    assert result["status"] == "explicit_provider_mapping"
    assert resolve_map("Synthetic Meadow", evidence)["provider_map_id"] == 777001


def test_no_fuzzy_match_or_floor_substitution() -> None:
    evidence = parse_map_table(TABLE)
    assert resolve_map("synthetic meadow", evidence)["provider_map_id"] is None
    assert resolve_map("Synthetic Meadow/2", evidence)["status"] == "unmapped_floor"
    assert resolve_map("Missing", evidence)["status"] == "unmapped"


def test_fake_instance_map_never_becomes_public_map_id() -> None:
    result = resolve_map("Synthetic Instance", parse_map_table(TABLE))
    assert result["status"] == "provider_synthetic_instance"
    assert result["provider_map_id"] is None


def test_duplicate_names_are_ambiguous_even_if_same_value() -> None:
    source = TABLE.replace(b"}\nos.execute", b'["Synthetic Meadow"]={[0]=777001},\n}\nos.execute')
    result = resolve_map("Synthetic Meadow", parse_map_table(source))
    assert result["status"] == "ambiguous"
    assert result["provider_map_id"] is None


def test_duplicate_floors_not_silently_overwritten() -> None:
    evidence = parse_map_table(TABLE.replace(b"[1]=777002", b"[0]=777002"))
    assert resolve_map("Synthetic Meadow", evidence)["status"] == "unsupported"


@pytest.mark.parametrize("source", [b"", b"data.MapIDsByName = {\n", TABLE + TABLE])
def test_missing_truncated_or_multiple_tables_refused(source: bytes) -> None:
    with pytest.raises(ValueError):
        parse_map_table(source)


def test_expressions_are_not_interpreted() -> None:
    evidence = parse_map_table(TABLE.replace(b"[0]=777001,[1]=777002", b"[0]=1+777000"))
    assert resolve_map("Synthetic Meadow", evidence)["provider_map_id"] is None


def test_unrecognized_table_row_is_reported() -> None:
    evidence = parse_map_table(TABLE.replace(b'["Synthetic Meadow"]', b"[variable]"))
    assert evidence["unsupported_lines"] == [2]
