-- Validation queries (SQLite, no views)

-- Total rows
SELECT COUNT(*) AS total_rows FROM book_std;

-- Count by site
SELECT site, COUNT(*) AS c FROM book_std GROUP BY site ORDER BY c DESC;

-- Duplicate PK check (should be 0 rows)
SELECT site, url, COUNT(*) AS c
FROM book_std
GROUP BY site, url
HAVING COUNT(*) > 1;

-- Missing key fields
SELECT COUNT(*) AS missing_isbn FROM book_std WHERE isbn IS NULL OR TRIM(isbn) = '';
SELECT COUNT(*) AS missing_titulo FROM book_std WHERE titulo IS NULL OR TRIM(titulo) = '';
SELECT COUNT(*) AS missing_autor FROM book_std WHERE autor IS NULL OR TRIM(autor) = '';
SELECT COUNT(*) AS missing_sinopsis FROM book_std WHERE sinopsis IS NULL OR TRIM(sinopsis) = '';
