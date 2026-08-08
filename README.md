# Weather Intelligence - Lakebase Vector Search Databricks App

An end-to-end unstructured-data pipeline that harvests free-text weather
alerts and forecast discussions from the National Weather Service,
embeds them for semantic search, and serves retrieval (with a basic RAG
summary) through a Flask API deployed as a Databricks App.

**Databricks App name:** `weather_intelligence_app`

## What this is

| Stage | What happens | File |
|---|---|---|
| Harvest | Resolve locations → NWS grid points → fetch active alerts + forecast discussions → normalize to a common document schema | `weather_client.py` |
| Store | Upsert normalized documents into Lakebase (Postgres) | `lakebase.py`, `app.py` (`POST /weather/sync`) |
| Vectorize | Chunk long text, embed with `sentence-transformers/all-MiniLM-L6-v2`, batch-write vectors | `ingest_weather_embeddings.py` |
| Retrieve | Cosine-similarity search over embeddings via pgvector's `<=>` operator, optional LLM summary | `app.py` (`POST`/`GET /weather/search`) |

For the detailed schema decisions, chunking parameters, and design
rationale, see [`README_WEATHER.md`](./README_WEATHER.md). This file is
the project-level overview: what's in the repo, how to set it up, and
how to deploy it.

## Architecture

```
NWS API (api.weather.gov)
        │  alerts + forecast discussions
        ▼
weather_client.py  ──normalize──▶  weather_documents  (Lakebase / Postgres)
                                          │
                                          │  chunk + embed
                                          ▼
                              weather_embeddings  (pgvector, vector(384))
                                          ▲
                                          │  cosine similarity <=>
                                    app.py (Flask)
                              /weather/sync   /weather/search
```

Runs as a Databricks App backed by a Lakebase Postgres instance with the
`pgvector` extension enabled. Auth to Lakebase is via a dedicated
`weather_app` Postgres role using **native password auth** (not OAuth),
with the connection string stored as a Databricks secret and resolved at
runtime by `lakebase.py`.

## Project structure

```
.
├── app.py                              # Flask app: /weather/sync, /weather/search
├── app.yaml                            # Databricks App run config (command, env)
├── lakebase.py                         # Connection helper + schema (weather_documents/embeddings)
├── weather_client.py                   # NWS API client + document normalization
├── setup_secrets.py                    # One-time: store Lakebase connection URL as a Databricks secret
├── ingest_weather_embeddings.py    # Batch job: chunk, embed, write vectors
├── requirements.txt
├── .env.example                        # Template for local dev env vars
├── .gitignore
├── README.md                           # This file
└── README_WEATHER.md                   # Schema/design deep-dive (assignment write-up)
```

## Prerequisites

- A Databricks workspace with a Lakebase (Postgres) instance provisioned
  and the `pgvector` extension enabled.
- A dedicated `weather_app` Postgres role with native password auth and
  privileges to create tables in the target database:

  ```sql
  CREATE ROLE weather_app WITH LOGIN PASSWORD '<password>';
  GRANT CONNECT ON DATABASE databricks_postgres TO weather_app;
  GRANT USAGE, CREATE ON SCHEMA public TO weather_app;
  ```

- Databricks CLI ≥ 0.229.0, authenticated (`databricks auth login`).
- Python 3.10+.

## Local setup

```bash
git clone <this-repo>
cd weather-intelligence
pip install -r requirements.txt
cp .env.example .env   # fill in DATABASE_URL for local dev
```

Tables are created automatically the first time the app runs
(`ensure_weather_schema()` in `lakebase.py` runs at startup and is
idempotent), so no manual schema step is required — just make sure the
`weather_app` role above already exists first.

```bash
python app.py
```

## One-time: store the Lakebase secret for the deployed app

Run once, locally (no workspace upload needed — this talks to the
Databricks REST API directly):

```bash
python setup_secrets.py
```

Prompts for the Lakebase host and the `weather_app` password, then
stores the full connection URL as a Databricks secret
(`database/lakebase-url` by default) that the deployed app reads at
runtime.

## Deploying to Databricks Apps

This repo is deployed from Git rather than a synced workspace folder.

```bash
databricks apps create weather_intelligence_app \
  --json '{"git_repository": {"url": "<your-github-repo-url>", "provider": "gitHub"}}'

# private repos only:
databricks git-credentials create --json '{
  "git_provider": "gitHub",
  "git_email": "<you>@example.com",
  "personal_access_token": "<PAT>",
  "principal_id": <service-principal-id>,
  "name": "weather-app-github"
}'

databricks apps deploy weather_intelligence_app \
  --json '{"git_source": {"branch": "main"}}'
```

Check the app's **Logs** tab after the first deploy — that's where
`ensure_weather_schema()` runs and where a missing/under-privileged
`weather_app` role would surface.

## Running the pipeline end-to-end

```bash
# 1. Harvest + normalize + upsert documents
curl -X POST https://<your-app-url>/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}'

# 2. Chunk + embed + write vectors 
python ingest_weather_embeddings.py

# 3. Semantic search
curl -X POST https://<your-app-url>/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "risk of flooding near rivers", "top_k": 5}'

# 3b. Same search + LLM-generated summary (RAG)
curl "https://<your-app-url>/weather/search?query=risk+of+flooding+near+rivers&top_k=5"
```

## API reference

| Method | Path | Body / Query | Returns |
|---|---|---|---|
| `POST` | `/weather/sync` | `{"locations": [...], "limit": 50}` | `{"synced": <count>, "locations": [...]}` |
| `POST` | `/weather/search` | `{"query": "...", "top_k": 5, "source_type": "alert"\|"forecast"}` (optional filter) | `{"results": [...]}` |
| `GET` | `/weather/search` | `?query=...&top_k=5&source_type=...` | `{"results": [...], "summary": "..."}` |

`top_k` is clamped to 1–20. An empty `weather_embeddings` table or a
missing/empty `query` returns a clear message rather than an error.

## Known limitations

See the "Known limitations / what I'd improve" section in
[`README_WEATHER.md`](./README_WEATHER.md) — covers the AFD dedup key,
the geocoding dependency, the scheduled re-sync job that isn't wired up
yet, and the HNSW benchmark.