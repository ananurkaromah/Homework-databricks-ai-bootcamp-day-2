-- 03_report_queries.sql
--
-- Read-only queries for verifying the pipeline from the database side,
-- independent of the Flask app or the embedding script. Run these in a
-- Databricks SQL editor connected to your Lakebase instance, after at
-- least one POST /weather/sync and one run of ingest_weather_embeddings.py.
--
-- Each query is self-contained; run them individually.


-- 1. Schema of both tables -- confirms the designed columns exist with the
--    intended types (`vector` surfaces through udt_name, not data_type).
SELECT table_name,
       ordinal_position AS pos,
       column_name,
       CASE
           WHEN data_type = 'USER-DEFINED' THEN udt_name
           ELSE data_type
       END AS type,
       is_nullable
FROM information_schema.columns
WHERE table_name IN ('weather_documents', 'weather_embeddings')
ORDER BY table_name, ordinal_position;


-- 2. Indexes -- indexdef spells out `USING hnsw (embedding vector_cosine_ops)`.
SELECT tablename,
       indexname,
       indexdef
FROM pg_indexes
WHERE tablename IN ('weather_documents', 'weather_embeddings')
ORDER BY tablename, indexname;


-- 3. How much data is stored.
SELECT 'weather_documents'  AS table_name, count(*) AS row_count FROM weather_documents
UNION ALL
SELECT 'weather_embeddings' AS table_name, count(*) AS row_count FROM weather_embeddings;


-- 4. Documents by source type and location -- evidence of the harvest.
SELECT source_type,
       location,
       count(*)                            AS documents,
       min(issued_at)                      AS oldest,
       max(issued_at)                      AS newest,
       round(avg(length(narrative_text)))  AS avg_text_chars,
       max(length(narrative_text))         AS max_text_chars
FROM weather_documents
GROUP BY source_type, location
ORDER BY source_type, location;


-- 5. Chunking -- what the 800/100 sliding window actually did. Expect
--    ~1 chunk for most alerts and short forecasts, more for any document
--    whose narrative_text exceeded 800 characters.
SELECT d.source_type,
       count(DISTINCT d.id)                        AS documents,
       count(e.id)                                 AS chunks,
       round(count(e.id)::numeric
             / NULLIF(count(DISTINCT d.id), 0), 2) AS chunks_per_doc,
       max(length(d.narrative_text))               AS longest_doc_chars,
       round(avg(length(e.chunk_text)))            AS avg_chunk_chars,
       max(length(e.chunk_text))                   AS max_chunk_chars
FROM weather_documents d
LEFT JOIN weather_embeddings e ON e.document_id = d.id
GROUP BY d.source_type
ORDER BY d.source_type;


-- 6. Embedding sanity -- vector_dims() must be 384 for every row, matching
--    vector(384) and sentence-transformers/all-MiniLM-L6-v2.
SELECT model_name,
       count(*)                     AS embeddings,
       min(vector_dims(embedding))  AS min_dims,
       max(vector_dims(embedding))  AS max_dims,
       min(created_at)              AS first_embedded,
       max(created_at)              AS last_embedded
FROM weather_embeddings
GROUP BY model_name;


-- 7. Semantic search, in pure SQL. Takes one stored chunk as the query
--    vector and ranks every chunk by cosine similarity with `<=>` -- the
--    same ordering POST /weather/search uses, minus the Python that
--    embeds the user's own free-text query. The probe row returns first
--    at similarity 1.0000, which doubles as a check that the distance
--    arithmetic is right. Change the WHERE clause to probe a different
--    source_type.
WITH probe AS (
    SELECT e.id, e.embedding, e.chunk_text
    FROM weather_embeddings e
    JOIN weather_documents d ON d.id = e.document_id
    WHERE d.source_type = 'alert'          -- try 'forecast'
    ORDER BY d.issued_at DESC NULLS LAST
    LIMIT 1
)
SELECT round((1 - (e.embedding <=> p.embedding))::numeric, 4)  AS similarity,
       d.source_type,
       d.location,
       left(coalesce(d.headline, ''), 60)                      AS headline,
       e.chunk_index,
       left(regexp_replace(e.chunk_text, '\s+', ' ', 'g'), 90) AS chunk_preview
FROM weather_embeddings e
JOIN weather_documents d ON d.id = e.document_id
CROSS JOIN probe p
ORDER BY e.embedding <=> p.embedding
LIMIT 10;


-- 8. Coverage -- documents with no embeddings yet (what
--    ingest_weather_embeddings.py's NOT EXISTS query would pick up on its
--    next run). An empty result means the pipeline is fully caught up.
SELECT d.id,
       d.source_type,
       d.location,
       left(coalesce(d.headline, ''), 60) AS headline,
       d.issued_at
FROM weather_documents d
WHERE NOT EXISTS (
    SELECT 1 FROM weather_embeddings e WHERE e.document_id = d.id
)
ORDER BY d.issued_at DESC NULLS LAST
LIMIT 20;


-- 9. Upsert/idempotency evidence -- weather_documents.id is the PK, so
--    duplicate ids are structurally impossible; this instead confirms
--    re-syncing refreshes existing rows rather than silently doing
--    nothing. Documents whose synced_at is much newer than their
--    issued_at were touched by a later sync run without their content
--    changing (or NWS reissued them under the same id).
SELECT id,
       source_type,
       location,
       issued_at,
       synced_at,
       synced_at - issued_at AS sync_lag
FROM weather_documents
WHERE issued_at IS NOT NULL
ORDER BY synced_at DESC
LIMIT 20;