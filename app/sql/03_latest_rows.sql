SELECT isbn, titulo, site, encuadernacion, updated_at
FROM book_std
ORDER BY updated_at DESC
LIMIT 30;