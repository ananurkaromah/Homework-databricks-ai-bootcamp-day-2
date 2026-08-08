"""
ingest_weather_embeddings.py
---------------------------------------
Plain-Python (psycopg2-based) embedding ingestion for weather_documents.

Deliberately does NOT use spark.write.jdbc — Spark JDBC writes are
unreliable against this Lakebase instance, per the assignment constraints.
Everything here reads and writes through lakebase.get_connection()
(psycopg2), same as app.py.

Pipeline:
    1. Read weather_documents rows with no CURRENT embedding — a document
       counts as "needing embedding" if no weather_embeddings row exists
       for it with a matching content_hash. This is what makes a re-issued
       alert (same id, edited narrative_text) get re-embedded instead of
       silently serving stale vectors: the LEFT JOIN condition includes
       content_hash, so an id match with a stale hash still counts as
       unmatched.
    2. Chunk each document's narrative_text with a sliding window
       (CHUNK_SIZE=800 chars, CHUNK_OVERLAP=100 chars). Alerts and
       individual forecast periods are almost always under 800 chars and
       end up as one chunk each; Area Forecast Discussions are the one
       source long enough to actually split.
    3. Embed every chunk with sentence-transformers/all-MiniLM-L6-v2
       (384-dim).
    4. Batch-write (id, document_id, source_type, chunk_index, chunk_text,
       content_hash, embedding, model_name) into weather_embeddings via
       psycopg2.extras.execute_values, casting the embedding to ::vector.
       `id` is "{document_id}#{chunk_index}", which is itself the PK, so
       ON CONFLICT (id) DO UPDATE makes re-runs idempotent.

Run directly:
    python ingest_weather_embeddings.py [--batch-size 500]
"""

import argparse
import logging

from psycopg2.extras import execute_values

from lakebase import get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384  # must match lakebase.EMBEDDING_DIM / vector(384) column

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100

# A document counts as "unembedded" if no weather_embeddings row exists for
# it with a matching content_hash — that covers both never-embedded
# documents AND documents whose text changed since they were last embedded
# (NWS re-issues alerts under the same id with edited wording).
FETCH_UNEMBEDDED_SQL = """
SELECT d.id, d.source_type, d.narrative_text, d.content_hash
FROM weather_documents d
LEFT JOIN weather_embeddings e
       ON e.document_id = d.id AND e.content_hash = d.content_hash
WHERE e.id IS NULL
  AND d.narrative_text IS NOT NULL
  AND length(trim(d.narrative_text)) > 0;
"""

UPSERT_EMBEDDINGS_SQL = """
INSERT INTO weather_embeddings
    (id, document_id, source_type, chunk_index, chunk_text, content_hash, embedding, model_name)
VALUES %s
ON CONFLICT (id) DO UPDATE SET
    chunk_text   = EXCLUDED.chunk_text,
    content_hash = EXCLUDED.content_hash,
    embedding    = EXCLUDED.embedding,
    model_name   = EXCLUDED.model_name,
    created_at   = now();
"""

# Column order must match the INSERT list above:
# id, document_id, source_type, chunk_index, chunk_text, content_hash, embedding, model_name
_TEMPLATE = "(%s, %s, %s, %s, %s, %s, %s::vector, %s)"


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Sliding-window chunker over raw characters. A document shorter than
    chunk_size returns a single chunk (itself).
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
    Full ingestion pass: fetch documents needing (re-)embedding, chunk +
    embed, write. Returns the number of embedding rows written.
    """
    total_written = 0

    with get_connection() as conn:
        with conn.cursor() as cur:
            documents = fetch_unembedded_documents(cur)

        logger.info("Found %d document(s) needing embeddings.", len(documents))
        if not documents:
            return 0

        rows_to_write = []
        for doc in documents:
            chunks = chunk_text(doc["narrative_text"])
            if not chunks:
                continue
            vectors = embed_chunks(chunks)
            for idx, (chunk, vector) in enumerate(zip(chunks, vectors)):
                rows_to_write.append(
                    (
                        f"{doc['id']}#{idx}",
                        doc["id"],
                        doc["source_type"],
                        idx,
                        chunk,
                        doc["content_hash"],
                        vector,
                        MODEL_NAME,
                    )
                )

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
    # When run in Databricks (e.g. via the Jobs "Python script" task type),
    # call run() directly with the default batch size. For command-line
    # usage with --batch-size, uncomment main() and comment the lines below.
    written = run(batch_size=500)
    logger.info("Done. %d embedding rows written.", written)