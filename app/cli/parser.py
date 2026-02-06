from __future__ import annotations

import argparse

from app.utils.common import _str2bool
from app.config import DB_PATH, DATA_DIR

# Optional: if you add SITE_PARAMS_PATH to app/config.py later, we'll use it automatically.
try:
    from app.config import SITE_PARAMS_PATH  # type: ignore
except ImportError:  # pragma: no cover
    SITE_PARAMS_PATH = DATA_DIR / "site_params.json"


# ---------------------------------------------------------------------------
# Lazy command loaders (avoid importing heavy deps just to show -h)
# ---------------------------------------------------------------------------

def _cmd_sites(args: argparse.Namespace) -> int:
    from app.cli.commands.sites import cmd_sites
    return int(cmd_sites(args))


def _cmd_site_help(args: argparse.Namespace) -> int:
    from app.cli.commands.sites import cmd_site_help
    return int(cmd_site_help(args))


def _cmd_run(args: argparse.Namespace) -> int:
    from app.cli.commands.run import cmd_run
    return int(cmd_run(args))


def _cmd_sync(args: argparse.Namespace) -> int:
    from app.cli.commands.sync import cmd_sync
    return int(cmd_sync(args))


def _cmd_export(args: argparse.Namespace) -> int:
    from app.cli.commands.export import cmd_export
    return int(cmd_export(args))


def _cmd_clean(args: argparse.Namespace) -> int:
    from app.cli.commands.clean import cmd_clean
    return int(cmd_clean(args))


def _cmd_db(args: argparse.Namespace) -> int:
    from app.cli.commands.db import cmd_db
    return int(cmd_db(args))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app", description="BookSearchV2 CLI maestro")
    sub = p.add_subparsers(dest="cmd", required=True)

    # sites
    p_sites = sub.add_parser("sites", help="Listar sitios disponibles")
    p_sites.set_defaults(func=_cmd_sites)

    # site-help
    p_help = sub.add_parser("site-help", help="Ayuda de un sitio específico")
    p_help.add_argument("--site", required=True)
    p_help.set_defaults(func=_cmd_site_help)

    # -----------------
    # run
    # -----------------
    p_run = sub.add_parser("run", help="Ejecutar un sitio (query o query-file)")
    p_run.add_argument("--site", required=True)
    p_run.add_argument(
        "--fetch-detail", "--fetch_detail",
        dest="fetch_detail",
        type=_str2bool,
        nargs="?",
        const=True,
        default=True,
        help="Si False, no pide la página de detalle (/bd)."
    )
    p_run.add_argument("--query", help="Consulta única (ISBN o título)")
    p_run.add_argument("--query-file", help="Archivo .txt con consultas")
    p_run.add_argument("--max-results", type=int, default=4)
    p_run.add_argument("--delay", type=float, default=2.0)
    p_run.add_argument("--query-delay", type=float, default=1.0)
    p_run.add_argument("--batch-size", type=int, default=0)
    p_run.add_argument("--batch-pause", type=float, default=0.0)
    p_run.add_argument("--limit-queries", type=int, default=0, help="0 = sin límite")
    p_run.add_argument("--output", help="Salida CSV")
    p_run.add_argument("--db-path", default=str(DB_PATH))
    p_run.add_argument("--write-db", action="store_true", help="Upsert resultados en la DB")
    p_run.set_defaults(func=_cmd_run)

    # -----------------
    # sync
    # -----------------
    p_sync = sub.add_parser("sync", help="DB-first: busca en DB, scrappea faltantes, exporta")
    p_sync.add_argument("--query", help="ISBN único")
    p_sync.add_argument(
        "--fetch-detail", "--fetch_detail",
        dest="fetch_detail",
        type=_str2bool,
        nargs="?",
        const=True,
        default=True,
        help="Si False, no pide la página de detalle (/bd)."
    )
    p_sync.add_argument("--query-file", help="Archivo .txt con ISBNs")
    p_sync.add_argument(
        "--sites",
        nargs="*",
        default=[],
        help="Orden de sitios para buscar faltantes (ej: yenny_search bookpeople)"
    )
    p_sync.add_argument(
        "--site-params-file",
        default=str(SITE_PARAMS_PATH),
        help="JSON con overrides por sitio (delay/query_delay/batch_size/batch_pause/max_results)"
    )
    p_sync.add_argument("--db-path", default=str(DB_PATH))
    p_sync.add_argument("--remaining-out", default="", help="Archivo .txt con ISBNs que siguen faltando al final del sync")
    p_sync.add_argument("--output", default="", help="Salida CSV final (exporta desde DB al terminar)")
    p_sync.add_argument("--limit-queries", type=int, default=0)
    p_sync.add_argument("--max-results", type=int, default=4)
    p_sync.add_argument("--delay", type=float, default=2.0)
    p_sync.add_argument("--query-delay", type=float, default=1.0)
    p_sync.add_argument("--batch-size", type=int, default=0)
    p_sync.add_argument("--batch-pause", type=float, default=0.0)
    p_sync.set_defaults(func=_cmd_sync)

    # -----------------
    # export
    # -----------------
    p_export = sub.add_parser("export", help="Exportar desde DB")
    p_export.add_argument("--db-path", default=str(DB_PATH))
    p_export.add_argument("--all", action="store_true", help="Exportar toda la tabla libros")
    p_export.add_argument("--query", help="ISBN único")
    p_export.add_argument("--query-file", help="Archivo .txt con ISBNs")
    p_export.add_argument("--limit-queries", type=int, default=0)
    p_export.add_argument("--output", required=True, help="Salida CSV")
    p_export.set_defaults(func=_cmd_export)

    # -----------------
    # clean
    # -----------------
    p_clean = sub.add_parser("clean", help="Post-procesar un CSV (limpiar HTML / aplanar JSON)")
    p_clean.add_argument("--input", required=True, help="CSV de entrada")
    p_clean.add_argument("--output", help="CSV de salida (default: *_clean.csv)")
    p_clean.add_argument("--strip-html-cols", default="", help="Columnas a limpiar HTML (coma-separadas)")
    p_clean.add_argument("--flatten-json-col", default="", help="Columna JSON a aplanar (ej: info_adicional)")
    p_clean.set_defaults(func=_cmd_clean)

    # -----------------
    # db
    # -----------------
    p_db = sub.add_parser("db", help="Herramientas de base de datos (QC report, etc.)")
    p_db.add_argument("--db-path", default=str(DB_PATH))
    p_db.add_argument("--qc-report", action="store_true", help="Genera reporte QC desde book_std.raw_json")
    p_db.add_argument("--output", default="", help="Salida CSV reporte agregado (default: data/exports/qc_report.csv)")
    p_db.add_argument("--details", default="", help="Salida CSV con detalle (opcional)")
    p_db.add_argument("--details-limit", type=int, default=0, help="0 = sin límite (solo aplica a --details)")
    p_db.add_argument("--site", default="", help="Filtra por site (ej: yenny, bookpeople)")
    p_db.add_argument("--only-warn", action="store_true", help="Solo incluir severity=warn")
    p_db.add_argument("--min-count", type=int, default=1, help="Mínimo de ocurrencias para incluir en el agregado")
    p_db.set_defaults(func=_cmd_db)

    return p
