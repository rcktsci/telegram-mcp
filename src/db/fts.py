"""Full-text search objects (RU+EN snowball), shared by alembic and tests.

fts_vector(text) на лету режет текст на нормализованные лексемы: русский
конфиг даёт морфологию («модели/моделью → модел»), english добавляет
стемминг латиницы. GIN-индекс по этому выражению ускоряет
`fts_vector(messages.text) @@ query`.

Тело fts_vector — контракт: изменив его, пересоздай индекс (секунды).
"""

FTS_STATEMENTS = [
    """
    CREATE OR REPLACE FUNCTION fts_vector(t text) RETURNS tsvector
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
        SELECT setweight(to_tsvector('russian', coalesce(t, '')), 'A')
            || setweight(to_tsvector('english', coalesce(t, '')), 'B')
    $$;
    """,
    "CREATE INDEX IF NOT EXISTS messages_fts ON messages USING GIN (fts_vector(text))",
]
