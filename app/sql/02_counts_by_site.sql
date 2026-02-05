SELECT site, COUNT(*) AS c
FROM book_std
GROUP BY site
ORDER BY c DESC;