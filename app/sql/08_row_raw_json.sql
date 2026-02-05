SELECT isbn, site, encuadernacion, raw_json, updated_at
FROM book_std
WHERE isbn = ? AND site = ?
ORDER BY updated_at DESC
LIMIT 1;