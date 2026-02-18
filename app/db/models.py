from __future__ import annotations

from sqlalchemy import Column, DateTime, Index, MetaData, PrimaryKeyConstraint, Table, Text, text

metadata = MetaData()

book_std = Table(
    "book_std",
    metadata,
    Column("site", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("isbn", Text, nullable=True),
    Column("titulo", Text, nullable=True),
    Column("autor", Text, nullable=True),
    Column("editorial", Text, nullable=True),
    Column("encuadernacion", Text, nullable=True),
    Column("categoria", Text, nullable=True),
    Column("sinopsis", Text, nullable=True),
    Column("idioma", Text, nullable=True),
    Column("paginas", Text, nullable=True),
    Column("dimensiones", Text, nullable=True),
    Column("fecha_publicacion", Text, nullable=True),
    Column("url_portada", Text, nullable=True),
    Column("raw_json", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")),
    Column("updated_at", DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")),
    PrimaryKeyConstraint("site", "url", name="pk_book_std"),
    Index("idx_book_std_isbn", "isbn"),
    Index("idx_book_std_site", "site"),
)
