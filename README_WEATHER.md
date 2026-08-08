# Weather Intelligence — Data & Vector Search Design

This project uses the National Weather Service (NWS) API as the main source of unstructured weather data.

The pipeline runs in Databricks. Local development is used for writing and updating code, while the actual application, synchronization, embedding job, and search are executed through the Databricks UI.

---

## 1. Data Source

The project uses the **National Weather Service API**:

`https://api.weather.gov`

The NWS API was chosen because it provides real-time, public weather information without requiring an API key.

Three types of data are collected:

| Source type | Description |
|---|---|
| `alert` | Active weather alerts |
| `forecast` | Forecast information for individual periods |
| `discussion` | Area Forecast Discussion written by NWS forecasters |

The pipeline first resolves a location to an NWS grid point and then retrieves the relevant weather information.

The data is normalized into a common document structure before being stored in Lakebase.

---

## 2. Schema Design

### `weather_documents`

This table stores the normalized weather documents.

Important columns include:

| Column | Purpose |
|---|---|
| `id` | Stable document identifier |
| `source_type` | `alert`, `forecast`, or `discussion` |
| `location` | Weather location |
| `narrative_text` | Main text used for embedding |
| `content_hash` | SHA-256 hash of the document text |
| `source_url` | Original NWS source |
| `issued_at` | Time the information was issued |
| `expires_at` | Expiration time when available |
| `metadata` | Additional source information |

The document ID is stable and source-prefixed, for example:

```text
alert:...
forecast:...
discussion:...
```

### `content_hash` is used to detect changes in the source text. If the NWS
re-issues a document with changed text, the embedding pipeline can recognize that the document needs to be embedded again.

---

### `weather_embeddings`

This table stores the vector representation of each document chunk. 

Important columns include:

| Column | Purpose |
|---|---|
| `id` | Unique embedding row ID |
| `document_id` | Reference to `weather_documents.id` |
| `source_type` | Source type for filtering |
| `chunk_index` | Position of the chunk within the document |
| `chunk_text` | Text represented by the vector |
| `content_hash` | Hash of the source document |
| `embedding` | 384-dimensional vector |
| `model_name` | Embedding model used |
| `created_at` | Embedding creation time |

`document_id` has a foreign-key relationship with
`weather_documents.id`.

The embedding table uses **pgvector** with:

```text
VECTOR(384)
```

and an HNSW index for vector similarity search.

---

## 3. Chunking and Embedding

Long weather documents are divided into smaller text chunks before embedding.

Current configuration:

```text
Chunk size:       800 characters
Chunk overlap:    100 characters
Embedding model:  sentence-transformers/all-MiniLM-L6-v2
Dimensions:       384
```

The overlap helps preserve context between adjacent chunks.

Most alerts and individual forecast periods are short enough to remain as one chunk. Longer Area Forecast Discussions are split into multiple chunks.

The embedding model produces a 384-dimensional vector for every chunk.

---

## 4. End-to-End Pipeline

The complete pipeline is:

```text
NWS API
   │
   │ fetch weather data
   ▼
Databricks App
/weather/sync
   │
   │ normalize + upsert
   ▼
weather_documents
   │
   │ Databricks Workflow
   │ ingest_weather_embeddings.py
   ▼
weather_embeddings
   │
   │ pgvector cosine similarity
   ▼
/weather/search
   │
   ▼
Semantic search results
```

---

## 5. How to Run

All execution is done through the **Databricks UI**.

### Step 1 — Sync weather data

Open the deployed Databricks App:

```text
weather-intelligence-app
```

Run the weather synchronization from the UI.

Example locations:

```text
Chicago, IL
Austin, TX
Miami, FL
```

This populates:

```text
weather_documents
```

The current successful run produced:

```text
36 weather documents
```

---

### Step 2 — Run the embedding job

In Databricks:

```text
Workflows → Jobs
```

Run the Python script:

```text
ingest_weather_embeddings.py
```

The job:

1. Reads documents that need embeddings.
2. Checks `content_hash`.
3. Chunks the document text.
4. Creates 384-dimensional embeddings.
5. Writes the vectors to `weather_embeddings`.

Current configuration:

```text
Model:          sentence-transformers/all-MiniLM-L6-v2
Chunk size:     800
Chunk overlap:  100
Batch size:     64
```

The current successful run produced:

```text
58 embedding rows
```

---

### Step 3 — Verify the embedding data

In the Databricks SQL editor connected to Lakebase:

```sql
SELECT COUNT(*) AS document_count
FROM weather_documents;
```

```sql
SELECT COUNT(*) AS embedding_count
FROM weather_embeddings;
```

Check for invalid foreign-key references:

```sql
SELECT COUNT(*) AS orphan_embeddings
FROM weather_embeddings e
LEFT JOIN weather_documents d
    ON e.document_id = d.id
WHERE d.id IS NULL;
```

Expected:

```text
document_count     = 36
embedding_count    = 58
orphan_embeddings  = 0
```

---

### Step 4 — Run semantic search

After embeddings are available, use the Databricks App's search UI or the `/weather/search` endpoint.

Example query:

```text
risk of flooding near rivers
```

The application:

1. Embeds the search query using the same embedding model.
2. Searches `weather_embeddings`.
3. Uses pgvector cosine similarity.
4. Returns the most relevant weather chunks.
5. Optionally generates an LLM summary.

---

## 6. Design Summary

The design separates the pipeline into three layers:

```text
weather_documents
       │
       │ source / normalized data
       ▼
weather_embeddings
       │
       │ vector representation
       ▼
semantic search
```

This separation allows the original weather documents to remain available while embeddings can be regenerated whenever the source text changes or a different embedding model is used.

The current implementation uses Lakebase/Postgres for both the document store and vector store, with pgvector providing the semantic similarity search.