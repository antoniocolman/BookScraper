# BookSearchV2

CLI maestro + plugins de sitios + storage (SQLite) + exports.

Ver utilidades puntuales en `tools/README.md`.

## Quick start
1. Crear venv.
2. Instalar requirements.
3. Ejecutar `python -m app sites`.
4. Ejecutar `python -m app analyze-url --url "https://example.com/product"`.

## CLI (comandos frecuentes)
1. Listar sitios: `python -m app sites`
2. Scraping simple: `python -m app run --site yenny --query "978..." --output data/exports/run.csv`
3. Sync DB-first: `python -m app sync --query-file data/queries/isbn.txt --output data/exports/sync.csv`
4. Export DB: `python -m app export --all --output data/exports/all.csv`
5. Export view: `python -m app export --view book_master --output data/exports/book_master.csv`
6. Aplicar SQL: `python -m app db-apply-sql --sql app/sql/book_master.sql --db-path data/booksearchv2.db`
7. Ejecutar SQL: `python -m app db-run-sql --db-path data/booksearchv2.db --sql app/sql/10_missing_fields_summary.sql`
8. Listar views: `python -m app db --list-views`
9. Ingest Amazon JSON: `python -m app ingest-amazon --input .\Downloads\amazon_*.json`
10. Enrich queue: `python -m app enrich-queue --output data/exports/enrich_queue_amazon.csv`
11. Split queries: `python -m app split-query-file --infile data/queries/isbn.txt --outdir data/queries/chunks`
12. Amazon ingest server: `python -m app amazon-ingest-server --db-path data/booksearchv2.db`
13. DB bootstrap: `python -m app db-bootstrap --db-url "postgresql+psycopg://user:pass@host/db"`
14. DB validate: `python -m app db-validate --db-url "postgresql+psycopg://user:pass@host/db"`
