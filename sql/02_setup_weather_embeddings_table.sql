-- Setup script for the weather_embeddings table
-- Run after 01_setup_weather_documents_table.sql (there is a foreign key).
-- The Flask app runs equivalent DDL automatically via
-- lakebase.ensure_weather_schema().
--
-- vector(384) matches sentence-transformers/all-MiniLM-L6-v2. If you swap
-- models, change the dimension here to match:
--   - sentence-transformers/all-MiniLM-L6-v2: 384
--   - sentence-transformers/all-mpnet-base-v2: 768
--   - BAAI/bge-small-en-v1.5: 384
--   - BAAI/bge-base-en-v1.5: 768
--   - BAAI/bge-large-en-v1.5: 1024

-- Enable pgvector (already enabled on this Lakebase instance)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS weather_embeddings (
    id TEXT PRIMARY KEY,                 -- "{document_id}#{chunk_index}"
    document_id TEXT NOT NULL
        REFERENCES weather_documents (id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,           -- denormalized so filtered search can
                                          -- narrow before joining the documents
    chunk_index INT NOT NULL,
    chunk_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,          -- parent document's hash at embed time
    embedding vector(384) NOT NULL,
    model_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);

-- HNSW index for fast approximate cosine similarity search. This is what makes
-- `ORDER BY embedding <=> $query` an index scan instead of a full table scan.
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding
    ON weather_embeddings
    USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_weather_embeddings_document_id
    ON weather_embeddings (document_id);

CREATE INDEX IF NOT EXISTS idx_weather_embeddings_source_type
    ON weather_embeddings (source_type);

-- Verify the table was created
SELECT
    table_name,
    column_name,
    data_type,
    udt_name
FROM information_schema.columns
WHERE table_name = 'weather_embeddings'
ORDER BY ordinal_position;