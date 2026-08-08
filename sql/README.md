# sql/

Standalone SQL for the Weather Intelligence pipeline's Lakebase schema.
Everything here is also created automatically by `lakebase.py`'s
`ensure_weather_schema()` on every app startup — these files exist so the
schema can be reviewed, run by hand, or diffed independently of the
Python code, not because a manual step is required to use the app.

| File | What it does | When to run it |
|---|---|---|
| `01_setup_weather_documents_table.sql` | Creates `weather_documents` + its indexes | Once, before the app's first run, if you want the schema to exist before `app.py` boots |
| `02_setup_weather_embeddings_table.sql` | Enables `pgvector`, creates `weather_embeddings` + the HNSW index | After `01_...`, same reasoning |
| `03_report_queries.sql` | Nine read-only checks: schema, indexes, row counts, chunking stats, embedding sanity, a pure-SQL similarity search, coverage, sync evidence | Any time, after at least one `POST /weather/sync` and one run of `ingest_weather_embeddings.py` |

## Running these

Use a Databricks SQL editor connected to your Lakebase instance, or
`psql` directly:

```bash
psql "$DATABASE_URL" -f sql/01_setup_weather_documents_table.sql
psql "$DATABASE_URL" -f sql/02_setup_weather_embeddings_table.sql
```

Both are safe to re-run — every statement uses `CREATE ... IF NOT EXISTS`.

`03_report_queries.sql` is meant to be run query-by-query, not as one
script — copy individual `SELECT` blocks into your SQL editor as needed.
Query 7 (the pure-SQL similarity search) is worth running first if you
want to confirm cosine search actually works before trusting the Flask
`/weather/search` endpoint's Python-side embedding step — the probe row
returning at `similarity = 1.0000` confirms the `<=>` distance arithmetic
is correct.

## Role / privileges

All of this assumes you're connected as a role with `CREATE` on the
target schema for `01`/`02`, or just `SELECT` for `03`. See the main
[`README.md`](../README.md) for the `weather_app` role setup.