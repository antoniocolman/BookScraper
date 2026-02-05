SELECT
  site,
  COUNT(*) AS total,
  SUM(CASE WHEN IFNULL(TRIM(isbn),'')='' THEN 1 ELSE 0 END) AS missing_isbn,
  SUM(CASE WHEN IFNULL(TRIM(titulo),'')='' THEN 1 ELSE 0 END) AS missing_titulo,
  SUM(CASE WHEN IFNULL(TRIM(autor),'')='' THEN 1 ELSE 0 END) AS missing_autor,
  SUM(CASE WHEN IFNULL(TRIM(editorial),'')='' THEN 1 ELSE 0 END) AS missing_editorial,
  SUM(CASE WHEN IFNULL(TRIM(encuadernacion),'')='' THEN 1 ELSE 0 END) AS missing_encuadernacion,
  SUM(CASE WHEN IFNULL(TRIM(categoria),'')='' THEN 1 ELSE 0 END) AS missing_categoria,
  SUM(CASE WHEN IFNULL(TRIM(sinopsis),'')='' THEN 1 ELSE 0 END) AS missing_sinopsis,
  SUM(CASE WHEN IFNULL(TRIM(idioma),'')='' THEN 1 ELSE 0 END) AS missing_idioma,
  SUM(CASE WHEN IFNULL(TRIM(paginas),'')='' THEN 1 ELSE 0 END) AS missing_paginas,
  SUM(CASE WHEN IFNULL(TRIM(dimensiones),'')='' THEN 1 ELSE 0 END) AS missing_dimensiones,
  SUM(CASE WHEN IFNULL(TRIM(url_portada),'')='' THEN 1 ELSE 0 END) AS missing_portada
FROM book_std
GROUP BY site
ORDER BY total DESC;