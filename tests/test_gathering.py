"""All addon data in these tests is explicitly synthetic."""

from pathlib import Path

import pytest

from goldmaking.gathering import FILES, inspect_gathermate, parse_nodes


def test_synthetic_coordinates_and_collision() -> None:
    rows = parse_nodes(b"Fake = { [9999] = { [1234567801] = 999, }, }", "Fake")
    assert rows == [
        {
            "provider_map_id": 9999,
            "packed_coordinate": 1234567801,
            "x_basis_points": 1234,
            "y_basis_points": 5678,
            "provider_node_id": 999,
            "provider_collision_suffix": 1,
        }
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Fake = os.execute('bad')",
        "Fake = {} os.execute('bad')",
        "Fake = {[1] = {[123000000] = 1, [123000000] = 2,},}",
        "Fake = {[1] = {}, [1] = {},}",
        "Fake = {[1] = {[10001000000] = 1,},}",
        "Fake = {[1] = {[123000000] = 0,},}",
        "Fake = {[1] = {[123000000] = true,},}",
        "Fake = {[1] = {[123000000] = 1.5,},}",
        "Fake = {",
    ],
)
def test_rejects_invalid_or_executable_lua(text: str) -> None:
    with pytest.raises(ValueError):
        parse_nodes(text.encode(), "Fake")


def test_empty_is_not_a_yield() -> None:
    assert parse_nodes(b"-- synthetic\nFake = {};", "Fake") == []


def synthetic_addon(root: Path) -> Path:
    (root / "Forever").mkdir()
    for name, variable in FILES.items():
        (root / "Forever" / name).write_text(f"{variable} = {{}}", encoding="utf-8")
    toc = root / "GatherMate2_Data_Mainline.toc"
    toc.write_text(
        "## Interface: 16001\n## Version: synthetic-test\n"
        + "\n".join(f"Forever/{name}" for name in FILES)
        + "\nForever/GatherMateData.lua",
        encoding="utf-8",
    )
    return toc


def test_report_provenance_and_no_invented_market(tmp_path: Path) -> None:
    synthetic_addon(tmp_path)
    report = inspect_gathermate(tmp_path, "synthetic")
    assert report["dataset"] == "synthetic"
    assert report["scan_at"] is None
    assert report["region"] is None
    assert report["status"] == "staging_only"
    assert report["node_count"] == 0
    assert len(report["files"][0]["sha256"]) == 64
    assert report["inspected_at"].endswith("+00:00")


def test_rejects_other_version_before_reading_data(tmp_path: Path) -> None:
    toc = synthetic_addon(tmp_path)
    toc.write_text(toc.read_text().replace("Forever/", "Era/"))
    with pytest.raises(ValueError, match="TOC"):
        inspect_gathermate(tmp_path, "real")


def test_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic_addon(tmp_path)
    monkeypatch.setattr("goldmaking.gathering.MAX_BYTES", 10)
    with pytest.raises(ValueError, match="volumineux"):
        inspect_gathermate(tmp_path, "synthetic")


def test_node_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("goldmaking.gathering.MAX_NODES", 0)
    with pytest.raises(ValueError, match="Trop"):
        parse_nodes(b"Fake = {[1] = {[123000000] = 1,},}", "Fake")
