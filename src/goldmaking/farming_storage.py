"""Atomic immutable archives of catalogues built by the local reader."""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from goldmaking.models import FarmingCatalog, FarmingTarget, FarmingVariant
from goldmaking.route_catalog import digest


def archive_catalog(engine: Engine, catalog: dict[str, Any], allow_synthetic: bool = False) -> str:
    if catalog.get("schema_version") != "zygor-profile-catalog-v2":
        raise ValueError("Version du catalogue non prise en charge")
    dataset = catalog.get("dataset")
    if dataset not in {"real", "synthetic"} or (dataset == "synthetic" and not allow_synthetic):
        raise ValueError("Dataset refusé : autorisation fictive explicite requise")
    inspected = datetime.fromisoformat(catalog["inspected_at"])
    if inspected.tzinfo is None:
        raise ValueError("Date d'inspection sans fuseau")
    metadata = {k: v for k, v in catalog.items() if k not in {"variants", "inspected_at"}}
    variants = catalog["variants"]
    if len(json.dumps(metadata).encode()) > 1024 * 1024:
        raise ValueError("Métadonnées trop volumineuses")
    if not 1 <= len(variants) <= 5000:
        raise ValueError("Nombre de variantes hors limites (1–5000)")
    if len({v["variant_id"] for v in variants}) != len(variants):
        raise ValueError("Variante dupliquée")
    if len(json.dumps(catalog).encode()) > 64 * 1024 * 1024:
        raise ValueError("Catalogue trop volumineux")
    for variant in variants:
        if len(json.dumps(variant).encode()) > 128 * 1024:
            raise ValueError("Variante trop volumineuse")
        ids = variant["route"]["target_item_ids"]
        if len(ids) > 500 or any(type(i) is not int or not 0 < i <= 2**31 - 1 for i in ids):
            raise ValueError("Objets cibles invalides")
        if variant["assessment"]["eligible_for_recommendation"] is not False:
            raise ValueError("Catalogue de candidats uniquement")
    catalog_id = digest(
        {"metadata": metadata, "variants": sorted(variants, key=lambda v: v["variant_id"])}
    )
    metadata["inspected_at"] = inspected.astimezone(UTC).isoformat()
    try:
        with Session(engine) as session, session.begin():
            if session.get(FarmingCatalog, catalog_id) is not None:
                return catalog_id
            session.add(
                FarmingCatalog(
                    id=catalog_id,
                    dataset=dataset,
                    created_at=datetime.now(UTC),
                    metadata_json=metadata,
                )
            )
            session.flush()
            session.add_all(
                FarmingVariant(catalog_id=catalog_id, variant_id=v["variant_id"], payload=v)
                for v in variants
            )
            session.flush()
            session.add_all(
                FarmingTarget(catalog_id=catalog_id, variant_id=v["variant_id"], item_id=i)
                for v in variants
                for i in sorted(set(v["route"]["target_item_ids"]))
            )
    except IntegrityError:
        # Unique identity also handles simultaneous replay of the same catalogue.
        with Session(engine) as session:
            if session.get(FarmingCatalog, catalog_id) is None:
                raise
    return catalog_id
