import gzip
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from goldmaking.api import create_app
from goldmaking.ingestion import IngestionError, Ingestor
from goldmaking.models import DataSource, IngestionRun, MarketObservation, MarketSnapshot
from goldmaking.tsm import FeedSpec, TSMClient


def test_vertical_import_api_and_dedup(engine, settings, ingestor, fixture_dir, realm, region):
    first = ingestor.import_file(fixture_dir / "realm_items.csv", realm)
    duplicate = ingestor.import_file(fixture_dir / "realm_items.csv", realm)
    ingestor.import_file(fixture_dir / "region_items.csv", region)
    assert first.snapshot_id == duplicate.snapshot_id
    assert duplicate.status == "unchanged"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 2
        snapshot = session.get(MarketSnapshot, first.snapshot_id)
        assert gzip.decompress(snapshot.raw_gzip) == (fixture_dir / "realm_items.csv").read_bytes()
        assert snapshot.observed_at.tzinfo is not None
        assert snapshot.fetched_at > snapshot.observed_at
    with TestClient(create_app(settings, engine)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").json()["info"]["version"] == "0.1.0"
        response = client.get("/v1/items/2447?dataset=synthetic")
        assert response.status_code == 200
        data = response.json()
        assert data["currency"] == "copper"
        market, regional = sorted(data["markets"], key=lambda m: m["source"]["spec"]["scope"])
        assert market["values"]["min_buyout"] == 10000
        assert market["values"]["sale_rate"] is None
        assert regional["values"]["sale_rate"] == "0.2500000000"
        assert regional["source"]["spec"]["realm_slug"] is None
        assert market["evidence"]["freshness"] == "stale"
        assert client.get("/v1/items/2447").status_code == 404  # pas de pollution réelle
        assert client.post("/v1/items/2447", json={}).status_code == 405


def test_history_orders_scan_time_and_does_not_infer_sale(
    engine, settings, ingestor, fixture_dir, realm
):
    # Import tardif d'un ancien CSV ne doit pas redevenir le prix courant.
    newest = ingestor.import_file(fixture_dir / "realm_items_later.csv", realm)
    ingestor.import_file(fixture_dir / "realm_items.csv", realm)
    with TestClient(create_app(settings, engine)) as client:
        current = client.get("/v1/items/2447?dataset=synthetic").json()["markets"][0]
        assert current["evidence"]["snapshot_id"] == newest.snapshot_id
        assert current["values"]["min_buyout"] == 11000
        missing = client.get("/v1/items/765?dataset=synthetic").json()["markets"][0]
        assert missing["status"] == "missing_in_latest_snapshot"
        assert missing["values"] is None
        history = client.get(f"/v1/items/2447/history?source_id={realm.source_id}&limit=1").json()
        assert history["has_more"] is True
        assert history["points"][0]["values"]["min_buyout"] == 11000
        old = client.get(
            f"/v1/items/2447/history?source_id={realm.source_id}&limit=1&offset=1"
        ).json()
        assert old["points"][0]["values"]["min_buyout"] == 10000
        assert old["has_more"] is False
        assert (
            client.get(f"/v1/items/1/history?source_id={realm.source_id}&limit=501").status_code
            == 422
        )
        assert (
            client.get(
                f"/v1/items/1/history?source_id={realm.source_id}&since=2026-01-01"
            ).status_code
            == 422
        )
        assert client.get("/v1/sources?limit=0").status_code == 422
        assert client.get("/v1/ingestion-runs").json()[0]["status"] == "imported"


def test_region_faction_ruleset_version_and_dataset_isolation(
    engine, settings, ingestor, fixture_dir, realm
):
    specs = [
        realm,
        realm.model_copy(update={"faction": "horde"}),
        realm.model_copy(update={"ruleset": "pvp"}),
        realm.model_copy(update={"region": "us"}),
        realm.model_copy(update={"game_type": "classic"}),
        realm.model_copy(update={"dataset": "real"}),
    ]
    for spec in specs:
        ingestor.import_file(fixture_dir / "realm_items.csv", spec)
    assert len({s.source_id for s in specs}) == 6
    with TestClient(create_app(settings, engine)) as client:
        assert len(client.get("/v1/items/2447?dataset=synthetic").json()["markets"]) == 3
        assert len(client.get("/v1/items/2447?dataset=synthetic&region=us").json()["markets"]) == 1
        assert (
            len(client.get("/v1/items/2447?dataset=synthetic&game_type=classic").json()["markets"])
            == 1
        )
        assert len(client.get("/v1/items/2447").json()["markets"]) == 1


def test_failure_rolls_back_but_preserves_audit(engine, ingestor, tmp_path, realm):
    file = tmp_path / "bad.csv"
    file.write_text("itemId,marketValue\n1,2\n2,-1\n")
    with pytest.raises(IngestionError) as error:
        ingestor.import_file(file, realm)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 0
        assert session.scalar(select(func.count()).select_from(MarketObservation)) == 0
        assert session.get(IngestionRun, error.value.run_id).status == "failed"


def test_unknown_timestamp_and_future_rejection(engine, settings, ingestor, tmp_path, realm):
    file = tmp_path / "unknown.csv"
    file.write_text("itemId,marketValue\n1,2\n")
    ingestor.import_file(file, realm)
    with TestClient(create_app(settings, engine)) as client:
        e = client.get("/v1/items/1?dataset=synthetic").json()["markets"][0]["evidence"]
        assert e["observed_at"] is None and e["freshness"] == "unknown"
    file.write_text("itemId,marketValue,updatedAt\n1,2,2099-01-01T00:00:00Z\n")
    with pytest.raises(IngestionError, match="futur"):
        ingestor.import_file(file, realm)


def test_conditional_requests_cadence_and_unchanged_snapshot(engine, settings, fixture_dir):
    spec = FeedSpec(scope="region", transport="http")
    requests = []
    content = (fixture_dir / "region_items.csv").read_bytes()

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=content,
                headers={"ETag": '"v1"', "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"},
            )
        assert request.headers["If-None-Match"] == '"v1"'
        assert "If-Modified-Since" in request.headers
        return httpx.Response(304)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        ingestor = Ingestor(engine, settings, TSMClient(settings, http))
        assert ingestor.fetch(spec).status == "imported"
        assert ingestor.fetch(spec).status == "not_due"
        assert len(requests) == 1
        with Session(engine) as session, session.begin():
            session.get(DataSource, spec.source_id).next_check_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
        assert ingestor.fetch(spec).status == "not_modified"
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 1
        assert session.get(DataSource, spec.source_id).next_check_at > datetime.now(
            UTC
        ) + timedelta(hours=2, minutes=59)


def test_invalid_remote_payload_does_not_commit_etag(engine, settings):
    spec = FeedSpec(scope="region", transport="http")
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="denied", headers={"ETag": "bad"})
        )
    ) as http:
        ingestor = Ingestor(engine, settings, TSMClient(settings, http))
        with pytest.raises(IngestionError):
            ingestor.fetch(spec)
        assert ingestor.fetch(spec).status == "not_due"
    with Session(engine) as session:
        assert session.get(DataSource, spec.source_id).etag is None


def test_migrations_roundtrip(migration_config):
    command.upgrade(migration_config, "head")
    command.check(migration_config)
    command.downgrade(migration_config, "base")
    command.upgrade(migration_config, "head")


def test_not_ready_without_migrations(tmp_path, settings):
    from goldmaking.db import make_engine

    engine = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    with TestClient(create_app(settings, engine)) as client:
        assert client.get("/health").status_code == 503
    engine.dispose()
