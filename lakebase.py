"""
lakebase.py
-----------
Connection + schema helpers for Databricks Lakebase (Postgres + pgvector).

Follows the existing repo convention:
    - get_connection() is a context manager yielding a psycopg2 connection
      configured with RealDictCursor (rows come back as dicts).
    - Connection info is resolved from a DATABASE_URL env var first
      (useful for local dev / testing), and falls back to a Databricks
      secret scope via the databricks-sdk WorkspaceClient, matching the
      pattern used elsewhere in this project.

This module adds the `weather_documents` and `weather_embeddings` tables
for the Weather Intelligence pipeline. It does NOT touch any existing
`ticker_news_*` tables/functions — only new, additive schema + helpers
live here.

Role / auth note (per assignment spec):
    Connections are made as the `weather_app` Postgres role using native
    Postgres password auth (NOT Databricks OAuth token auth). The
    connection string therefore looks like:

        postgresql://weather_app:<password>@<lakebase-host>:5432/databricks_postgres?sslmode=require

    Store this in the `DATABASE_URL` env var locally, or in a Databricks
    secret (scope/key configurable via LAKEBASE_SECRET_SCOPE /
    LAKEBASE_SECRET_KEY) when deployed as a Databricks App.
"""

import os
import base64
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

LAKEBASE_SECRET_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
# Distinct from the lakebase-support-app's "lakebase-url" key so the two
# apps' secrets coexist in the shared "database" scope without collision
# (see setup_secrets.py for details).
LAKEBASE_SECRET_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "weather-lakebase-url")

# Embedding dimensionality — must match the model used in
# notebooks/ingest_weather_embeddings.py (sentence-transformers/all-MiniLM-L6-v2 = 384)
EMBEDDING_DIM = 384


def _resolve_database_url() -> str:
    """
    Resolve the Postgres connection URL.

    1. DATABASE_URL env var (local dev / CI / testing) — takes priority.
    2. Databricks secret scope (deployed Databricks App), via the
       databricks-sdk WorkspaceClient, matching the existing repo's
       secret-fetch pattern for Lakebase credentials.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as exc:
        raise RuntimeError(
            "DATABASE_URL not set and databricks-sdk is not installed, "
            "so the Lakebase connection string could not be resolved."
        ) from exc

    client = WorkspaceClient()
    secret = client.secrets.get_secret(
        scope=LAKEBASE_SECRET_SCOPE, key=LAKEBASE_SECRET_KEY
    )
    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """
    Context manager yielding a psycopg2 connection configured with
    RealDictCursor as the default cursor factory, so query results come
    back as dicts (e.g. row["narrative_text"]) rather than tuples.

    Usage:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                print(cur.fetchone())
    """
    database_url = _resolve_database_url()
    conn = psycopg2.connect(
        database_url, cursor_factory=psycopg2.extras.RealDictCursor
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

WEATHER_DOCUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS weather_documents (
    id              TEXT PRIMARY KEY,
    location        TEXT NOT NULL,
    source_type     TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),
    headline        TEXT,
    narrative_text  TEXT NOT NULL,
    issued_at       TIMESTAMPTZ,
    effective_at    TIMESTAMPTZ,
    payload         JSONB,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

WEATHER_DOCUMENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_weather_documents_location ON weather_documents (location);",
    "CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type ON weather_documents (source_type);",
]

WEATHER_EMBEDDINGS_DDL = f"""
CREATE TABLE IF NOT EXISTS weather_embeddings (
    id              BIGSERIAL PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    chunk_text      TEXT NOT NULL,
    embedding       vector({EMBEDDING_DIM}) NOT NULL,
    model_name      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
"""

# HNSW index for cosine-distance search (pgvector's `<=>` operator).
# Falls back to ivfflat if the pgvector version in this Lakebase instance
# doesn't support HNSW yet (added in pgvector 0.5.0).
WEATHER_EMBEDDINGS_HNSW_INDEX = """
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_hnsw
    ON weather_embeddings
    USING hnsw (embedding vector_cosine_ops);
"""

WEATHER_EMBEDDINGS_IVFFLAT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_ivfflat
    ON weather_embeddings
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
"""

WEATHER_EMBEDDINGS_DOCID_INDEX = """
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_document_id
    ON weather_embeddings (document_id);
"""


def ensure_weather_schema():
    """
    Idempotently create the weather_documents / weather_embeddings tables,
    the pgvector extension, and the vector index. Safe to call on every
    app startup (mirrors an ensure_schema()-style pattern).
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(WEATHER_DOCUMENTS_DDL)
            for stmt in WEATHER_DOCUMENTS_INDEXES:
                cur.execute(stmt)
            cur.execute(WEATHER_EMBEDDINGS_DDL)
            cur.execute(WEATHER_EMBEDDINGS_DOCID_INDEX)
            try:
                cur.execute(WEATHER_EMBEDDINGS_HNSW_INDEX)
            except psycopg2.Error:
                logger.warning(
                    "HNSW index creation failed (pgvector may be < 0.5.0); "
                    "falling back to ivfflat."
                )
                conn.rollback()
                with conn.cursor() as cur2:
                    cur2.execute(WEATHER_EMBEDDINGS_IVFFLAT_INDEX)
    logger.info("weather_documents / weather_embeddings schema ensured.")


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

UPSERT_WEATHER_DOCUMENT = """
INSERT INTO weather_documents
    (id, location, source_type, headline, narrative_text, issued_at, effective_at, payload, synced_at)
VALUES
    (%(id)s, %(location)s, %(source_type)s, %(headline)s, %(narrative_text)s,
     %(issued_at)s, %(effective_at)s, %(payload)s, now())
ON CONFLICT (id) DO UPDATE SET
    location        = EXCLUDED.location,
    source_type     = EXCLUDED.source_type,
    headline        = EXCLUDED.headline,
    narrative_text  = EXCLUDED.narrative_text,
    issued_at       = EXCLUDED.issued_at,
    effective_at    = EXCLUDED.effective_at,
    payload         = EXCLUDED.payload,
    synced_at       = now();
"""


def upsert_weather_documents(documents: list[dict]) -> int:
    """
    Batch-upsert normalized weather document records (as produced by
    weather_client.normalize_alert / normalize_forecast) into
    weather_documents. Dedup/upsert key is `id`.

    Returns the number of documents written.
    """
    if not documents:
        return 0

    import json

    with get_connection() as conn:
        with conn.cursor() as cur:
            for doc in documents:
                params = dict(doc)
                params["payload"] = json.dumps(params.get("payload") or {})
                cur.execute(UPSERT_WEATHER_DOCUMENT, params)
    return len(documents)