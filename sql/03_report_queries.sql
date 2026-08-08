-- Verification / report queries for the weather pipeline
--
-- Run these in a Databricks SQL editor connected to your Lakebase instance
-- AFTER: POST /weather/sync  ->  ingest_weather_embeddings.py
--
-- Each block is independent and answers one question a reader of the report
-- would ask: does the schema match what was designed, did data actually land,
-- did chunking do anything, are the vectors real, and does similarity search
-- work? Query 7 proves retrieval end to end in pure SQL, with no Python.


-- =========================================================================
-- 1. Schema of both tables
--    Shows the designed columns actually exist, with the intended types.
-- =========================================================================
SELECT table_name,
       ordinal_position AS pos,
       column_name,
       CASE
           WHEN data_type = 'USER-DEFINED' THEN udt_name   -- 'vector' shows up here
           ELSE data_type
       END AS type,
       is_nullable
FROM information_schema.columns
WHERE table_name IN ('weather_documents', 'weather_embeddings')
ORDER BY table_name, ordinal_position;


-- =========================================================================
-- 2. Indexes, including the pgvector HNSW one
--    `indexdef` spells out "USING hnsw (embedding vector_cosine_ops)".
-- =========================================================================
SELECT tablename,
       indexname,
       indexdef
FROM pg_indexes
WHERE tablename IN ('weather_documents', 'weather_embeddings')
ORDER BY tablename, indexname;


-- =========================================================================
-- 3. How much data is stored
-- =========================================================================
SELECT 'weather_documents'  AS table_name, count(*) AS row_count FROM weather_documents
UNION ALL
SELECT 'weather_embeddings' AS table_name, count(*) AS row_count FROM weather_embeddings;


-- =========================================================================
-- 4. Documents by source type and location
--    Demonstrates the multi-source harvest: alerts + forecasts + discussions.
-- =========================================================================
SELECT source_type,
       location,
       state,
       count(*)                                   AS documents,
       min(issued_at)                             AS oldest,
       max(issued_at)                             AS newest,
       round(avg(length(narrative_text)))         AS avg_text_chars,
       max(length(narrative_text))                AS max_text_chars
FROM weather_documents
GROUP BY source_type, location, state
ORDER BY source_type, location;


-- =========================================================================
-- 5. Chunking: what the 800/100 sliding window actually did
--    Expect ~1 chunk for alerts and forecast periods, and several for the
--    Area Forecast Discussions - they are the only source long enough to split.
-- =========================================================================
SELECT d.source_type,
       count(DISTINCT d.id)                                          AS documents,
       count(e.id)                                                   AS chunks,
       round(count(e.id)::numeric
             / NULLIF(count(DISTINCT d.id), 0), 2)                   AS chunks_per_doc,
       max(length(d.narrative_text))                                 AS longest_doc_chars,
       round(avg(length(e.chunk_text)))                              AS avg_chunk_chars,
       max(length(e.chunk_text))                                     AS max_chunk_chars
FROM weather_documents d
LEFT JOIN weather_embeddings e ON e.document_id = d.id
GROUP BY d.source_type
ORDER BY d.source_type;


-- =========================================================================
-- 6. Embedding sanity: dimensionality and model
--    vector_dims() must return 384 for every row, matching vector(384) and
--    sentence-transformers/all-MiniLM-L6-v2.
-- =========================================================================
SELECT model_name,
       count(*)                     AS embeddings,
       min(vector_dims(embedding))  AS min_dims,
       max(vector_dims(embedding))  AS max_dims,
       min(created_at)              AS first_embedded,
       max(created_at)              AS last_embedded
FROM weather_embeddings
GROUP BY model_name;


-- =========================================================================
-- 7. Semantic search, in pure SQL
--    Takes one stored chunk as the query vector and ranks every other chunk by
--    cosine similarity with pgvector's <=> operator - the same ordering
--    POST /weather/search uses, minus the Python that embeds the user's text.
--    The probe row itself comes back first at similarity 1.0000, which is a
--    useful check that the distance maths is right.
--
--    Swap the WHERE clause to probe with a different starting chunk.
-- =========================================================================
WITH probe AS (
    SELECT e.id, e.embedding, e.chunk_text
    FROM weather_embeddings e
    JOIN weather_documents d ON d.id = e.document_id
    WHERE d.source_type = 'alert'          -- try 'forecast' or 'discussion'
    ORDER BY d.issued_at DESC NULLS LAST
    LIMIT 1
)
SELECT round((1 - (e.embedding <=> p.embedding))::numeric, 4) AS similarity,
       d.source_type,
       d.location,
       left(coalesce(d.headline, d.event, ''), 60)            AS headline,
       e.chunk_index,
       left(regexp_replace(e.chunk_text, '\s+', ' ', 'g'), 90) AS chunk_preview
FROM weather_embeddings e
JOIN weather_documents d ON d.id = e.document_id
CROSS JOIN probe p
ORDER BY e.embedding <=> p.embedding
LIMIT 10;


-- =========================================================================
-- 8. Coverage: any documents still lacking a current embedding?
--    This is the same content_hash join the ingest job uses to decide what to
--    embed, so an empty result means the pipeline is fully caught up. Rows here
--    are either newly synced or documents whose text was re-issued by the NWS.
-- =========================================================================
SELECT d.source_type,
       d.location,
       left(coalesce(d.headline, d.event, ''), 60) AS headline,
       d.issued_at
FROM weather_documents d
LEFT JOIN weather_embeddings e
       ON e.document_id = d.id
      AND e.content_hash = d.content_hash
WHERE e.id IS NULL
ORDER BY d.issued_at DESC NULLS LAST
LIMIT 20;


-- =========================================================================
-- 9. Idempotency evidence
--    Every id is derived from a stable upstream identifier, so re-running
--    /weather/sync upserts rather than duplicating. Both counts must match,
--    and the id prefixes show the three id schemes in use.
-- =========================================================================
SELECT split_part(id, ':', 1)  AS id_scheme,
       count(*)                AS documents,
       count(DISTINCT id)      AS distinct_ids,
       max(synced_at)          AS last_synced
FROM weather_documents
GROUP BY split_part(id, ':', 1)
ORDER BY id_scheme;