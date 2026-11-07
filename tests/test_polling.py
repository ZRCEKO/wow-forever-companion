import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session

from goldmaking.api import evidence
from goldmaking.cli import main
from goldmaking.ingestion import IngestionError, Ingestor
from goldmaking.models import DataSource, MarketSnapshot
from goldmaking.tsm import FeedSpec, TSMClient


def test_cli_poll_auto_requires_primary_manifest(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "fictional-manifest.json"
    manifest.write_text(json.dumps([{"scope": "region", "transport": "http"}]), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["goldmaking", "poll", str(manifest), "--analyze-primary"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert "exige le flux" in capsys.readouterr().err


def test_cli_poll_auto_failures_do_not_stop_collection_watch(
    engine,
    tmp_path,
    monkeypatch,
    capsys,
):
    from goldmaking import cli
    from goldmaking.analysis import primary_source_id
    from goldmaking.ingestion import ImportResult

    manifest = tmp_path / "fictional-manifest.json"
    # Contrats réels, mais aucun appel fournisseur ni donnée réelle dans cette fixture.
    feeds = [
        FeedSpec(
            scope="realm",
            realm_slug="normal-alliance",
            ruleset="normal",
            faction="alliance",
            transport="http",
        ),
        FeedSpec(scope="region", transport="http"),
    ]
    manifest.write_text(json.dumps([f.model_dump() for f in feeds]), encoding="utf-8")
    calls = []

    def fetch(self, spec):
        calls.append(spec.source_id)
        return ImportResult("not_due", spec.source_id)

    tries = []

    def analyze(_engine):
        tries.append(True)
        if len(tries) == 1:
            raise ValueError("fixture should not leak SQL or credentials")
        return "fixture-analysis"

    cycles = []

    def sleep(_seconds):
        cycles.append(True)
        if len(cycles) == 2:
            raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "make_engine", lambda _: engine)
    monkeypatch.setattr(Ingestor, "fetch", fetch)
    monkeypatch.setattr(cli, "analyze_if_changed", analyze)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    monkeypatch.setattr(
        "sys.argv", ["goldmaking", "poll", str(manifest), "--watch", "--analyze-primary"]
    )
    main()
    output = capsys.readouterr()
    assert calls == [primary_source_id(), feeds[1].source_id] * 2
    assert len(tries) == 2 and "analysis_finished" in output.out
    assert "analysis_failed" in output.err and "fixture should not leak" not in output.err


def test_polling_scope_and_publication_are_independent(settings):
    primary = FeedSpec(
        scope="realm", realm_slug="normal-alliance", ruleset="normal", faction="alliance"
    )
    other = FeedSpec(scope="realm", realm_slug="pvp-horde", ruleset="pvp", faction="horde")
    regional = FeedSpec(scope="region")
    assert primary.interval(settings) == 1800
    assert other.interval(settings) == 3600
    assert regional.interval(settings) == 10800
    assert primary.publication_interval(settings) == other.publication_interval(settings) == 10800
    assert regional.publication_interval(settings) == 86400
    assert primary.model_copy(update={"region": "us"}).interval(settings) == 3600
    snapshot = MarketSnapshot(
        id="test",
        content_sha256="test",
        schema_sha256="test",
        parser_version="1",
        columns=[],
        unknown_columns=[],
        observed_at=datetime.now(UTC) - timedelta(hours=2),
        fetched_at=datetime.now(UTC),
    )
    assert evidence(snapshot, primary, settings).freshness == "fresh"


def test_existing_success_schedule_shortens_and_preserves_cache(engine, settings, fixture_dir):
    spec = FeedSpec(scope="region", transport="http")
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                200,
                content=(fixture_dir / "region_items.csv").read_bytes(),
                headers={"ETag": '"v1"'},
            )
        assert request.headers["If-None-Match"] == '"v1"'
        return httpx.Response(304)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = TSMClient(settings, http)
        old = settings.model_copy(update={"tsm_region_interval_seconds": 86400})
        first = Ingestor(engine, old, client).fetch(spec)
        with Session(engine) as session, session.begin():
            source = session.get(DataSource, spec.source_id)
            source.last_checked_at = datetime.now(UTC) - timedelta(hours=2)
            source.next_check_at = source.last_checked_at + timedelta(days=1)
        ingestor = Ingestor(engine, settings, client)
        assert ingestor.fetch(spec).status == "not_due"
        assert len(calls) == 1
        with Session(engine) as session, session.begin():
            source = session.get(DataSource, spec.source_id)
            assert source.next_check_at - source.last_checked_at == timedelta(hours=3)
            source.last_checked_at = datetime.now(UTC) - timedelta(hours=4)
            source.next_check_at = source.last_checked_at + timedelta(days=1)
        assert ingestor.fetch(spec).status == "not_modified"
        assert len(calls) == 2
        with Session(engine) as session:
            assert session.get(MarketSnapshot, first.snapshot_id) is not None


def test_retry_after_is_not_shortened_by_configuration_change(engine, settings):
    spec = FeedSpec(scope="region", transport="http")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "172800"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        ingestor = Ingestor(engine, settings, TSMClient(settings, http))
        with pytest.raises(IngestionError):
            ingestor.fetch(spec)
        with Session(engine) as session:
            due = session.get(DataSource, spec.source_id).next_check_at
        faster = settings.model_copy(update={"tsm_region_interval_seconds": 1800})
        assert Ingestor(engine, faster, TSMClient(faster, http)).fetch(spec).status == "not_due"
        assert len(calls) == 1
        with Session(engine) as session:
            assert session.get(DataSource, spec.source_id).next_check_at == due
