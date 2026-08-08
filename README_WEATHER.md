# Weather Intelligence - Lakebase Vector Search Databricks App

`databricks_app` name: **`weather_intelligence_app`**

An end-to-end pipeline that harvests unstructured (free-text) weather
alerts and forecast discussions from the National Weather Service API,
stores them in a Postgres/Lakebase table, embeds the text with a sentence
transformer, and exposes cosine-similarity semantic search (plus a small
RAG summary variant) over a Flask API.

## 1. Data source

**National Weather Service API — `api.weather.gov`.**

Chosen because:
- No API key required, generous rate limits, stable public JSON schema.
- Genuinely unstructured, human-written free text: alert `description` /
  `instruction` fields, and full-prose Area Forecast Discussions (AFDs) —
  meteorologists writing paragraphs of reasoning, not structured data.
- Two distinct document "genres" (`alert` vs `forecast`) from one source,
  which lets retrieval demonstrate filtering by `source_type`.

One extra hop is required that isn't part of the NWS API itself:
`api.weather.gov` only accepts raw lat/lon, not place names, so
`weather_client.py` geocodes `"City, ST"` strings via the free US Census
Bureau geocoder before calling `GET /points/{lat},{lon}`. If you already
have lat/lon pairs, this hop is skipped entirely — pass `(lat, lon)`
tuples instead of strings to `harvest()`.

## 2. Schema decisions

**`weather_documents`** (raw, normalized documents — mirrors
`ticker_news_documents`):

| column           | type          | notes                                             |
|------------------|---------------|----------------------------------------------------|
| `id`             | TEXT PK       | alert's own NWS `id` URN, or a hash of location+issued_at for forecasts (no natural key exists for AFDs) |
| `location`       | TEXT          | as passed in (`"Chicago, IL"`, or `"lat,lon"`)     |
| `source_type`    | TEXT          | `'alert'` \| `'forecast'`, CHECK-constrained       |
| `headline`       | TEXT          | alert headline/event, or a generated forecast title |
| `narrative_text` | TEXT          | the text to embed — alert `description`+`instruction`, or the AFD `productText` |
| `issued_at`      | TIMESTAMPTZ   | alert `sent`, or AFD `issuanceTime`                |
| `effective_at`   | TIMESTAMPTZ   | alert `effective`, or same as `issued_at` for forecasts |
| `payload`        | JSONB         | full raw API response, for provenance/debugging    |
| `synced_at`      | TIMESTAMPTZ   | set on every upsert (`now()`)                      |

**`weather_embeddings`** (mirrors `ticker_news_embeddings`):

| column        | type            | notes                                    |
|---------------|-----------------|-------------------------------------------|
| `id`          | BIGSERIAL PK    |                                            |
| `document_id` | TEXT            | FK -> `weather_documents.id`, `ON DELETE CASCADE` |
| `chunk_index` | INTEGER         | 0-based index within the parent document  |
| `chunk_text`  | TEXT            | the actual chunk that was embedded        |
| `embedding`   | `vector(384)`   | pgvector column                           |
| `model_name`  | TEXT            | `sentence-transformers/all-MiniLM-L6-v2`  |
| `created_at`  | TIMESTAMPTZ     |                                            |

`UNIQUE (document_id, chunk_index)` + `ON CONFLICT ... DO UPDATE` makes
re-embedding idempotent. An `hnsw (embedding vector_cosine_ops)` index is
created for fast cosine-distance search (falls back to `ivfflat` if the
Lakebase pgvector version predates HNSW support).

**Chunking:** `CHUNK_SIZE=800` characters, `CHUNK_OVERLAP=100` characters
(sliding window, no tokenizer — kept dependency-free since NWS text is
plain English prose). Most alert text is well under 800 characters and
ends up as a single chunk; chunking mainly matters for combined
description+instruction alert text or long AFD products.

**Embedding model:** `sentence-transformers/all-MiniLM-L6-v2` (384-dim) —
the same model as the existing `ticker_news_embeddings` pipeline, so both
tables share the same dimensionality and are queryable with the same
`<=>` cosine-distance convention.

## 3. Auth / connection

Per the assignment spec, all connections are made as a dedicated
`weather_app` Postgres role using **native Postgres password auth (not
Databricks OAuth)**:

```
postgresql://weather_app:<password>@<lakebase-host>:5432/databricks_postgres?sslmode=require
```

No credentials are committed to source control. Run `setup_secrets.py`
**once**, after provisioning the Lakebase instance and creating the
`weather_app` role, to store this URL as a Databricks secret — same
pattern as the `lakebase-support-app` project:

```bash
python setup_secrets.py
```

It stores the URL under scope `database`, key `weather-lakebase-url` —
a **different key** from the support app's `lakebase-url` in that same
shared scope, so the two apps' secrets don't collide. `lakebase.py`
reads this via `LAKEBASE_SECRET_SCOPE` / `LAKEBASE_SECRET_KEY` (both
overridable as env vars, e.g. in `app.yaml`) using the same
`databricks-sdk WorkspaceClient` fetch pattern as the support app.

For local development without touching Databricks secrets at all, set
`DATABASE_URL` directly — `lakebase.py` checks it first and only falls
back to the secret scope if it's unset:

```bash
export DATABASE_URL="postgresql://weather_app:<password>@<host>:5432/databricks_postgres?sslmode=require"
```

## 4. Running the pipeline end-to-end

```bash
pip install -r requirements.txt

# One-time: store the connection URL as a Databricks secret (skip this
# and use `export DATABASE_URL=...` instead for local-only dev)
python setup_secrets.py

# 1. Start the app (also ensures schema exists on boot)
python app.py

# 2. Harvest + normalize + upsert documents
curl -X POST http://localhost:5000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}'

# 3. Chunk + embed + write vectors
python ingest_weather_embeddings.py

# 4. Semantic search
curl -X POST http://localhost:5000/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "risk of flooding near rivers", "top_k": 5}'

# 4b. Same search, plus an LLM-generated summary (RAG)
curl "http://localhost:5000/weather/search?query=risk+of+flooding+near+rivers&top_k=5"
```

Re-running `/weather/sync` and the embedding script is safe — both upsert
on a stable key (`weather_documents.id`, `(document_id, chunk_index)`), so
nothing duplicates.

## 5. Known limitations / what I'd improve with more time

- **AFD dedup key is a hash, not a natural key.** NWS text products don't
  expose a short stable business ID the way alerts do, so re-issuance of
  an AFD with an identical `issuanceTime` (rare, but possible on reissue)
  would overwrite rather than version. Acceptable for a forecast snapshot
  use case, but worth a `product.id` UUID column if version history matters.
- **Geocoding adds an external dependency** (Census Bureau geocoder) and
  a point of failure between the user's location string and the NWS grid
  lookup. Accepting `(lat, lon)` directly avoids this entirely.
- **No scheduled re-sync is wired up yet** — the spec calls for a
  Databricks Job / cron to re-sync alerts every N minutes. The `/weather/sync`
  endpoint is idempotent and cheap enough to call from a simple
  Databricks Jobs schedule hitting the endpoint (or invoking
  `weather_client.harvest()` directly from a python task) every 10–15
  minutes for active alerts.
- **HNSW benchmark not automated.** The `CREATE INDEX ... USING hnsw` vs.
  no-index latency comparison from the "extra" list is a manual `EXPLAIN
  ANALYZE` exercise against the two index variants in `lakebase.py`
  (`WEATHER_EMBEDDINGS_HNSW_INDEX` / drop it and compare) rather than a
  scripted benchmark — worth automating into a small script.
- **RAG summary falls back to extractive-only** when `ANTHROPIC_API_KEY`
  isn't set, so the `GET /weather/search` endpoint works out of the box
  without extra configuration, but the summary quality is much better
  with a real LLM call wired in.