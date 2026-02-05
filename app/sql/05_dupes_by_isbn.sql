SELECT site, isbn, COUNT(*) AS c
FROM book_std
WHERE isbn IS NOT NULL AND TRIM(isbn) <> ''
GROUP BY site, isbn
HAVING COUNT(*) > 1
ORDER BY c DESC, site, isbn;