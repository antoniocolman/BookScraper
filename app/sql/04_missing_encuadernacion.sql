SELECT isbn, titulo, site, url, updated_at
FROM book_std
WHERE encuadernacion IS NULL OR TRIM(encuadernacion) = ''
ORDER BY updated_at DESC
LIMIT 100;