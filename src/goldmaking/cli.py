import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from goldmaking.analysis import analyze_if_changed, analyze_market, primary_source_id
from goldmaking.config import Settings
from goldmaking.db import make_engine
from goldmaking.detector import CostProfile, DetectorParameters
from goldmaking.farming_options import find_options
from goldmaking.farming_storage import archive_catalog
from goldmaking.gathering import inspect_gathermate
from goldmaking.ingestion import IngestionError, Ingestor
from goldmaking.route_catalog import CharacterProfile, build_catalog
from goldmaking.tsm import FeedError, FeedSpec
from goldmaking.zygor import inspect_zygor


def load_feeds(path: Path) -> list[FeedSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not 1 <= len(data) <= 100:
        raise FeedError("Le manifeste exige entre 1 et 100 flux")
    feeds = [FeedSpec.model_validate(row) for row in data]
    if any(f.transport != "http" for f in feeds):
        raise FeedError("Le manifeste de collecte n'accepte que les flux HTTP")
    if len({f.source_id for f in feeds}) != len(feeds):
        raise FeedError("Flux dupliqués dans le manifeste")
    return feeds


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingestion économique WoW (aucune action en jeu)")
    sub = parser.add_subparsers(dest="command", required=True)
    gathering = sub.add_parser(
        "inspect-gathermate", help="Normaliser un addon local sans exécuter Lua"
    )
    gathering.add_argument("path", type=Path, help="Dossier GatherMate2_Data extrait")
    gathering.add_argument("--dataset", required=True, choices=["real", "synthetic"])
    zygor = sub.add_parser(
        "inspect-zygor", help="Inspecter les parcours Zygor locaux sans exécuter Lua"
    )
    zygor.add_argument("path", type=Path)
    zygor.add_argument("--dataset", required=True, choices=["real", "synthetic"])
    for name in ("catalog-zygor", "find-farming", "import-farming"):
        catalog = sub.add_parser(name, help="Consulter les parcours candidats par profil")
        catalog.add_argument("path", type=Path)
        catalog.add_argument("--dataset", required=True, choices=["real", "synthetic"])
        catalog.add_argument(
            "--profile", type=Path, required=True, help="Profil JSON local, maximum 8 Kio"
        )
        if name == "find-farming":
            catalog.add_argument("--item-id", type=int, required=True)
            catalog.add_argument("--limit", type=int, default=20)
    for command in ("import-tsm", "fetch-tsm"):
        cmd = sub.add_parser(command)
        if command == "import-tsm":
            cmd.add_argument("path", type=Path)
            cmd.add_argument("--synthetic", action="store_true", help="Données fictives isolées")
        cmd.add_argument("--game-type", default="forever")
        cmd.add_argument("--region", default="eu")
        cmd.add_argument("--scope", required=True, choices=["realm", "region", "commodities"])
        cmd.add_argument("--realm-slug")
        cmd.add_argument("--ruleset")
        cmd.add_argument("--faction", choices=["alliance", "horde", "neutral", "shared", "unknown"])
    sub.add_parser("demo", help="Charger uniquement les fixtures fictives dans cinq flux EU")
    poll = sub.add_parser("poll", help="Collecter un manifeste HTTP en respectant les cadences")
    poll.add_argument("manifest", type=Path)
    poll.add_argument("--watch", action="store_true")
    poll.add_argument(
        "--analyze-primary",
        action="store_true",
        help="Analyser Normal Alliance après nouvel import, avec rattrapage au redémarrage",
    )
    analyze = sub.add_parser("analyze-market", help="Analyser et archiver sans action dans le jeu")
    analyze.add_argument("--source-id", help="Défaut : TSM réel Forever EU Normal Alliance HTTP")
    analyze.add_argument("--as-of", type=datetime.fromisoformat, help="Date ISO avec fuseau")
    analyze.add_argument("--parameters", type=Path, help="Paramètres JSON, maximum 8 Kio")
    analyze.add_argument(
        "--cost-profile", type=Path, help="Profil hypothétique JSON, maximum 64 Kio"
    )
    analyze.add_argument("--budget", type=int, help="Cuivres entiers, facultatif")
    analyze.add_argument("--item-id", type=int, help="Limiter l'analyse à un objet")
    analyze.add_argument("--synthetic", action="store_true", help="Autoriser une source fictive")
    args = parser.parse_args()
    if args.command in {"catalog-zygor", "find-farming", "import-farming"}:
        try:
            with args.profile.open("rb") as stream:
                content = stream.read(8193)
            if len(content) > 8192:
                raise ValueError("Profil trop volumineux")
            character_profile = CharacterProfile.model_validate_json(content)
            catalog_result = build_catalog(args.path, args.dataset, character_profile)
            if args.command == "import-farming":
                archive_engine = make_engine(Settings().database_url)
                try:
                    catalog_id = archive_catalog(
                        archive_engine, catalog_result, args.dataset == "synthetic"
                    )
                finally:
                    archive_engine.dispose()
                print(json.dumps({"catalog_id": catalog_id, "status": "archived"}))
                return
            if args.command == "find-farming":
                catalog_result = find_options(catalog_result, args.item_id, args.limit)
            print(json.dumps(catalog_result, ensure_ascii=True))
        except SQLAlchemyError:
            parser.exit(2, "Archivage impossible : vérifier la base et les migrations Alembic.\n")
        except (OSError, ValueError) as exc:
            parser.exit(2, f"Catalogue refusé : {exc}\n")
        return
    if args.command in {"inspect-gathermate", "inspect-zygor"}:
        try:
            inspector = inspect_zygor if args.command == "inspect-zygor" else inspect_gathermate
            print(json.dumps(inspector(args.path, args.dataset), ensure_ascii=False))
        except (OSError, ValueError) as exc:
            parser.exit(2, f"Inspection refusée : {exc}\n")
        return
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(message)s")
    engine = make_engine(settings.database_url)
    ingestor = Ingestor(engine, settings)
    try:
        if args.command == "analyze-market":

            def read_contract(path: Path, maximum: int) -> str:
                with path.open("rb") as file:
                    content = file.read(maximum + 1)
                if len(content) > maximum:
                    raise ValueError("Fichier de paramètres trop volumineux")
                return content.decode("utf-8")

            parameters = (
                None
                if args.parameters is None
                else DetectorParameters.model_validate_json(read_contract(args.parameters, 8192))
            )
            profile = (
                None
                if args.cost_profile is None
                else CostProfile.model_validate_json(read_contract(args.cost_profile, 65536))
            )
            analysis_id = analyze_market(
                engine,
                args.source_id,
                args.as_of,
                parameters,
                profile,
                args.budget,
                args.item_id,
                args.synthetic,
            )
            print(json.dumps({"analysis_id": analysis_id, "status": "complete"}))
        elif args.command == "demo":
            root = Path("fixtures")
            if not root.exists():
                raise FeedError("Exécuter demo depuis la racine du dépôt")
            for ruleset in ("normal", "pvp"):
                for faction in ("alliance", "horde"):
                    spec = FeedSpec(
                        scope="realm",
                        realm_slug=f"fixture-{ruleset}-{faction}",
                        ruleset=ruleset,
                        faction=faction,
                        dataset="synthetic",
                    )
                    for file in ("realm_items.csv", "realm_items_later.csv"):
                        print(json.dumps(asdict(ingestor.import_file(root / file, spec))))
            print(
                json.dumps(
                    asdict(
                        ingestor.import_file(
                            root / "region_items.csv", FeedSpec(scope="region", dataset="synthetic")
                        )
                    )
                )
            )
        elif args.command == "poll":
            feeds = load_feeds(args.manifest)
            if args.analyze_primary and primary_source_id() not in {f.source_id for f in feeds}:
                raise FeedError(
                    "--analyze-primary exige le flux HTTP réel Forever EU Normal Alliance"
                )
            while True:
                failures = 0
                for feed in feeds:
                    try:
                        print(json.dumps(asdict(ingestor.fetch(feed))), flush=True)
                    except IngestionError:
                        failures += 1
                if args.analyze_primary:
                    try:
                        automatic_id = analyze_if_changed(engine)
                        if automatic_id is not None:
                            print(
                                json.dumps(
                                    {
                                        "event": "analysis_finished",
                                        "analysis_id": automatic_id,
                                        "source_id": primary_source_id(),
                                        "status": "complete",
                                    }
                                ),
                                flush=True,
                            )
                    except (ValueError, SQLAlchemyError):
                        # L'archivage est atomique ; réessayer au prochain cycle.
                        # Ne pas divulguer de paramètres SQL ou d'URL de connexion.
                        failures += 1
                        print(
                            json.dumps(
                                {
                                    "event": "analysis_failed",
                                    "source_id": primary_source_id(),
                                    "error": "Analyse non enregistrée ; vérifier les limites",
                                }
                            ),
                            file=sys.stderr,
                            flush=True,
                        )
                if not args.watch:
                    if failures:
                        sys.exit(1)
                    break
                time.sleep(60)
        else:
            spec = FeedSpec(
                game_type=args.game_type,
                region=args.region,
                scope=args.scope,
                realm_slug=args.realm_slug,
                ruleset=args.ruleset,
                faction=args.faction,
                dataset="synthetic" if getattr(args, "synthetic", False) else "real",
                transport="local" if args.command == "import-tsm" else "http",
            )
            result = (
                ingestor.import_file(args.path, spec)
                if args.command == "import-tsm"
                else ingestor.fetch(spec)
            )
            print(json.dumps(asdict(result)))
    except (ValueError, OSError) as exc:
        print(
            json.dumps({"error": str(exc), "run_id": getattr(exc, "run_id", None)}), file=sys.stderr
        )
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
