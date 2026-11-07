"""All archived data in these tests is explicitly fictional."""

from copy import deepcopy

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select
from sqlalchemy.orm import Session

from goldmaking.api import create_app
from goldmaking.farming_storage import archive_catalog
from goldmaking.models import FarmingCatalog, FarmingTarget, FarmingVariant


def catalog():
    return {
        "schema_version": "zygor-profile-catalog-v2",
        "dataset": "synthetic",
        "inspected_at": "2026-01-01T00:00:00+00:00",
        "variant_count": 2,
        "profile": {"game_type": "forever", "faction": "alliance", "level": 13},
        "variants": [
            {
                "variant_id": c * 64,
                "route": {"target_item_ids": [999999]},
                "assessment": {"eligible_for_recommendation": False, "status": "unknown"},
                "duration_seconds": None,
                "source": "fictional-test-only",
            }
            for c in ("a", "b")
        ],
    }


def test_archive_replay_and_profile_version(engine):
    data = catalog()
    first = archive_catalog(engine, data, True)
    data["inspected_at"] = "2026-01-02T00:00:00+00:00"
    assert archive_catalog(engine, data, True) == first
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(FarmingCatalog)) == 1
        assert session.scalar(select(func.count()).select_from(FarmingTarget)) == 2
        assert session.get(FarmingCatalog, first).created_at.utcoffset().total_seconds() == 0
    data["profile"]["level"] = 14
    assert archive_catalog(engine, data, True) != first


def test_api_dataset_isolation_pagination_and_no_mutation(engine, settings):
    identifier = archive_catalog(engine, catalog(), True)
    with TestClient(create_app(settings, engine)) as client:
        assert client.get("/v1/farming/catalogs").json()["catalogs"] == []
        base = f"/v1/farming/catalogs/{identifier}"
        assert client.get(base).status_code == 404
        assert client.get(base, params={"dataset": "synthetic"}).status_code == 200
        url = base + "/items/999999"
        first = client.get(url, params={"dataset": "synthetic", "limit": 1}).json()
        assert first["has_more"] is True
        assert len(first["variants"]) == 1
        assert first["variants"][0]["duration_seconds"] is None
        second = client.get(url, params={"dataset": "synthetic", "limit": 1, "offset": 1}).json()
        assert second["has_more"] is False
        assert first["variants"][0]["variant_id"] != second["variants"][0]["variant_id"]
        assert client.get(url, params={"dataset": "synthetic", "limit": 100}).status_code == 422
        assert client.post(url, json={}).status_code == 405
        assert (
            client.get(base + "/items/888888", params={"dataset": "synthetic"}).json()["variants"]
            == []
        )


def test_atomic_rollback(engine):
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO farming_targets"):
            raise RuntimeError("synthetic injected failure")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            archive_catalog(engine, catalog(), True)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(FarmingCatalog)) == 0
        assert session.scalar(select(func.count()).select_from(FarmingVariant)) == 0


def test_explicit_synthetic_and_invalid_data_leave_no_archive(engine):
    with pytest.raises(ValueError):
        archive_catalog(engine, catalog())
    bad = deepcopy(catalog())
    bad["variants"][0]["route"]["target_item_ids"] = [0]
    with pytest.raises(ValueError):
        archive_catalog(engine, bad, True)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(FarmingCatalog)) == 0


def test_migration_roundtrip_preserves_existing_market(
    engine, migration_config, ingestor, realm, fixture_dir
):
    snapshot = ingestor.import_file(fixture_dir / "realm_items.csv", realm)
    command.downgrade(migration_config, "a41c8e219f02")
    assert "farming_catalogs" not in inspect(engine).get_table_names()
    command.upgrade(migration_config, "head")
    from goldmaking.models import MarketSnapshot

    with Session(engine) as session:
        assert session.get(MarketSnapshot, snapshot.snapshot_id) is not None
    archive_catalog(engine, catalog(), True)
