# Weather Intelligence — Lakebase Vector Search Databricks App

Day 2 homework for the Context Engineering on Databricks course.

This project is an end-to-end unstructured-data pipeline that ingests free-text weather alerts and forecast discussions from the National Weather Service (`api.weather.gov`), stores them in **Lakebase** (Databricks-managed Postgres), creates vector embeddings using **pgvector**, and provides semantic search through a Flask API and web UI deployed as a Databricks App.

**Databricks App name:** `weather-intelligence-app`

---

## What this is

| Stage | What happens | File |
|---|---|---|
| Harvest | Resolve locations → NWS grid points → fetch active alerts + forecast discussions → normalize into documents | `weather_client.py` |
| Store | Upsert normalized documents into Lakebase | `lakebase.py`, `app.py` |
| Vectorize | Chunk text (800/100), create embeddings with `all-MiniLM-L6-v2`, and store vectors | `ingest_weather_embeddings.py` |
| Retrieve | Search using cosine similarity with pgvector | `app.py`, `templates/weather.html` |

---

## Architecture

```text
NWS API
   │
   │ weather alerts + forecast discussions
   ▼
weather_client.py
   │
   │ normalize + sync
   ▼
weather_documents
(Lakebase / Postgres)
   │
   │ chunk + embed
   ▼
ingest_weather_embeddings.py
   │
   │ 800 chars / 100 overlap
   │ all-MiniLM-L6-v2
   ▼
weather_embeddings
(pgvector, VECTOR(384))
   │
   │ cosine similarity
   ▼
app.py
(Flask Databricks App)
   │
   ├── /weather/sync
   │
   └── /weather/search
```

The application runs as a **Databricks App** and uses a **Lakebase Postgres** database with the `pgvector` extension.

Lakebase authentication uses the `weather_app` PostgreSQL role with native password authentication. The connection URL is stored as a Databricks secret and accessed by `lakebase.py`.

---

## Project Structure

```text
.
├── README.md
│   └── Project overview and deployment guide
│
├── README_WEATHER.md
│   └── Schema and design details
│
├── app.py
│   └── Flask app and API endpoints
│
├── app.yml
│   └── Databricks App configuration
│
├── databricks.yml
│   └── Databricks Asset Bundle configuration
│
├── ingest_weather_embeddings.py
│   └── Python script for chunking and embedding
│
├── lakebase.py
│   └── Lakebase connection, schema, and vector search
│
├── requirements.txt
│   └── Python dependencies
│
├── resources/
│   └── ingest_weather_embeddings_job.yml
│       └── Databricks Workflow job definition
│
├── setup_secret.py
│   └── One-time Lakebase secret setup
│
├── sql/
│   ├── 01_setup_weather_documents_table.sql
│   ├── 02_setup_weather_embeddings_table.sql
│   ├── 03_report_queries.sql
│   └── README.md
│
├── templates/
│   └── weather.html
│       └── Web UI
│
└── weather_client.py
    └── NWS API client and document normalization
```

---

## How the project is operated

**Local environment is used only for writing and updating code.**

The application, database, synchronization, embedding job, and testing are performed in the **Databricks UI**.

```text
Local / GitHub
      │
      │ code updates
      ▼
Databricks Git Folder
      │
      ├──────────────► Databricks App
      │
      └──────────────► Databricks Workflow
                           │
                           ▼
                      Lakebase
```

---

## Setup

### 1. Create Lakebase

In Databricks:

1. Go to **Catalog → Lakebase**.
2. Create a Lakebase instance.
3. Open **Roles & Databases**.
4. Enable **Native passwords**.
5. Create the PostgreSQL role:

```text
weather_app
```

6. Save the Lakebase connection URL.

---

### 2. Store the Lakebase Secret

Run `setup_secret.py` from the Databricks environment.

The connection URL is stored as:

```text
database/lakebase-url
```

The NWS API does not require an API key.

---

### 3. Create the Databricks Git Folder

In Databricks:

**Workspace → Create → Git folder**

Connect the GitHub repository containing this project.

The repository contains the application code and embedding script.

---

## Deploy the Databricks App

In Databricks:

1. Go to **Compute → Apps**.
2. Click **Create app**.
3. Choose **Custom**.
4. Select the project Git folder.
5. Deploy the app.
6. Check the **Logs** tab.

The app provides:

```text
/healthz
/
/weather
/weather/sync
/weather/search
```

---

# Run the Pipeline

## Step 1 — Sync Weather Data

Open the deployed Databricks App.

Use the weather sync function from the UI or call:

```text
POST /weather/sync
```

Example locations:

```text
Chicago, IL
Austin, TX
Miami, FL
```

This creates or updates records in:

```text
weather_documents
```

The current successful run produced:

```text
weather_documents = 36 rows
```

---

## Step 2 — Create Embeddings

Embedding is executed through **Databricks Workflows UI**.

Go to:

**Workflows → Jobs**

Create a job with:

```text
Task type: Python script
```

Use:

```text
ingest_weather_embeddings.py
```

Configure the job with the required Python libraries, including:

```text
pg8000
sentence-transformers
```

Embedding configuration:

```text
Model:
sentence-transformers/all-MiniLM-L6-v2

Chunk size:
800

Chunk overlap:
100

Batch size:
64
```

Run the job using **Run now**.

The successful run produced:

```text
weather_embeddings = 58 rows
```

---

## Step 3 — Schedule the Embedding Job

The embedding job can be scheduled from:

**Workflows → Jobs → Add trigger → Scheduled**

Example:

```text
Every 30 minutes
```

This keeps embeddings updated as weather alerts change.

---

## Step 4 — Test Semantic Search

After embeddings have been created, test:

```text
/weather/search
```

Example query:

```text
risk of flooding near rivers
```

The search uses the vector embeddings stored in:

```text
weather_embeddings
```

and performs cosine similarity search using pgvector.

---

# API Reference

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Check application health |
| `GET` | `/` | Open web UI |
| `GET` | `/weather` | Open weather UI |
| `POST` | `/weather/sync` | Sync weather data into Lakebase |
| `POST` | `/weather/search` | Semantic search |
| `GET` | `/weather/search` | Semantic search with optional summary |

Example search request:

```json
{
  "query": "risk of flooding near rivers",
  "top_k": 5
}
```

---

# Verify the Data in Lakebase

After synchronization:

```sql
SELECT COUNT(*) AS document_count
FROM weather_documents;
```

After embedding:

```sql
SELECT COUNT(*) AS embedding_count
FROM weather_embeddings;
```

Check for orphan embeddings:

```sql
SELECT COUNT(*) AS orphan_embeddings
FROM weather_embeddings e
LEFT JOIN weather_documents d
    ON e.document_id = d.id
WHERE d.id IS NULL;
```

Expected:

```text
orphan_embeddings = 0
```

---

## Current Pipeline Result

The current successful pipeline has:

```text
weather_documents
        ↓
36 documents
        ↓
ingest_weather_embeddings.py
        ↓
58 embedding rows
        ↓
weather_embeddings
        ↓
semantic search
```

---

## Technical Notes

- **Lakebase:** Databricks-managed PostgreSQL.
- **pgvector:** Used for storing and searching embeddings.
- **Embedding model:** `sentence-transformers/all-MiniLM-L6-v2`.
- **Embedding dimension:** 384.
- **Chunk size:** 800 characters.
- **Chunk overlap:** 100 characters.
- **Embedding execution:** Databricks Workflows using a Python script task.
- **Application:** Flask running as a Databricks App.
- **Development:** Code is written and updated locally/GitHub; execution is performed in Databricks UI.
- **Idempotency:** Embedding ingestion uses document ID, chunk index, and content hash to avoid stale or duplicate embeddings.

---

## Main Workflow

```text
1. Update code locally
        ↓
2. Push code to GitHub
        ↓
3. Sync Git folder in Databricks
        ↓
4. Deploy/update Databricks App
        ↓
5. Run /weather/sync
        ↓
6. Check weather_documents
        ↓
7. Run embedding Workflow
        ↓
8. Check weather_embeddings
        ↓
9. Test /weather/search
        ↓
10. Schedule embedding Workflow
```

The project is intentionally operated primarily through the **Databricks UI**, while the local environment is used for code development and Git version control.

**weather-intelligence-app UI`s URL:**
**https://weather-intelligence-app-7474652369259280.aws.databricksapps.com/weather**

**Data Source: National Weather Service API (api.weather.gov)**