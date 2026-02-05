SELECT
  site,
  SUM(CASE WHEN IFNULL(TRIM(encuadernacion),'')='' THEN 1 ELSE 0 END) AS missing_encuadernacion,
  SUM(CASE WHEN IFNULL(TRIM(categoria),'')='' THEN 1 ELSE 0 END) AS missing_categoria,
  COUNT(*) AS total
FROM book_std
GROUP BY site
ORDER BY total DESC;