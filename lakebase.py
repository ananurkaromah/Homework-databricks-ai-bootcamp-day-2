"""
lakebase.py
-----------
Connection + schema helpers for Databricks Lakebase (Postgres + pgvector).

get_connection() is a context manager yielding a psycopg2 connection
configured with RealDictCursor (rows come back as dicts). Connection info
resolves from a DATABASE_URL env var first (local dev), falling back to a
Databricks secret scope via the databricks-sdk WorkspaceClient.

Schema here matches the 3-source-type document model:
    alert       - GET /alerts/active?area={state}      (statewide)
    forecast    - GET /gridpoints/{office}/{x},{y}/forecast  (per period)
    discussion  - GET /products/types/AFD/locations/{office} (forecaster prose)

Role / auth (per assignment spec): connections are made as the
`weather_app` Postgres role using native Postgres password auth (NOT
Databricks OAuth token auth):

    postgresql://weather_app:<password>@<lakebase-host>:5432/databricks_postgres?sslmode=require
"""

import os
import json
import logging
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

LAKEBASE_SECRET_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
# Must match SCOPE/KEY in setup_secrets.py and app.yaml exactly.
LAKEBASE_SECRET_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")

# Embedding dimensionality — must match the model in ingest_weather_embeddings.py
EMBEDDING_DIM = 384


def _resolve_database_url() -> str:
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
    # Databricks always base64-encodes `value` in the get_secret() response,
    # regardless of how the secret was written (this is API behavior, not a
    # convention of this project). setup_secrets.py stores the URL as plain
    # text via put_secret(string_value=...) — do NOT base64-encode it there
    # too, or this single decode only strips the API's own encoding and
    # leaves the string still base64-encoded here.
    import base64

    return base64.b64decode(secret.value).decode("utf-8")


@contextmanager
def get_connection():
    """
    Context manager yielding a psycopg2 connection configured with
    RealDictCursor as the default cursor factory.
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
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    state           TEXT,
    grid_office     TEXT,
    grid_x          INT,
    grid_y          INT,
    source_type     TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast', 'discussion')),
    event           TEXT,
    headline        TEXT,
    severity        TEXT,
    narrative_text  TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    issued_at       TIMESTAMPTZ,
    effective_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    source_url      TEXT,
    payload         JSONB NOT NULL,
    synced_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

WEATHER_DOCUMENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type ON weather_documents (source_type);",
    "CREATE INDEX IF NOT EXISTS idx_weather_documents_location ON weather_documents (location);",
    "CREATE INDEX IF NOT EXISTS idx_weather_documents_issued_at ON weather_documents (issued_at DESC);",
]

WEATHER_EMBEDDINGS_DDL = f"""
CREATE TABLE IF NOT EXISTS weather_embeddings (
    id              TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES weather_documents(id) ON DELETE CASCADE,
    source_type     TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    chunk_text      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    embedding       vector({EMBEDDING_DIM}) NOT NULL,
    model_name      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index)
);
"""

WEATHER_EMBEDDINGS_HNSW_INDEX = """
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding
    ON weather_embeddings
    USING hnsw (embedding vector_cosine_ops);
"""

WEATHER_EMBEDDINGS_IVFFLAT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_ivfflat
    ON weather_embeddings
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
"""

WEATHER_EMBEDDINGS_OTHER_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_weather_embeddings_document_id ON weather_embeddings (document_id);",
    "CREATE INDEX IF NOT EXISTS idx_weather_embeddings_source_type ON weather_embeddings (source_type);",
]


def ensure_weather_schema():
    """
    Idempotently create weather_documents / weather_embeddings, the
    pgvector extension, and all indexes. Safe to call on every app startup.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute(WEATHER_DOCUMENTS_DDL)
            for stmt in WEATHER_DOCUMENTS_INDEXES:
                cur.execute(stmt)
            cur.execute(WEATHER_EMBEDDINGS_DDL)
            for stmt in WEATHER_EMBEDDINGS_OTHER_INDEXES:
                cur.execute(stmt)
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
    (id, location, latitude, longitude, state, grid_office, grid_x, grid_y,
     source_type, event, headline, severity, narrative_text, content_hash,
     issued_at, effective_at, expires_at, source_url, payload, synced_at)
VALUES
    (%(id)s, %(location)s, %(latitude)s, %(longitude)s, %(state)s, %(grid_office)s,
     %(grid_x)s, %(grid_y)s, %(source_type)s, %(event)s, %(headline)s, %(severity)s,
     %(narrative_text)s, %(content_hash)s, %(issued_at)s, %(effective_at)s,
     %(expires_at)s, %(source_url)s, %(payload)s, now())
ON CONFLICT (id) DO UPDATE SET
    location        = EXCLUDED.location,
    latitude        = EXCLUDED.latitude,
    longitude       = EXCLUDED.longitude,
    state           = EXCLUDED.state,
    grid_office     = EXCLUDED.grid_office,
    grid_x          = EXCLUDED.grid_x,
    grid_y          = EXCLUDED.grid_y,
    source_type     = EXCLUDED.source_type,
    event           = EXCLUDED.event,
    headline        = EXCLUDED.headline,
    severity        = EXCLUDED.severity,
    narrative_text  = EXCLUDED.narrative_text,
    content_hash    = EXCLUDED.content_hash,
    issued_at       = EXCLUDED.issued_at,
    effective_at    = EXCLUDED.effective_at,
    expires_at      = EXCLUDED.expires_at,
    source_url      = EXCLUDED.source_url,
    payload         = EXCLUDED.payload,
    synced_at       = now();
"""


def upsert_weather_documents(documents: list[dict]) -> int:
    """
    Batch-upsert normalized weather document records (as produced by
    weather_client's normalize_alert / normalize_forecast_period /
    normalize_discussion) into weather_documents. Dedup/upsert key is `id`.
    content_hash is included in the UPDATE so a re-issued alert with
    changed wording is detected as needing re-embedding (see
    ingest_weather_embeddings.py's coverage query).
    """
    if not documents:
        return 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            for doc in documents:
                params = dict(doc)
                params["payload"] = json.dumps(params.get("payload") or {})
                cur.execute(UPSERT_WEATHER_DOCUMENT, params)
    return len(documents)