from decimal import Decimal

import pytest
from pydantic import ValidationError

from goldmaking.money import MAX_COPPER, copper_to_gold, gold_to_copper
from goldmaking.tsm import FeedError, FeedSpec, parse_csv


@pytest.mark.parametrize("copper", [0, 1, 9999, 10000, 123456789, MAX_COPPER])
def test_money_exact_roundtrip(copper):
    assert gold_to_copper(copper_to_gold(copper)) == copper


@pytest.mark.parametrize("value", ["-1", "0.00001", "NaN", "Infinity", "922337203685477.5808"])
def test_invalid_gold_rejected(value):
    with pytest.raises(ValueError):
        gold_to_copper(value)


def test_float_money_rejected():
    with pytest.raises(TypeError):
        gold_to_copper(0.1)


def test_documented_schemas_missing_and_zero(fixture_dir, realm, region, settings):
    market = parse_csv((fixture_dir / "realm_items.csv").read_bytes(), realm, settings)
    assert market.rows[1]["market_value"] == 0
    assert market.rows[1]["recent"] is None
    assert market.rows[0]["sale_rate"] is None
    regional = parse_csv((fixture_dir / "region_items.csv").read_bytes(), region, settings)
    assert regional.rows[0]["sold_per_day"] == Decimal("42.5")
    assert regional.rows[1]["sale_rate"] == Decimal("0")
    assert regional.rows[2]["sale_rate"] is None


def test_added_columns_preserved(fixture_dir, realm, settings):
    parsed = parse_csv((fixture_dir / "realm_items_later.csv").read_bytes(), realm, settings)
    assert parsed.unknown_columns == ["newProviderField"]
    assert parsed.rows[0]["extra"] == {"newProviderField": "example"}


@pytest.mark.parametrize(
    "csv_text",
    [
        "itemId,marketValue\n1,1.2\n",
        "itemId,marketValue\n1,-1\n",
        "itemId,marketValue\n1,NaN\n",
        "itemId,marketValue\n1,9223372036854775808\n",
        "itemId,marketValue\n1,2\n1,3\n",
        "itemId,marketValue\n0,2\n",
        "itemId,marketValue\ni:2447,2\n",
        "itemId,marketValue,marketValue\n1,2,3\n",
        "itemId,marketValue\n1\n",
        "itemId,marketValue\n1,2,3\n",
        "itemId,name\n1,example\n",
        "petSpeciesId,marketValue\n1,2\n",
        "itemId,marketValue\n",
        "<html>Access denied</html>",
        "itemId,marketValue,updatedAt\n1,2,2026-01-01T12:00:00\n",
        "itemId,marketValue,updatedAt\n1,2,2026-01-01T12:00:00Z\n2,2,\n",
        'itemId,marketValue\n1,"unterminated\n',
    ],
)
def test_invalid_csv_rejected_atomically(csv_text, realm, settings):
    with pytest.raises(FeedError):
        parse_csv(csv_text.encode(), realm, settings)


@pytest.mark.parametrize(
    "column,value",
    [
        ("saleRate", "1.1"),
        ("saleRate", "NaN"),
        ("saleRate", "0.12345678901"),
        ("soldPerDay", "-1"),
        ("soldPerDay", "1e20"),
    ],
)
def test_invalid_statistics(column, value, region, settings):
    with pytest.raises(FeedError):
        parse_csv(f"itemId,{column}\n1,{value}\n".encode(), region, settings)


def test_optional_date_and_bom(realm, settings):
    parsed = parse_csv(b"\xef\xbb\xbfitemId,marketValue\n2447,42\n", realm, settings)
    assert parsed.observed_at is None


def test_timestamp_normalized_utc(realm, settings):
    parsed = parse_csv(
        b"itemId,marketValue,updatedAt\n1,2,2026-01-01T14:00:00+02:00\n", realm, settings
    )
    assert parsed.observed_at.isoformat() == "2026-01-01T12:00:00+00:00"


def test_scope_guardrails(fixture_dir, realm, region, settings):
    with pytest.raises(FeedError, match="portée"):
        parse_csv((fixture_dir / "region_items.csv").read_bytes(), realm, settings)
    with pytest.raises(FeedError, match="portée"):
        parse_csv((fixture_dir / "realm_items.csv").read_bytes(), region, settings)
    with pytest.raises(ValidationError):
        FeedSpec(scope="commodities", game_type="forever")
    with pytest.raises(ValidationError):
        FeedSpec(scope="region", realm_slug="test")
    with pytest.raises(ValidationError):
        FeedSpec(scope="realm", realm_slug="test")
    with pytest.raises(ValidationError):
        FeedSpec(scope="realm", realm_slug="../../secret", game_type="retail")
    assert FeedSpec(scope="commodities", game_type="retail").url.endswith(
        "retail/eu/commodities.csv"
    )


def test_volume_bounds(realm, settings):
    with pytest.raises(FeedError, match="lignes"):
        parse_csv(
            b"itemId,marketValue\n1,1\n2,2\n",
            realm,
            settings.model_copy(update={"max_csv_rows": 1}),
        )
    with pytest.raises(FeedError, match="volumineux"):
        parse_csv(b"x" * 1025, realm, settings.model_copy(update={"max_download_bytes": 1024}))
