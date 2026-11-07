"""Intégration sur données entièrement fictives, SQLite et PostgreSQL en CI."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from goldmaking.analysis import analyze_if_changed, analyze_market, primary_source_id
from goldmaking.api import create_app
from goldmaking.cli import main
from goldmaking.detector import CostProfile
from goldmaking.models import (
    AnalysisEvaluation,
    AnalysisRun,
    AnalysisSnapshot,
    IngestionRun,
    Item,
    MarketObservation,
    MarketSnapshot,
)

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
AS_OF = NOW + timedelta(minutes=1)


def add_snapshot(engine, realm, index, values, *, known_at=None, observed_at="default", suffix=""):
    """Ne contient aucun dump réel. Les identifiants et hashes désignent des fixtures."""
    scan = NOW - timedelta(hours=index * 3)
    sid = str(uuid5(NAMESPACE_URL, f"fixture-only/{realm.source_id}/{index}/{suffix}"))
    with Session(engine) as session, session.begin():
        session.add(
            MarketSnapshot(
                id=sid,
                source_id=realm.source_id,
                content_sha256=hashlib.sha256(sid.encode()).hexdigest(),
                schema_sha256="f" * 64,
                columns=["itemId", "minBuyout", "updatedAt"],
                unknown_columns=[],
                parser_version="1",
                observed_at=scan if observed_at == "default" else observed_at,
                fetched_at=scan + timedelta(seconds=1),
                row_count=len(values),
                raw_gzip=b"explicitly fictitious test metadata; never replay this as CSV",
            )
        )
        session.flush()
        for item, price in values.items():
            if session.get(Item, (realm.game_type, item)) is None:
                session.add(Item(game_type=realm.game_type, item_id=item))
                session.flush()
            session.add(
                MarketObservation(
                    snapshot_id=sid,
                    item_id=item,
                    game_type=realm.game_type,
                    name="Objet entièrement fictif",
                    min_buyout=price,
                    extra={},
                )
            )
        session.add(
            IngestionRun(
                id=str(uuid5(NAMESPACE_URL, sid + "/run")),
                source_id=realm.source_id,
                snapshot_id=sid,
                started_at=scan + timedelta(seconds=1),
                finished_at=known_at or scan + timedelta(seconds=2),
                status="imported",
                origin="fixture-only",
            )
        )
    return sid


@pytest.fixture
def market_history(engine, ingestor, realm):
    ingestor.ensure_source(realm)
    for index in range(56, 0, -1):
        add_snapshot(engine, realm, index, {2447: 1000, 2448: 1000, 2449: 1000, 2450: 1000})
    current = add_snapshot(engine, realm, 0, {2447: 700, 2448: 600, 2449: 600})
    return current


def results(engine, run_id):
    with Session(engine) as session:
        return [
            r.result
            for r in session.scalars(
                select(AnalysisEvaluation)
                .where(AnalysisEvaluation.analysis_id == run_id)
                .order_by(AnalysisEvaluation.item_id)
            )
        ]


def test_analysis_persisted_ranked_paginated_and_read_only(
    engine,
    settings,
    realm,
    market_history,
):
    run_id = analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    with Session(engine) as session:
        run = session.get(AnalysisRun, run_id)
        assert run.summary["evaluated_items"] == 4
        assert run.summary["price_states"] == {"candidate_to_review": 3, "not_calculable": 1}
        assert run.as_of == AS_OF
        assert session.scalar(select(func.count()).select_from(AnalysisSnapshot)) == 57
        assert "raw_gzip" not in session.get(AnalysisSnapshot, (run_id, market_history)).evidence
    with TestClient(create_app(settings, engine)) as client:
        # Le défaut reste le marché réel principal ; une fixture doit être demandée.
        assert client.get("/v1/analyses").json()["items"] == []
        listing = client.get("/v1/analyses", params={"source_id": realm.source_id}).json()
        assert listing["items"][0]["id"] == run_id
        root = f"/v1/analyses/{run_id}"
        assert client.get(root).json()["contract"]["source"]["dataset"] == "synthetic"
        first = client.get(root + "/evaluations", params={"limit": 2}).json()
        assert [r["item_id"] for r in first["items"]] == [2448, 2449]
        assert [r["rank"] for r in first["items"]] == [1, 2]
        assert first["has_more"] is True and "evidence" not in first["items"][0]
        assert first["items"][0]["discount"] == "0.4"
        last = client.get(root + "/evaluations", params={"offset": 2}).json()
        assert [r["item_id"] for r in last["items"]] == [2447, 2450]
        assert last["items"][1]["reasons"] == ["missing_in_latest_snapshot"]
        filtered = client.get(
            root + "/evaluations", params={"price_state": "not_calculable"}
        ).json()
        assert len(filtered["items"]) == 1
        evidence_url = root + "/evaluations/2447/evidence"
        gathered = []
        for offset in range(0, 57, 10):
            page = client.get(evidence_url, params={"offset": offset, "limit": 10}).json()
            assert page["total"] == 57
            gathered.extend(page["items"])
        assert len(gathered) == 57
        current = next(e for e in gathered if e["role"] == "current")
        assert current["snapshot_id"] == market_history
        assert current["min_buyout"] == 700 and current["known_at"].endswith("+00:00")
        assert len([e for e in gathered if e["role"] == "reference"]) == 56
        assert client.get(root + "/evaluations/2447").status_code == 200
        assert client.get(root + "/evaluations/999/evidence").status_code == 404
        assert client.get("/v1/analyses/unknown").status_code == 404
        assert client.get(evidence_url, params={"limit": 101}).status_code == 422
        assert client.get(root + "/evaluations", params={"offset": -1}).status_code == 422
        assert client.post("/v1/analyses", json={}).status_code == 405
        assert client.get("/openapi.json").status_code == 200


def test_replay_excludes_later_completion_correction_and_unchanged_reimport(
    engine,
    realm,
    market_history,
):
    initial = analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    add_snapshot(
        engine, realm, 0, {2447: 1}, suffix="late-correction", known_at=NOW + timedelta(hours=1)
    )
    # Une réimportation identique n'a pas le droit de redater la première disponibilité.
    with Session(engine) as session, session.begin():
        session.add(
            IngestionRun(
                id="fixture-unchanged-run",
                source_id=realm.source_id,
                snapshot_id=market_history,
                started_at=AS_OF,
                finished_at=AS_OF + timedelta(seconds=1),
                status="unchanged",
                origin="fixture-only",
            )
        )
    replay = analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    assert results(engine, initial) == results(engine, replay)
    later = analyze_market(engine, realm.source_id, NOW + timedelta(hours=2), allow_synthetic=True)
    assert results(engine, later)[0]["current_price"] == 1
    assert "missing_in_latest_snapshot" in results(engine, later)[1]["reasons"]


def test_source_guards_limits_and_atomic_rollback(engine, realm, ingestor, monkeypatch):
    ingestor.ensure_source(realm)
    add_snapshot(engine, realm, 0, {2447: 700, 2448: 600})
    with pytest.raises(ValueError, match="synthetic"):
        analyze_market(engine, realm.source_id, AS_OF)
    with pytest.raises(ValueError, match="Source absente"):
        analyze_market(engine, primary_source_id(), AS_OF)
    monkeypatch.setattr("goldmaking.analysis.MAX_ITEMS", 1)
    with pytest.raises(ValueError, match="20000"):
        analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    monkeypatch.setattr("goldmaking.analysis.MAX_SNAPSHOTS", 0)
    with pytest.raises(ValueError, match="4096"):
        analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRun)) == 0


def test_error_during_evaluation_rolls_back_all_rows(engine, realm, market_history, monkeypatch):
    import goldmaking.analysis as module

    original = module.evaluate

    def fail_second(item, *args, **kwargs):
        if item == 2448:
            raise ValueError("fixture simulated failure")
        return original(item, *args, **kwargs)

    monkeypatch.setattr(module, "evaluate", fail_second)
    with pytest.raises(ValueError, match="simulated failure"):
        analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRun)) == 0
        assert session.scalar(select(func.count()).select_from(AnalysisSnapshot)) == 0
        assert session.scalar(select(func.count()).select_from(AnalysisEvaluation)) == 0
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 57


def test_newer_undated_snapshot_and_region_are_not_local_prices(engine, ingestor, realm, region):
    ingestor.ensure_source(realm)
    add_snapshot(engine, realm, 1, {2447: 1000})
    add_snapshot(engine, realm, 0, {2447: 1}, observed_at=None)
    run = analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    assert results(engine, run)[0]["reasons"] == ["unknown_current_scan"]
    ingestor.ensure_source(region)
    with pytest.raises(ValueError, match="marché"):
        analyze_market(engine, region.source_id, AS_OF, allow_synthetic=True)


def test_cli_runs_analysis_and_rejects_naive_date(
    engine,
    settings,
    realm,
    market_history,
    monkeypatch,
    capsys,
):
    monkeypatch.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(
        "sys.argv",
        [
            "goldmaking",
            "analyze-market",
            "--source-id",
            realm.source_id,
            "--synthetic",
            "--as-of",
            AS_OF.isoformat(),
            "--item-id",
            "2447",
        ],
    )
    main()
    output = json.loads(capsys.readouterr().out)
    assert len(results(engine, output["analysis_id"])) == 1
    monkeypatch.setattr("sys.argv", ["goldmaking", "analyze-market", "--as-of", "2026-11-06"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "fuseau" in capsys.readouterr().err


def test_upgrade_preserves_ingestion_and_matches_models(
    engine, migration_config, ingestor, realm, fixture_dir
):
    command.downgrade(migration_config, "82ce2415b883")
    old = ingestor.import_file(fixture_dir / "realm_items.csv", realm)
    with TestClient(create_app(engine=engine)) as client:
        assert client.get("/health").status_code == 503
    command.upgrade(migration_config, "head")
    command.check(migration_config)
    assert "analysis_runs" in inspect(engine).get_table_names()
    with Session(engine) as session:
        assert session.get(MarketSnapshot, old.snapshot_id) is not None
    command.downgrade(migration_config, "82ce2415b883")
    with Session(engine) as session:
        assert session.get(MarketSnapshot, old.snapshot_id) is not None
    command.upgrade(migration_config, "head")


def test_cost_profile_json_schema_requires_explicit_costs():
    assert "final_deposit" in CostProfile.model_json_schema()["required"]


def test_other_market_cannot_change_primary_results(engine, ingestor, realm, market_history):
    other = realm.model_copy(
        update={"realm_slug": "fixture-pvp-horde", "ruleset": "pvp", "faction": "horde"}
    )
    ingestor.ensure_source(other)
    add_snapshot(engine, other, 0, {2447: 1})
    run = analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    assert results(engine, run)[0]["current_price"] == 700
    with Session(engine) as session:
        refs = list(
            session.scalars(select(AnalysisSnapshot).where(AnalysisSnapshot.analysis_id == run))
        )
        assert {r.evidence["source_id"] for r in refs} == {realm.source_id}


def test_stale_scan_is_not_replaced_by_import_time(engine, ingestor, realm):
    ingestor.ensure_source(realm)
    add_snapshot(engine, realm, 20, {2447: 700})
    run = analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    assert results(engine, run)[0]["reasons"] == ["stale_scan"]


def test_requested_absent_item_has_explicit_explanation(engine, ingestor, realm):
    ingestor.ensure_source(realm)
    add_snapshot(engine, realm, 0, {2447: 700})
    run = analyze_market(engine, realm.source_id, AS_OF, item_id=999, allow_synthetic=True)
    r = results(engine, run)[0]
    assert r["current_price"] is None and r["reasons"] == ["missing_in_latest_snapshot"]


def test_automatic_analysis_catches_up_once_and_ignores_unchanged_import(
    engine,
    realm,
    market_history,
):
    first = analyze_if_changed(engine, realm.source_id, allow_synthetic=True)
    assert first is not None
    assert analyze_if_changed(engine, realm.source_id, allow_synthetic=True) is None
    with Session(engine) as session, session.begin():
        session.add(
            IngestionRun(
                id="fixture-unchanged-auto",
                source_id=realm.source_id,
                snapshot_id=market_history,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                status="unchanged",
                origin="fixture-only",
            )
        )
    assert analyze_if_changed(engine, realm.source_id, allow_synthetic=True) is None
    add_snapshot(
        engine, realm, 0, {2447: 600}, suffix="auto-new-import", known_at=datetime.now(UTC)
    )
    second = analyze_if_changed(engine, realm.source_id, allow_synthetic=True)
    assert second is not None and second != first
    assert analyze_if_changed(engine, realm.source_id, allow_synthetic=True) is None
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRun)) == 2


def test_existing_full_manual_analysis_is_a_checkpoint_but_partial_is_not(
    engine,
    realm,
    market_history,
):
    analyze_market(engine, realm.source_id, AS_OF, item_id=2447, allow_synthetic=True)
    assert analyze_if_changed(engine, realm.source_id, allow_synthetic=True) is not None
    assert analyze_if_changed(engine, realm.source_id, allow_synthetic=True) is None


def test_full_manual_analysis_prevents_startup_duplicate(engine, realm, market_history):
    analyze_market(engine, realm.source_id, AS_OF, allow_synthetic=True)
    assert analyze_if_changed(engine, realm.source_id, allow_synthetic=True) is None


def test_automatic_analysis_retries_after_failure_and_waits_without_data(
    engine,
    ingestor,
    realm,
    monkeypatch,
):
    assert analyze_if_changed(engine) is None
    ingestor.ensure_source(realm)
    assert analyze_if_changed(engine, realm.source_id, allow_synthetic=True) is None
    add_snapshot(engine, realm, 0, {2447: 700})
    import goldmaking.analysis as module

    original = module.analyze_market

    def fail(*args, **kwargs):
        raise ValueError("fixture-only failure")

    monkeypatch.setattr(module, "analyze_market", fail)
    with pytest.raises(ValueError, match="fixture-only"):
        analyze_if_changed(engine, realm.source_id, allow_synthetic=True)
    monkeypatch.setattr(module, "analyze_market", original)
    assert analyze_if_changed(engine, realm.source_id, allow_synthetic=True) is not None


def test_future_replay_does_not_prevent_automatic_analysis(engine, realm, market_history):
    analyze_market(
        engine, realm.source_id, datetime.now(UTC) + timedelta(days=1), allow_synthetic=True
    )
    assert analyze_if_changed(engine, realm.source_id, allow_synthetic=True) is not None


def test_indicators_api_is_read_only_bounded_and_explicit_about_fixture(
    engine,
    settings,
    realm,
    market_history,
):
    url = "/v1/items/2447/indicators"
    with TestClient(create_app(settings, engine)) as client:
        assert client.get(url).status_code == 404  # aucune substitution fictive au défaut réel
        params = {"source_id": realm.source_id, "as_of": AS_OF.isoformat()}
        response = client.get(url, params=params)
        assert response.status_code == 200
        data = response.json()
        assert data["source"]["dataset"] == "synthetic"
        assert data["source_id"] == realm.source_id and data["current_price"] == 700
        assert data["previous_scan_change"]["relative"] == "-0.3"
        assert data["change_24h"]["relative"] == "-0.3"
        assert data["distribution"]["median"] == 1000
        assert data["coverage"]["valid_buckets"] == 56
        assert data["coverage"]["meets_history_requirements"] is True
        assert data["indicator_version"] == "local-descriptive-v3"
        assert data["reference_deviation"]["median"] == 1000
        assert data["reference_deviation"]["absolute_copper"] == -300
        assert data["reference_deviation"]["relative"] == "-0.3"
        assert data["anomaly"]["reason"] == "zero_reference_iqr"
        assert data["anomaly"]["flagged"] is None
        assert data["volatility"]["state"] == "calculable"
        assert data["volatility"]["return_count"] == 56
        evidence_ids = {e["snapshot_id"] for e in data["evidence"]}
        assert set(data["volatility"]["snapshot_ids"]) <= evidence_ids
        assert len(data["evidence"]) <= 59
        assert all(len(e["content_sha256"]) == 64 for e in data["evidence"])
        assert all(e["fetched_at"].endswith("Z") for e in data["evidence"])
        assert client.get(url, params={**params, "as_of": "2026-11-06"}).status_code == 422
        assert client.get(url, params={"source_id": "bad"}).status_code == 422
        assert client.get("/v1/items/0/indicators", params=params).status_code == 422
        assert client.post(url, json={}).status_code == 405
        missing = client.get("/v1/items/2450/indicators", params=params).json()
        assert missing["current_price"] is None
        assert missing["coverage"]["valid_buckets"] is None
        assert missing["previous_scan_change"]["relative"] is None
        assert missing["volatility"]["return_count"] is None
        assert missing["reference_deviation"]["median"] is None
        assert missing["anomaly"]["flagged"] is None
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(AnalysisRun)) == 0
        assert session.scalar(select(func.count()).select_from(MarketSnapshot)) == 57


def test_indicators_source_isolation_replay_and_no_full_csv_loading(
    engine,
    settings,
    ingestor,
    realm,
    region,
    market_history,
):
    from sqlalchemy import event

    other = realm.model_copy(update={"realm_slug": "fixture-normal-horde", "faction": "horde"})
    ingestor.ensure_source(other)
    add_snapshot(engine, other, 0, {2447: 1})
    ingestor.ensure_source(region)
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with TestClient(create_app(settings, engine)) as client:
            url = "/v1/items/2447/indicators"
            params = {"source_id": realm.source_id, "as_of": AS_OF.isoformat()}
            first = client.get(url, params=params).json()
            assert (
                client.get(url, params={**params, "source_id": region.source_id}).status_code == 422
            )
            assert (
                client.get(url, params={**params, "source_id": other.source_id}).json()[
                    "current_price"
                ]
                == 1
            )
            add_snapshot(
                engine,
                realm,
                0,
                {2447: 1},
                suffix="future-indicator-correction",
                known_at=AS_OF + timedelta(hours=1),
            )
            assert client.get(url, params=params).json() == first
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any(
        "raw_gzip" in sql for sql in statements if sql.lstrip().upper().startswith("SELECT")
    )
