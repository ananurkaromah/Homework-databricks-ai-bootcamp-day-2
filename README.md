# Weather Intelligence — Lakebase Vector Search Databricks App

Day 2 homework for the Context Engineering on Databricks course.

This project is an end-to-end unstructured-data pipeline that ingests free-text weather alerts and forecast discussions from the National Weather Service (`api.weather.gov`), embeds them into **Lakebase** (Databricks-managed Postgres) using **pgvector**, and serves semantic search (with an optional RAG summary) via a Flask API and a simple web UI deployed as a Databricks App.

**Databricks App name:** `weather-intelligence-app`

---

## What this is

| Stage | What happens | File |
|---|---|---|
| Harvest | Resolve locations → NWS grid points → fetch active alerts + forecast discussions → normalize into a common document schema | `weather_client.py` |
| Store | Upsert normalized documents into Lakebase (Postgres) via `pg8000` | `lakebase.py`, `app.py` (`POST /weather/sync`) |
| Vectorize | Chunk long text (800/100), embed with `sentence-transformers/all-MiniLM-L6-v2`, and batch-write vectors | `ingest_weather_embeddings.py` |
| Retrieve | Cosine-similarity search over embeddings via pgvector’s `<=>` operator, with an optional LLM summary | `app.py` (`POST`/`GET /weather/search`), `templates/weather.html` |

For schema decisions, chunking parameters, and design rationale, see [`README_WEATHER.md`](./README_WEATHER.md).

---

## Architecture
NWS API (api.weather.gov)

│  alerts + forecast discussions

▼

weather_client.py  ──normalize──▶  weather_documents  (Lakebase / Postgres)

│

│  chunk + embed (800/100, MiniLM-L6-v2)

▼

weather_embeddings  (pgvector, VECTOR(384) + HNSW)

│

│  cosine similarity <=>

▼

app.py (Flask)

/weather/sync    /weather/search

```

Runs as a Databricks App backed by a Lakebase Postgres instance with the `pgvector` extension enabled. Auth to Lakebase uses a dedicated `weather_app` Postgres role with **native password auth** (not OAuth), with the connection string stored securely as a Databricks secret and resolved at runtime by `lakebase.py`.

---

## Project structure
```

.

├── README.md                           # Project overview & deployment guide (this file)

├── README_WEATHER.md                   # Schema & design deep-dive (assignment write-up)

├── app.py                              # Flask app: /healthz, UI, /weather/sync, /weather/search

├── app.yml                             # Databricks App deployment config (command + env vars)

├── databricks.yml                      # Databricks Asset Bundle (DAB) config

├── ingest_weather_embeddings.py        # Batch job: chunk, embed, write vectors (script & notebook)

├── lakebase.py                         # Connection helper + schema & vector queries

├── requirements.txt                    # Project dependencies (Flask, pg8000, sentence-transformers, etc.)

├── resources

│   └── ingest_weather_embeddings_job.yml # DAB job definition for scheduled workflows

├── setup_secret.py                     # One-time: store Lakebase connection URL as a Databricks secret

├── sql

│   ├── 01_setup_weather_documents_table.sql # Optional manual DDL for documents table

│   ├── 02_setup_weather_embeddings_table.sql # Optional manual DDL for embeddings table

│   ├── 03_report_queries.sql           # Verification & report queries

│   └── README.md                       # SQL folder documentation

├── templates

│   └── weather.html                    # Web UI for sync + semantic search

└── weather_client.py                   # NWS API client + document normalization logic

```

---

## Prerequisites & Setup

### 1. Create a Lakebase Instance & Native-Password Role
1. In your Databricks workspace, go to **Catalog** > **Lakebase** tab.
2. Click **Create Lakebase instance**, name it (e.g., `weather-search-db`), and wait for it to run.
3. Open the instance, go to **Roles & Databases**, and ensure **Native passwords** is enabled.
4. Create a new role named `weather_app` using **Password** authentication and copy its connection URL:
   ```text
   postgresql://weather_app:<password>@<host>.database.cloud.databricks.com:5432/databricks_postgres?sslmode=require
```

### 2. Store the Lakebase Secret

Run this once from a Databricks notebook in your workspace (via terminal or cell):

Bash

```
python setup_secret.py
```

This securely prompts for your Lakebase connection URL and stores it as the Databricks secret `database/lakebase-url`. (The NWS API requires no API key).

### 3. Local Development Setup

Bash

```
git clone <your-repo-url>
cd weather-intelligence
pip install -r requirements.txt
cp .env.example .env   # (Optional) Set WEATHER_USER_AGENT for local testing
python app.py          # Runs locally at http://localhost:8000
```

*(Note: Tables are created automatically on startup via `weather_db.ensure_weather_tables()` in `lakebase.py` if they don't exist).*

## Deploying to Databricks Apps (No CLI Required)

1. **Create a Git Folder in Databricks:**
    - Go to **Workspace** > **Create** > **Git folder**, paste your repository URL, and click **Create**.
2. **Create and Deploy the App:**
    - Go to **Compute** > **Apps** > **Create app**, choose **Custom**, and point it to your Git folder.
    - Databricks will automatically read `app.yml` for configuration and secrets.
    - Click **Deploy**. Check the app's **Logs** tab to confirm schema initialization.

## Running the Pipeline End-to-End

Bash

```
# 1. Harvest + normalize + upsert documents into Lakebase
curl -X POST https://<your-app-url>/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}'

# 2. Run the embedding job (via Databricks Workflow or locally)
python ingest_weather_embeddings.py

# 3. Test semantic search
curl -X POST https://<your-app-url>/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "risk of flooding near rivers", "top_k": 5}'

# 3b. Search + LLM-generated RAG summary
curl "https://<your-app-url>/weather/search?query=risk+of+flooding+near+rivers&top_k=5"
```

## API Reference

| **Method** | **Path** | **Body / Query Parameters** | **Returns** |
| --- | --- | --- | --- |
| `GET` | `/healthz` | — | Health status |
| `GET` | `/` or `/weather` | — | Web UI for sync & semantic search |
| `POST` | `/weather/sync` | `{"locations": [...], "limit": 50}` | `{"synced": <count>, "locations": [...]}` |
| `POST` | `/weather/search` | `{"query": "...", "top_k": 5, "source_type": "alert"}` | `{"results": [...]}` |
| `GET` | `/weather/search` | `?query=...&top_k=5&summarize=true` | `{"results": [...], "summary": "..."}` |

## Scheduling the Embedding Job

You can schedule `ingest_weather_embeddings.py` using **Databricks Asset Bundles (CLI)** or the **Workflows UI**:

- **Via CLI (DAB):**Bash
    
    ```
    databricks bundle deploy -t dev
    databricks bundle run ingest_weather_embeddings_job -t dev
    ```
    
- **Via Workflows UI:** Create a Job pointing to `ingest_weather_embeddings.py`, attach libraries (`pg8000`, `sentence-transformers`), set parameters, and schedule it (e.g., every 30 minutes since weather alerts expire quickly).

## Enabling Change Data Feed (CDF) for Postgres Tables

Lakebase supports streaming row changes into Unity Catalog Delta tables:

1. Enable `REPLICA IDENTITY FULL` on your tables:SQL
    
    ```
    ALTER TABLE weather_documents REPLICA IDENTITY FULL;
    ALTER TABLE weather_embeddings REPLICA IDENTITY FULL;
    ```
    
2. Go to the **Lakebase** tab in your Databricks workspace, select **Lakebase CDF**, and click **Start** to begin syncing into Unity Catalog.

## Technical Notes

- **Driver (`pg8000`):** The app and embedding scripts use `pg8000` instead of `psycopg2` to prevent kernel crashes on Databricks Serverless compute.
- **Singleton Model Loading:** The embedding model (`all-MiniLM-L6-v2`) is loaded once per process to optimize performance.