# WoW Forever Companion

Public review snapshot of a personal, local WoW Forever EU knowledge and in-game
economy analysis project. It supports manual decisions about gathering, crafting
and buying/reselling items for in-game gold. It does not generate real-world money,
trade automatically, control the game, or execute addon Lua.

The application package and CLI are named `goldmaking`; the neutral repository
name does not change its economic-analysis purpose. Application source, tests,
migrations and fictional fixtures are copied unchanged from the working project.
SOURCE_MANIFEST.json records their SHA-256 hashes. This is a review snapshot,
not an automatically synchronized mirror.

## Current capabilities

TSM price ingestion with provenance and UTC timestamps; read-only FastAPI;
local descriptive price indicators and archived discount analyses; local addon
readers and archived farming route candidates. Amounts are integer copper;
unknown data stays null. Regional statistics are not local sales observations.
Routes do not establish yields, availability or profitability.

## Proposed ForeverDB integration

Planned bulk, paginated import of items, professions/recipes and gathering data.
No ForeverDB connector or API key is included or active. ForeverDB attribution
with a visible link will accompany displayed data: https://foreverdb.net .
Classic-derived data must remain separate from observed Forever data.
Long-term local versioned retention is desired, subject to the provider's terms;
no exemption from upstream restrictions is assumed.

## Run locally (Python 3.12)

```sh
python -m venv .venv
# Activate the virtual environment for your platform.
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
alembic upgrade head
goldmaking demo
uvicorn goldmaking.api:create_app --factory --host 127.0.0.1 --port 8000
```

The demo uses explicitly fictional fixtures. Local Docker development configuration
is in compose.yaml; its sample password is for local development only.

```sh
python -m pytest
ruff check .
ruff format --check .
mypy
```

## Publication scope

Private deployment notes, host configuration, real addon packages, character data,
market dumps, backups, credentials, original Git history and registry publication
workflows are excluded. No third-party guide content is redistributed. The public
snapshot is supplied for inspection; no additional software license is granted here.
