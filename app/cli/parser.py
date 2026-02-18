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


def _cmd_db_apply_sql(args: argparse.Namespace) -> int:
    from app.cli.commands.db_apply_sql import cmd_db_apply_sql
    return int(cmd_db_apply_sql(args))


def _cmd_db_run_sql(args: argparse.Namespace) -> int:
    from app.cli.commands.db_run_sql import cmd_db_run_sql
    return int(cmd_db_run_sql(args))


def _cmd_ingest_amazon(args: argparse.Namespace) -> int:
    from app.cli.commands.ingest_amazon import cmd_ingest_amazon
    return int(cmd_ingest_amazon(args))


def _cmd_enrich_queue(args: argparse.Namespace) -> int:
    from app.cli.commands.enrich_queue import cmd_enrich_queue
    return int(cmd_enrich_queue(args))


def _cmd_split_query_file(args: argparse.Namespace) -> int:
    from app.cli.commands.split_query_file import cmd_split_query_file
    return int(cmd_split_query_file(args))


def _cmd_amazon_ingest_server(args: argparse.Namespace) -> int:
    from app.cli.commands.amazon_ingest_server import cmd_amazon_ingest_server
    return int(cmd_amazon_ingest_server(args))

def _cmd_db_bootstrap(args: argparse.Namespace) -> int:
    from app.cli.commands.db_bootstrap import cmd_db_bootstrap
    return int(cmd_db_bootstrap(args))

def _cmd_db_validate(args: argparse.Namespace) -> int:
    from app.cli.commands.db_validate import cmd_db_validate
    return int(cmd_db_validate(args))


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
    p_help = sub.add_parser("site-help", help="Ayuda de un sitio especÃ­fico")
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
        help="Si False, no pide la pÃ¡gina de detalle (/bd)."
    )
    p_run.add_argument("--query", help="Consulta Ãºnica (ISBN o tÃ­tulo)")
    p_run.add_argument("--query-file", help="Archivo .txt con consultas")
    p_run.add_argument("--max-results", type=int, default=4)
    p_run.add_argument("--delay", type=float, default=2.0)
    p_run.add_argument("--query-delay", type=float, default=1.0)
    p_run.add_argument("--batch-size", type=int, default=0)
    p_run.add_argument("--batch-pause", type=float, default=0.0)
    p_run.add_argument("--limit-queries", type=int, default=0, help="0 = sin lÃ­mite")
    p_run.add_argument("--output", help="Salida CSV")
    p_run.add_argument("--db-path", default=str(DB_PATH))
    p_run.add_argument("--write-db", action="store_true", help="Upsert resultados en la DB")
    p_run.set_defaults(func=_cmd_run)

    # -----------------
    # sync
    # -----------------
    p_sync = sub.add_parser("sync", help="DB-first: busca en DB, scrappea faltantes, exporta")
    p_sync.add_argument("--query", help="ISBN Ãºnico")
    p_sync.add_argument(
        "--fetch-detail", "--fetch_detail",
        dest="fetch_detail",
        type=_str2bool,
        nargs="?",
        const=True,
        default=True,
        help="Si False, no pide la pÃ¡gina de detalle (/bd)."
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
    p_export.add_argument("--view", default="", help="Exportar una view (ej: book_master)")
    p_export.add_argument("--query", help="ISBN Ãºnico")
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
    p_db.add_argument("--list-views", action="store_true", help="Lista views en sqlite_master")
    p_db.add_argument("--output", default="", help="Salida CSV reporte agregado (default: data/exports/qc_report.csv)")
    p_db.add_argument("--details", default="", help="Salida CSV con detalle (opcional)")
    p_db.add_argument("--details-limit", type=int, default=0, help="0 = sin lÃ­mite (solo aplica a --details)")
    p_db.add_argument("--site", default="", help="Filtra por site (ej: yenny, bookpeople)")
    p_db.add_argument("--only-warn", action="store_true", help="Solo incluir severity=warn")
    p_db.add_argument("--min-count", type=int, default=1, help="MÃ­nimo de ocurrencias para incluir en el agregado")
    p_db.set_defaults(func=_cmd_db)

    # -----------------
    # db-apply-sql
    # -----------------
    p_db_apply = sub.add_parser("db-apply-sql", help="Aplica un archivo .sql a la DB (executescript)")
    p_db_apply.add_argument("--sql", default="app/sql/book_master.sql", help="Ruta al .sql")
    p_db_apply.add_argument("--db-path", default=str(DB_PATH), help="Ruta a la DB .db")
    p_db_apply.set_defaults(func=_cmd_db_apply_sql)

    # -----------------
    # db-run-sql
    # -----------------
    p_db_run = sub.add_parser("db-run-sql", help="Ejecuta SQL con params; opcional export a CSV")
    p_db_run.add_argument("--db-path", default=str(DB_PATH), help="Ruta a la DB sqlite")
    p_db_run.add_argument("--sql", required=True, help="Ruta al archivo .sql")
    p_db_run.add_argument("--param", action="append", default=[], help="ParÃ¡metro individual (se puede repetir)")
    p_db_run.add_argument("--params", default="", help="ParÃ¡metros (JSON o lista relajada)")
    p_db_run.add_argument("--params-file", default="", help="Archivo .json con lista de params")
    p_db_run.add_argument("--limit", type=int, default=200, help="LÃ­mite de filas a imprimir (0 = sin lÃ­mite)")
    p_db_run.add_argument("--output", default="", help="Opcional: exportar resultado a CSV")
    p_db_run.set_defaults(func=_cmd_db_run_sql)

    # -----------------
    # ingest-amazon
    # -----------------
    p_ingest_amz = sub.add_parser("ingest-amazon", help="Ingesta JSON Amazon en book_std")
    p_ingest_amz.add_argument("--input", required=True, help="Archivo .json, carpeta o glob")
    p_ingest_amz.add_argument("--db-path", default=str(DB_PATH), help="Ruta DB SQLite")
    p_ingest_amz.set_defaults(func=_cmd_ingest_amazon)

    # -----------------
    # enrich-queue
    # -----------------
    p_enrich = sub.add_parser("enrich-queue", help="Genera CSV de cola para enrich Amazon")
    p_enrich.add_argument("--db-path", default=str(DB_PATH))
    p_enrich.add_argument("--limit", type=int, default=300)
    p_enrich.add_argument("--output", default=str(DATA_DIR / "exports" / "enrich_queue_amazon.csv"))
    p_enrich.set_defaults(func=_cmd_enrich_queue)

    # -----------------
    # split-query-file
    # -----------------
    p_split = sub.add_parser("split-query-file", help="Divide archivo de queries en chunks")
    p_split.add_argument("--infile", required=True)
    p_split.add_argument("--outdir", required=True)
    p_split.add_argument("--batch", type=int, default=50)
    p_split.add_argument("--prefix", default=None)
    p_split.set_defaults(func=_cmd_split_query_file)

    # -----------------
    # amazon-ingest-server
    # -----------------
    p_srv = sub.add_parser("amazon-ingest-server", help="Server HTTP para ingest/enrich Amazon")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8765)
    p_srv.add_argument("--db-path", default=str(DB_PATH))
    p_srv.add_argument("--queue-file", default=str(DATA_DIR / "exports" / "enrich_queue_amazon.csv"))
    p_srv.add_argument("--state-file", default=str(DATA_DIR / "state" / "enrich_queue_state.json"))
    p_srv.add_argument("--mode", default="local", choices=["local", "lan"])
    p_srv.add_argument("--users-file", default="", help="JSON: {'admin':'KEY','PC-A':'KEY2'}")
    p_srv.add_argument("--admin-user", default="admin")
    p_srv.add_argument("--api-key", default="")
    p_srv.add_argument("--lease-ttl", type=int, default=900)
    p_srv.set_defaults(func=_cmd_amazon_ingest_server)

    # -----------------
    # db-bootstrap
    # -----------------
    p_boot = sub.add_parser("db-bootstrap", help="Aplica bootstrap SQL (schema + views)")
    p_boot.add_argument("--db-url", default="", help="DATABASE_URL override (opcional)")
    p_boot.add_argument("--sql", default="app/db/migrations/bootstrap.sql", help="Ruta al SQL idempotente")
    p_boot.set_defaults(func=_cmd_db_bootstrap)

    # -----------------
    # db-validate
    # -----------------
    p_val = sub.add_parser("db-validate", help="Ejecuta queries de validacion (conteos/integridad)")
    p_val.add_argument("--db-url", default="", help="DATABASE_URL override (opcional)")
    p_val.add_argument("--sql", default="app/db/migrations/validation.sql", help="Ruta al SQL de validacion")
    p_val.add_argument("--limit", type=int, default=200, help="Limite de filas a imprimir (0 = sin limite)")
    p_val.set_defaults(func=_cmd_db_validate)

    return p
