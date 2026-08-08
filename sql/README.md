# Weather Intelligence - Lakebase Vector Search Databricks App

An end-to-end unstructured-data pipeline that harvests free-text weather
data from the National Weather Service, embeds it into **Lakebase**
(Databricks-managed Postgres) with **pgvector**, and serves semantic
search — with an optional RAG summary — through a Flask API and a
browser UI deployed as a Databricks App.

**Databricks App name:** `weather-intelligence-app` (hyphenated — Databricks
App resource names are kebab-case; this is what's actually deployed,
distinct from the underscored `weather_intelligence_app` in early planning
notes).

## What this is

Three distinct free-text products, all from `api.weather.gov`:

| `source_type` | Endpoint | What the text looks like |
|---|---|---|
| `alert` | `GET /alerts/active?area={state}` (statewide, not per-point) | Warning/advisory description + instruction prose |
| `forecast` | `GET /gridpoints/{office}/{x},{y}/forecast` | One `detailedForecast` per period (e.g. "Tonight", "Wednesday") |
| `discussion` | `GET /products/types/AFD/locations/{office}` → `GET /products/{id}` | The forecaster's own free-form Area Forecast Discussion — the one source long enough that chunking actually matters |

| Stage | What happens | File |
|---|---|---|
| Harvest | Resolve locations -> NWS grid points -> fetch alerts (statewide, deduped) + forecast periods + discussions -> normalize to a common schema | `weather_client.py` |
| Store | Upsert normalized documents into Lakebase via `psycopg2` | `lakebase.py`, `app.py` (`POST /weather/sync`) |
| Vectorize | Chunk long text (800/100 sliding window), embed with `sentence-transformers/all-MiniLM-L6-v2`, batch-write vectors | `ingest_weather_embeddings.py` |
| Retrieve | Cosine-similarity search over embeddings via pgvector's `<=>` operator, optional RAG summary | `app.py` (`POST`/`GET /weather/search`), `templates/weather.html` |

For schema decisions, chunking parameters, and design rationale, see
[`README_WEATHER.md`](./README_WEATHER.md).

## Architecture

```
api.weather.gov
     |  alerts (statewide) + forecast periods + discussions
     v
weather_client.py --normalize--> weather_documents  (Lakebase / Postgres)
                                        |
                                        |  chunk (800/100) + embed (MiniLM-L6-v2)
                                        v
                                 weather_embeddings  (pgvector, vector(384) + HNSW)
                                        |
                                        |  cosine similarity <=>
                                        v
                                  app.py (Flask)
                        /weather/sync    /weather/search    /weather (UI)
```

Runs as a Databricks App backed by a Lakebase Postgres instance with the
`pgvector` extension enabled. Auth to Lakebase is via a dedicated
`weather_app` Postgres role using **native password auth** (not OAuth),
with the connection string stored as a Databricks secret and resolved at
runtime by `lakebase.py`.

## Project structure

```
.
├── app.py                              # Flask: /healthz, /weather (UI), /weather/sync, /weather/search
├── app.yaml                            # Databricks App run config (command, env)
├── databricks.yml                      # Databricks Asset Bundle config (for the scheduled job)
├── lakebase.py                         # Connection helper + schema (weather_documents/embeddings)
├── weather_client.py                   # NWS API client + document normalization
├── ingest_weather_embeddings.py        # Batch job: chunk, embed, write vectors
├── setup_secrets.py                    # One-time: store Lakebase connection URL as a Databricks secret
├── resources/
│   └── ingest_weather_embeddings_job.yml   # Scheduled Databricks Job (every 30 min, created PAUSED)
├── sql/
│   ├── 01_setup_weather_documents_table.sql
│   ├── 02_setup_weather_embeddings_table.sql
│   ├── 03_report_queries.sql
│   └── README.md
├── templates/
│   └── weather.html                    # Browser UI: sync panel + semantic search
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md                           # This file
└── README_WEATHER.md                   # Schema/design deep-dive
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

- Databricks CLI >= 0.229.0, authenticated (`databricks auth login`).
- Python 3.10+.

## Local setup

```bash
git clone <this-repo>
cd weather-intelligence
pip install -r requirements.txt
cp .env.example .env   # then fill in DATABASE_URL for local dev
python app.py          # runs locally at http://localhost:5000
```

Tables are created automatically the first time the app runs
(`ensure_weather_schema()` in `lakebase.py` runs at startup and is
idempotent) — no manual schema step is required, just make sure the
`weather_app` role above already exists first. `sql/01_...` / `sql/02_...`
are there if you'd rather inspect or run the DDL by hand first; see
[`sql/README.md`](./sql/README.md).

## One-time: store the Lakebase secret

Run once, locally — no workspace upload needed, this talks to the
Databricks REST API directly:

```bash
pip install databricks-sdk   # or: pip install -r requirements.txt
databricks auth login --host https://<your-workspace>.cloud.databricks.com/
python setup_secrets.py
```

Prompts for the Lakebase host and the `weather_app` password, then stores
the full connection URL as a Databricks secret (`database/lakebase-url`
by default) that the deployed app reads at runtime. Alternative: paste
the script into a Databricks notebook cell instead — `WorkspaceClient()`
auto-authenticates there, no local CLI login needed.

## Deploying (Databricks CLI, from a workspace Git folder)

```bash
# Clone the repo into a workspace Git folder
databricks repos create https://github.com/<you>/<repo>.git gitHub \
  --path /Workspace/Users/<your-email>/weather-intelligence

# Create and deploy the app from that workspace path
databricks apps create weather-intelligence-app
databricks apps deploy weather-intelligence-app \
  --source-code-path /Workspace/Users/<your-email>/weather-intelligence
```

To update later: `git pull` inside the workspace Git folder, then re-run
the `databricks apps deploy` command above. Check the app's **Logs** tab
after the first deploy — that's where `ensure_weather_schema()` runs, and
where a missing/under-privileged `weather_app` role would surface.

**Testing the deployed app:** Databricks Apps sit behind workspace OAuth2
by default, so a bare `curl` without an auth header gets redirected to
the Databricks sign-in page (as HTML, not a clean 401). From a notebook:

```python
from databricks.sdk import WorkspaceClient
import requests

wc = WorkspaceClient()
headers = wc.config.authenticate()   # use this dict directly -- don't hand-build
                                      # headers from wc.config.token, which can be None
resp = requests.post(f"{BASE}/weather/sync", headers=headers, json={...})
```

Or from the browser: `GET /weather` (the UI) and `GET /weather/search?...`
work directly in an already-logged-in browser session — only scripted
calls need the explicit header.

## Running the pipeline end-to-end

```bash
# 1. Harvest + normalize + upsert documents
curl -X POST https://<your-app-url>/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}'

# 2. Chunk + embed + write vectors (separate batch step, not part of the web app)
python ingest_weather_embeddings.py

# 3. Semantic search
curl -X POST https://<your-app-url>/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "risk of flooding near rivers", "top_k": 5}'

# 3b. Same search + LLM-generated summary (RAG) -- only the GET variant with
#     summarize=true pays for the LLM call; plain POST never does
curl "https://<your-app-url>/weather/search?query=risk+of+flooding+near+rivers&top_k=5&summarize=true"
```

Or use the browser UI at `https://<your-app-url>/weather` — sync panel,
search box with source/top_k filters, and a summary toggle.

Re-running `/weather/sync` and `ingest_weather_embeddings.py` is safe —
both upsert on stable keys (`weather_documents.id`, `weather_embeddings.id`
as `"{document_id}#{chunk_index}"`), and the embedding job re-embeds a
document only if its `content_hash` changed since it was last embedded
(so a re-issued alert with edited wording gets fresh vectors, not stale
ones).

## API reference

| Method | Path | Body / Query | Returns |
|---|---|---|---|
| `GET` | `/` | — | JSON landing page listing these endpoints |
| `GET` | `/healthz` | — | Health check |
| `GET` | `/weather` | — | Browser UI: sync panel + semantic search |
| `POST` | `/weather/sync` | `{"locations": [...], "limit": 50, "sources": ["alert","forecast","discussion"]}` | `{"synced": N, "by_source": {...}, "skipped": [...]}` |
| `POST` | `/weather/search` | `{"query": "...", "top_k": 5, "source_type": "alert"\|"forecast"\|"discussion"}` | `{"results": [...]}` — never generates a summary |
| `GET` | `/weather/search` | `?query=...&top_k=5&source_type=...&summarize=true` | `{"results": [...], "summary": "..." or null}` — summary only computed when `summarize=true` is passed |

`top_k` is clamped to 1-20. An empty `weather_embeddings` table or a
missing/empty `query` returns a clear message rather than an error.

## Scheduling the embedding job

```bash
databricks bundle deploy -t dev
databricks bundle run ingest_weather_embeddings_job -t dev
```

`resources/ingest_weather_embeddings_job.yml` runs harvest + embed every
30 minutes on serverless compute — created `PAUSED` until you've verified
a manual run works end to end. Alerts expire fast (a Severe Thunderstorm
Warning can have a 45-minute lifetime), which is why 30 minutes rather
than a daily cadence.

## Known limitations

See the "Known limitations / what I'd improve" section in
[`README_WEATHER.md`](./README_WEATHER.md) — covers the `KNOWN_LOCATIONS`
lookup-table geocoding approach (the Census geocoder was tested and
returns zero matches for plain "City, ST" input, so this project uses a
small built-in city table instead of live geocoding), fixed-width
chunking vs. AFD section markers, HNSW's approximate recall, and the fact
that only currently-active alerts are ever harvested (no retention pass
on `expires_at`).