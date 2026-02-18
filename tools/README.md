# Tools (one-off)

Este directorio contiene utilidades puntuales para mantenimiento o tareas manuales.

Herramientas
- `amazon_assisted.py`: scraping asistido de Amazon con Selenium (modo manual/captcha).
- `backfill_std_fields.py`: backfill de encuadernacion/categoria desde `raw_json`.
- `crawl_el_lector_catalog.py`: crawl completo del catalogo de El Lector y export CSV.
- `db_inspect.py`: inspeccion basica de schema/indices de `book_std`.
- `fix_encuadernacion_typos.py`: corrige typos en `encuadernacion` y `raw_json`.
- `merge_book_std.py`: merge de tabla `book_std` entre dos DBs.
- `requeue_from_csv.py`: genera un nuevo archivo de queries no encontradas.

Comandos equivalentes en CLI (recomendado)
- `db-apply-sql`: aplica un .sql a la DB.
- `db-run-sql`: ejecuta SQL con parametros y export opcional.
- `ingest-amazon`: ingesta JSON de Amazon en `book_std`.
- `enrich-queue`: genera CSV de cola para enrich Amazon.
- `split-query-file`: divide un archivo de queries en chunks.
- `amazon-ingest-server`: server HTTP para ingest/enrich Amazon.
- `export --view`: exporta views (ej. `book_master`).
- `db --list-views`: lista views de la DB.
