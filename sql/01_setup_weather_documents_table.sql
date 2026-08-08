-- Setup script for the weather_documents table
-- Run this manually in your Lakebase Postgres database, or just call
-- POST /weather/sync - the Flask app runs the same DDL via
-- lakebase.ensure_weather_schema() on every app startup.
--
-- This is the RAW document store: one row per normalized National Weather
-- Service item (an active alert, a single forecast period, or a forecaster's
-- Area Forecast Discussion). ingest_weather_embeddings.py reads from
-- here to compute the vectors in weather_embeddings.

CREATE TABLE IF NOT EXISTS weather_documents (
    -- Stable, upstream-derived dedup key, e.g.
    --   alert:urn:oid:2.49.0.1.840.0.f0923...001.1
    --   forecast:LOT:76,73:2026-08-06T18:00:00-05:00
    --   discussion:f84f7939-ed27-46b6-ad37-fab12a2664cd
    id TEXT PRIMARY KEY,
    location TEXT NOT NULL,              -- as the caller supplied it, or "{state} (statewide)" for alerts
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    state TEXT,                          -- two-letter code; drives /alerts/active?area=
    grid_office TEXT,                    -- NWS forecast office, e.g. "LOT"
    grid_x INT,
    grid_y INT,
    source_type TEXT NOT NULL
        CHECK (source_type IN ('alert', 'forecast', 'discussion')),
    event TEXT,                          -- e.g. "Flash Flood Warning", or forecast period name
    headline TEXT,
    severity TEXT,                       -- alerts only: Minor/Moderate/Severe/Extreme
    narrative_text TEXT NOT NULL,        -- the free text that gets embedded
    content_hash TEXT NOT NULL,          -- sha256(narrative_text); see note below
    issued_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    source_url TEXT,
    payload JSONB NOT NULL,              -- raw API response, for provenance
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- content_hash exists so the embedding job can distinguish "never embedded"
-- from "embedded, but NWS has since re-issued this alert with new wording".
-- See the LEFT JOIN in ingest_weather_embeddings.py's FETCH_UNEMBEDDED_SQL.

CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type
    ON weather_documents (source_type);

CREATE INDEX IF NOT EXISTS idx_weather_documents_location
    ON weather_documents (location);

CREATE INDEX IF NOT EXISTS idx_weather_documents_issued_at
    ON weather_documents (issued_at DESC);

-- Verify the table was created
SELECT
    table_name,
    column_name,
    data_type
FROM information_schema.columns
WHERE table_name = 'weather_documents'
ORDER BY ordinal_position;