"""
ingest_weather_embeddings.py
---------------------------------------
Plain-Python (psycopg2-based) embedding ingestion for weather_documents.

Mirrors notebooks/ingest_ticker_news_embeddings.py, but deliberately does
NOT use spark.write.jdbc — Spark JDBC writes are unreliable against this
Lakebase instance, per the assignment constraints. Everything here reads
and writes through lakebase.get_connection() (psycopg2), same as the rest
of this project.

Pipeline:
    1. Read weather_documents rows that have no corresponding row(s) in
       weather_embeddings yet (a simple NOT EXISTS check against
       document_id — this doubles as the "unembedded rows" filter and
       makes the script safely re-runnable).
    2. Chunk each document's narrative_text with a sliding window
       (CHUNK_SIZE=800 chars, CHUNK_OVERLAP=100 chars). Most NWS alert
       and forecast text is well under 800 chars, so most documents end
       up as a single chunk — chunking mainly kicks in for combined
       alert description+instruction text or long AFD products.
    3. Embed every chunk with sentence-transformers/all-MiniLM-L6-v2
       (384-dim) — the same model as the existing ticker_news embedding
       pipeline, so both tables stay dimensionally compatible and
       queryable with the same `<=>` cosine-distance convention.
    4. Batch-write (document_id, chunk_index, chunk_text, embedding,
       model_name) into weather_embeddings via psycopg2.extras.execute_values,
       casting the embedding to ::vector. ON CONFLICT (document_id, chunk_index)
       DO UPDATE makes re-runs idempotent.

Run directly:
    python ingest_weather_embeddings.py [--batch-size 500]

Or import `run()` from a Databricks notebook cell / scheduled job.
"""

import argparse
import logging
import sys
from pathlib import Path

from psycopg2.extras import execute_values

# In Databricks, __file__ is not defined. Since lakebase.py is in the same directory,
# we can import it directly without sys.path manipulation.
from lakebase import get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384  # must match lakebase.EMBEDDING_DIM / vector(384) column

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

FETCH_UNEMBEDDED_SQL = """
SELECT d.id, d.narrative_text
FROM weather_documents d
WHERE NOT EXISTS (
    SELECT 1 FROM weather_embeddings e WHERE e.document_id = d.id
)
AND d.narrative_text IS NOT NULL
AND length(trim(d.narrative_text)) > 0;
"""

UPSERT_EMBEDDINGS_SQL = """
INSERT INTO weather_embeddings
    (document_id, chunk_index, chunk_text, embedding, model_name)
VALUES %s
ON CONFLICT (document_id, chunk_index) DO UPDATE SET
    chunk_text = EXCLUDED.chunk_text,
    embedding  = EXCLUDED.embedding,
    model_name = EXCLUDED.model_name,
    created_at = now();
"""

_TEMPLATE = "(%s, %s, %s, %s::vector, %s)"


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Sliding-window chunker over raw characters. Kept simple/dependency-free
    (no tokenizer) since NWS text is plain English prose and character
    count is a good-enough proxy for chunk size here.

    A document shorter than chunk_size returns a single chunk (itself).
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    step = chunk_size - overlap
    while start < len(text):
        chunk = text[start : start + chunk_size].strip()
        if chunk:
            chunks.append(chunk)
        start += step
    return chunks


def fetch_unembedded_documents(cur) -> list[dict]:
    cur.execute(FETCH_UNEMBEDDED_SQL)
    return cur.fetchall()


_model = None


def get_model():
    """Lazily load the sentence-transformers model once per process."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model: %s", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def embed_chunks(chunks: list[str]) -> list[list[float]]:
    model = get_model()
    vectors = model.encode(chunks, show_progress_bar=False, normalize_embeddings=True)
    return [v.tolist() for v in vectors]


def run(batch_size: int = 500) -> int:
    """
    Full ingestion pass: fetch unembedded docs, chunk + embed, write.
    Returns the number of embedding rows written.
    """
    total_written = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            documents = fetch_unembedded_documents(cur)

        logger.info("Found %d unembedded document(s).", len(documents))
        if not documents:
            return 0

        rows_to_write = []
        for doc in documents:
            chunks = chunk_text(doc["narrative_text"])
            if not chunks:
                continue
            vectors = embed_chunks(chunks)
            for idx, (chunk, vector) in enumerate(zip(chunks, vectors)):
                rows_to_write.append((doc["id"], idx, chunk, vector, MODEL_NAME))

        logger.info("Writing %d embedding row(s).", len(rows_to_write))

        with conn.cursor() as cur:
            for i in range(0, len(rows_to_write), batch_size):
                batch = rows_to_write[i : i + batch_size]
                execute_values(cur, UPSERT_EMBEDDINGS_SQL, batch, template=_TEMPLATE)
                total_written += len(batch)
                logger.info("Wrote batch of %d (running total: %d)", len(batch), total_written)

    return total_written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()

    written = run(batch_size=args.batch_size)
    logger.info("Done. %d embedding rows written.", written)


if __name__ == "__main__":
    # When run in Databricks, call run() directly with default batch size
    # For command-line usage with argparse, uncomment main() and comment the lines below
    written = run(batch_size=500)
    logger.info("Done. %d embedding rows written.", written)