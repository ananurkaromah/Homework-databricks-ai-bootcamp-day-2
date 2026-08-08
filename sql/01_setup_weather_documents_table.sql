-- 01_setup_weather_documents_table.sql
--
-- Standalone copy of the weather_documents DDL. This is the same schema
-- lakebase.py's ensure_weather_schema() creates automatically on every
-- app startup — this file exists so the schema can be inspected, run by
-- hand, or reviewed independently of the Python code.
--
-- Run in a Databricks SQL editor (or psql) connected to your Lakebase
-- instance, as a role with CREATE privilege on the target schema.

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

COMMENT ON TABLE weather_documents IS
    'Raw, normalized NWS documents (alerts + forecast discussions). '
    'One row per document; id is a stable dedup/upsert key derived from '
    'the alert''s own NWS id, or a hash of location+issued_at for forecasts.';

CREATE INDEX IF NOT EXISTS idx_weather_documents_location
    ON weather_documents (location);

CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type
    ON weather_documents (source_type);