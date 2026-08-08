-- 02_setup_weather_embeddings_table.sql
--
-- Standalone copy of the weather_embeddings DDL. Requires the pgvector
-- extension (already enabled on this Lakebase instance per the project
-- brief). Same schema lakebase.py's ensure_weather_schema() creates
-- automatically — this file exists for manual inspection/setup.
--
-- Run AFTER 01_setup_weather_documents_table.sql (this table has an FK
-- into weather_documents).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS weather_embeddings (
    id              BIGSERIAL PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    chunk_text      TEXT NOT NULL,
    embedding       vector(384) NOT NULL,
    model_name      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

COMMENT ON TABLE weather_embeddings IS
    'One row per chunk. embedding is 384-dim to match '
    'sentence-transformers/all-MiniLM-L6-v2, the same model used by the '
    'existing ticker_news embedding pipeline, so both stay dimensionally '
    'compatible under the <=> cosine-distance convention.';

CREATE INDEX IF NOT EXISTS idx_weather_embeddings_document_id
    ON weather_embeddings (document_id);

-- HNSW for fast approximate cosine-distance search via the `<=>` operator.
-- Without this index, every /weather/search call is a sequential scan.
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_hnsw
    ON weather_embeddings
    USING hnsw (embedding vector_cosine_ops);

-- Fallback if this Lakebase instance's pgvector predates HNSW support
-- (added in pgvector 0.5.0). Only run this if the HNSW CREATE INDEX above
-- fails — don't create both on the same column.
-- CREATE INDEX IF NOT EXISTS idx_weather_embeddings_ivfflat
--     ON weather_embeddings
--     USING ivfflat (embedding vector_cosine_ops)
--     WITH (lists = 100);