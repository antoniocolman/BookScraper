SELECT isbn, site, encuadernacion
FROM book_std
WHERE isbn IN (?,?,?)
ORDER BY site;