SELECT
  site,
  encuadernacion,
  hex(encuadernacion) AS hex,
  length(encuadernacion) AS len,
  COUNT(*) AS c
FROM book_std
WHERE
  lower(replace(replace(trim(encuadernacion), char(160), ''), char(9), '')) IN ('rstica','rustica')
  OR lower(encuadernacion) LIKE '%rstica%'
  OR lower(encuadernacion) LIKE '%rustica%'
GROUP BY site, encuadernacion
ORDER BY c DESC;