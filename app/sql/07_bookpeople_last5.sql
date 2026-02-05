SELECT isbn, site, encuadernacion, updated_at
FROM book_std
WHERE site = ?
ORDER BY updated_at DESC
LIMIT 5;