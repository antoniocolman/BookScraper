UPDATE book_std
SET encuadernacion = 'Rústica',
    updated_at = datetime('now')
WHERE lower(replace(replace(trim(encuadernacion), char(160), ''), char(9), '')) IN ('rstica','rustica');

SELECT changes() AS rows_updated;
