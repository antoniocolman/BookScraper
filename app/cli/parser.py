from __future__ import annotations
import argparse

from app.cli.commands import (
    cmd_sites, cmd_site_help, cmd_run, cmd_sync, cmd_export, cmd_clean, cmd_db
)
from app.utils.common import _str2bool, _repo_root

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app", description="BookSearchV2 CLI maestro")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_sites = sub.add_parser("sites", help="Listar sitios disponibles")
    
    p_sites.set_defaults(func=cmd_sites)
    p_help = sub.add_parser("site-help", help="Ayuda de un sitio específico")
    p_help.add_argument("--site", required=True)
    p_help.set_defaults(func=cmd_site_help)
    
    # -----------------
    # run 
    # -----------------
    
    p_run = sub.add_parser("run", help="Ejecutar un sitio (query o query-file)")
    p_run.add_argument("--site", required=True)
    p_run.add_argument( "--fetch-detail", "--fetch_detail",
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
    p_run.add_argument("--db-path", default=str(_repo_root() / "data" / "booksearchv2.db"))
    p_run.add_argument("--write-db", action="store_true", help="Upsert resultados en la DB")
    p_run.set_defaults(func=cmd_run)
    
    # -----------------
    # sync
    #  -----------------
    
    p_sync = sub.add_parser("sync", help="DB-first: busca en DB, scrappea faltantes, exporta")
    p_sync.add_argument("--query", help="ISBN único")
    p_sync.add_argument( "--fetch-detail",
                        "--fetch_detail",
                        dest="fetch_detail",
                        type=_str2bool,
                        nargs="?",
                        const=True,
                        default=True, help="Si False, no pide la página de detalle (/bd)." )
    p_sync.add_argument("--query-file", help="Archivo .txt con ISBNs")
    p_sync.add_argument("--sites",
                        nargs="*",
                        default=[],
                        help="Orden de sitios para buscar faltantes (ej: yenny_search bookpeople)"
                        )
    p_sync.add_argument("--site-params-file",
                        default=str(_repo_root() / "data" / "site_params.json"),
                        help="JSON con overrides por sitio (delay/query_delay/batch_size/batch_pause/max_results)")
    p_sync.add_argument("--db-path", default=str(_repo_root() / "data" / "booksearchv2.db"))
    p_sync.add_argument("--remaining-out", default="", help="Archivo .txt con ISBNs que siguen faltando al final del sync")
    p_sync.add_argument("--output", default="", help="Salida CSV final (exporta desde DB al terminar)")
    p_sync.add_argument("--limit-queries", type=int, default=0)
    p_sync.add_argument("--max-results", type=int, default=4)
    p_sync.add_argument("--delay", type=float, default=2.0)
    p_sync.add_argument("--query-delay", type=float, default=1.0)
    p_sync.add_argument("--batch-size", type=int, default=0)
    p_sync.add_argument("--batch-pause", type=float, default=0.0)
    p_sync.set_defaults(func=cmd_sync)
    
    # ----------------- 
    #  export 
    # ----------------- 

    p_export = sub.add_parser("export", help="Exportar desde DB")
    p_export.add_argument("--db-path", default=str(_repo_root() / "data" / "booksearchv2.db"))
    p_export.add_argument("--all", action="store_true", help="Exportar toda la tabla libros")
    p_export.add_argument("--query", help="ISBN único")
    p_export.add_argument("--query-file", help="Archivo .txt con ISBNs")
    p_export.add_argument("--limit-queries", type=int, default=0)
    p_export.add_argument("--output", required=True, help="Salida CSV")
    p_export.set_defaults(func=cmd_export)
    
    # -----------------
    # Clean 
    #  -----------------
    
    p_clean = sub.add_parser("clean", help="Post-procesar un CSV (limpiar HTML / aplanar JSON)")
    p_clean.add_argument("--input", required=True, help="CSV de entrada")
    p_clean.add_argument("--output", help="CSV de salida (default: *_clean.csv)")
    p_clean.add_argument("--strip-html-cols", default="", help="Columnas a limpiar HTML (coma-separadas)")
    p_clean.add_argument("--flatten-json-col", default="", help="Columna JSON a aplanar (ej: info_adicional)")
    p_clean.set_defaults(func=cmd_clean)
    
    # ----------------- 
    #  db 
    # ----------------- 

    p_db = sub.add_parser("db", help="Herramientas de base de datos (QC report, etc.)")
    p_db.add_argument("--db-path", default=str(_repo_root() / "data" / "booksearchv2.db"))
    p_db.add_argument("--qc-report", action="store_true", help="Genera reporte QC desde book_std.raw_json")
    p_db.add_argument("--output", default="", help="Salida CSV reporte agregado (default: data/exports/qc_report.csv)")
    p_db.add_argument("--details", default="", help="Salida CSV con detalle (opcional)")
    p_db.add_argument("--details-limit", type=int, default=0, help="0 = sin límite (solo aplica a --details)")
    p_db.add_argument("--site", default="", help="Filtra por site (ej: yenny, bookpeople)")
    p_db.add_argument("--only-warn", action="store_true", help="Solo incluir severity=warn")
    p_db.add_argument("--min-count",
                      type=int,
                      default=1,
                      help="Mínimo de ocurrencias para incluir en el agregado")
    p_db.set_defaults(func=cmd_db)
    
    return p