DROP VIEW IF EXISTS book_best_std;
DROP VIEW IF EXISTS book_isbn_site;
DROP VIEW IF EXISTS book_master;

-- 1) Elegimos 1 fila "mejor" por (isbn, site), sin window functions
CREATE VIEW book_isbn_site AS
WITH scored AS (
  SELECT
    url, site, isbn, titulo, autor, editorial, sinopsis, idioma,
    paginas, dimensiones, fecha_publicacion, url_portada,
    raw_json, updated_at,
    (
      (CASE WHEN trim(ifnull(titulo,''))<>'' THEN 10 ELSE 0 END) +
      (CASE WHEN trim(ifnull(autor,''))<>'' THEN 10 ELSE 0 END) +
      (CASE WHEN trim(ifnull(editorial,''))<>'' THEN 8 ELSE 0 END) +
      (CASE WHEN trim(ifnull(sinopsis,''))<>'' THEN 20 ELSE 0 END) +
      (CASE WHEN trim(ifnull(idioma,''))<>'' THEN 5 ELSE 0 END) +
      (CASE WHEN trim(ifnull(paginas,''))<>'' THEN 5 ELSE 0 END) +
      (CASE WHEN trim(ifnull(dimensiones,''))<>'' THEN 5 ELSE 0 END) +
      (CASE WHEN trim(ifnull(fecha_publicacion,''))<>'' THEN 5 ELSE 0 END) +
      (CASE WHEN trim(ifnull(url_portada,''))<>'' THEN 4 ELSE 0 END)
    ) AS score
  FROM book_std
  WHERE trim(ifnull(isbn,'')) <> ''
)
SELECT s1.url, s1.site, s1.isbn, s1.titulo, s1.autor, s1.editorial, s1.sinopsis, s1.idioma,
       s1.paginas, s1.dimensiones, s1.fecha_publicacion, s1.url_portada,
       s1.raw_json, s1.updated_at
FROM scored s1
WHERE NOT EXISTS (
  SELECT 1
  FROM scored s2
  WHERE s2.isbn = s1.isbn AND s2.site = s1.site
    AND (
      s2.score > s1.score
      OR (s2.score = s1.score AND s2.updated_at > s1.updated_at)
      OR (s2.score = s1.score AND s2.updated_at = s1.updated_at AND s2.url < s1.url)
    )
);

CREATE TABLE book_std (
    url TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    isbn TEXT NOT NULL,
    titulo TEXT,
    autor TEXT,
    editorial TEXT,
    sinopsis TEXT,
    idioma TEXT,
    paginas TEXT,
    dimensiones TEXT,
    fecha_publicacion TEXT,
    url_portada TEXT,
    raw_json TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_isbn_site ON book_std(isbn, site);
CREATE INDEX idx_isbn_updated ON book_std(isbn, updated_at DESC);

-- 2) Alias estable: book_best_std
CREATE VIEW book_best_std AS
SELECT * FROM book_isbn_site;

-- 3) Master: 1 fila por ISBN (usa best_std)
CREATE VIEW book_master AS
WITH s AS (
  SELECT * FROM book_best_std
),
agg AS (
  SELECT
    isbn,

    COALESCE(
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(titulo,''))<>'' THEN titulo END),
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(titulo,''))<>'' THEN titulo END),
      MAX(CASE WHEN trim(ifnull(titulo,''))<>'' THEN titulo END)
    ) AS titulo,

    COALESCE(
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(autor,''))<>'' THEN autor END),
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(autor,''))<>'' THEN autor END),
      MAX(CASE WHEN trim(ifnull(autor,''))<>'' THEN autor END)
    ) AS autor,

    COALESCE(
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(editorial,''))<>'' THEN editorial END),
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(editorial,''))<>'' THEN editorial END),
      MAX(CASE WHEN trim(ifnull(editorial,''))<>'' THEN editorial END)
    ) AS editorial,

    COALESCE(
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(sinopsis,''))<>'' THEN sinopsis END),
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(sinopsis,''))<>'' THEN sinopsis END),
      MAX(CASE WHEN trim(ifnull(sinopsis,''))<>'' THEN sinopsis END)
    ) AS sinopsis,

    COALESCE(
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(idioma,''))<>'' THEN idioma END),
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(idioma,''))<>'' THEN idioma END),
      MAX(CASE WHEN trim(ifnull(idioma,''))<>'' THEN idioma END)
    ) AS idioma,

    COALESCE(
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(paginas,''))<>'' THEN paginas END),
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(paginas,''))<>'' THEN paginas END),
      MAX(CASE WHEN trim(ifnull(paginas,''))<>'' THEN paginas END)
    ) AS paginas,

    COALESCE(
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(dimensiones,''))<>'' THEN dimensiones END),
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(dimensiones,''))<>'' THEN dimensiones END),
      MAX(CASE WHEN trim(ifnull(dimensiones,''))<>'' THEN dimensiones END)
    ) AS dimensiones,

    COALESCE(
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(fecha_publicacion,''))<>'' THEN fecha_publicacion END),
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(fecha_publicacion,''))<>'' THEN fecha_publicacion END),
      MAX(CASE WHEN trim(ifnull(fecha_publicacion,''))<>'' THEN fecha_publicacion END)
    ) AS fecha_publicacion,

    COALESCE(
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(url,''))<>'' THEN url END),
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(url,''))<>'' THEN url END),
      MAX(CASE WHEN trim(ifnull(url,''))<>'' THEN url END)
    ) AS url,

    COALESCE(
      MAX(CASE WHEN site='el_lector' AND trim(ifnull(url_portada,''))<>'' THEN url_portada END),
      MAX(CASE WHEN site='yenny'     AND trim(ifnull(url_portada,''))<>'' THEN url_portada END),
      MAX(CASE WHEN trim(ifnull(url_portada,''))<>'' THEN url_portada END)
    ) AS url_portada

  FROM s
  GROUP BY isbn
)
SELECT
  isbn AS ISBN,
  titulo AS TITULO,
  autor AS AUTOR,
  editorial AS EDITORIAL,
  sinopsis AS SINOPSIS,
  idioma AS IDIOMA,
  paginas AS PAGINAS,
  dimensiones AS DIMENSIONES,
  fecha_publicacion AS FECHA_PUBLICACION,
  url AS URL,
  url_portada AS URL_PORTADA,
  'master' AS SITE
FROM agg;
