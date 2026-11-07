import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url

from goldmaking.config import Settings
from goldmaking.db import make_engine
from goldmaking.ingestion import Ingestor
from goldmaking.tsm import FeedSpec


@pytest.fixture
def settings():
    return Settings(_env_file=None)


@pytest.fixture
def migration_config(tmp_path):
    config = Config("alembic.ini")
    config.attributes["database_url"] = f"sqlite:///{tmp_path / 'test.db'}"
    return config


@pytest.fixture
def engine(migration_config):
    pg_url = os.environ.get("TEST_POSTGRES_URL")
    admin = None
    if pg_url:
        schema = "gmi_test_" + uuid4().hex
        admin = make_engine(pg_url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        migration_config.attributes["database_url"] = (
            make_url(pg_url)
            .update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False)
        )
    command.upgrade(migration_config, "head")
    engine = make_engine(migration_config.attributes["database_url"])
    yield engine
    engine.dispose()
    if admin is not None:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def ingestor(engine, settings):
    return Ingestor(engine, settings)


@pytest.fixture
def realm():
    return FeedSpec(
        scope="realm",
        realm_slug="fixture-normal-alliance",
        ruleset="normal",
        faction="alliance",
        dataset="synthetic",
    )


@pytest.fixture
def region():
    return FeedSpec(scope="region", dataset="synthetic")


@pytest.fixture
def fixture_dir():
    return Path("fixtures")
