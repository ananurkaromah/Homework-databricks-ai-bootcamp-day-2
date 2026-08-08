"""
app.py
------
Flask app: health check, browser UI, and the weather sync/search endpoints.

Endpoints:
    GET  /                 Landing page (JSON) listing available endpoints.
    GET  /healthz           Health check.
    GET  /weather           Browser UI: sync panel + semantic search.

    POST /weather/sync
        Body: {"locations": [...], "limit": 50, "sources": ["alert","forecast","discussion"]}
        Harvests + normalizes + upserts weather documents. Returns counts
        by source and any skipped (unresolvable) locations.

    POST /weather/search
        Body: {"query": "...", "top_k": 5, "source_type": "alert"|"forecast"|"discussion"}
        Cosine-similarity search over weather_embeddings via pgvector's
        `<=>` operator. Never generates an LLM summary (see GET variant).

    GET /weather/search?query=...&top_k=5&source_type=...&summarize=true
        Same retrieval. Only runs the RAG summary step when summarize=true
        is explicitly passed — plain GET calls skip the LLM entirely, so
        browsing results doesn't always pay for a summary nobody asked for.
"""

import logging

from flask import Flask, jsonify, render_template, request

import lakebase
import weather_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Ensure schema exists at startup (idempotent — safe to call every boot).
lakebase.ensure_weather_schema()

VALID_SOURCE_TYPES = ("alert", "forecast", "discussion")

# Load the embedding model once at module level, not per-request.
_embedding_model = None


def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer

        logger.info("Loading query embedding model: sentence-transformers/all-MiniLM-L6-v2")
        _embedding_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _embedding_model


def embed_query(text: str) -> list[float]:
    model = get_embedding_model()
    vector = model.encode([text], show_progress_bar=False, normalize_embeddings=True)[0]
    return vector.tolist()


# ---------------------------------------------------------------------------
# Root / health check / UI
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "app": "weather-intelligence",
            "status": "ok",
            "endpoints": {
                "GET /weather": "Browser UI: sync panel + semantic search",
                "POST /weather/sync": 'Body: {"locations": [...], "limit": 50, "sources": [...]}',
                "POST /weather/search": 'Body: {"query": "...", "top_k": 5, "source_type": "..."}',
                "GET /weather/search": "Query params: query, top_k, source_type, summarize",
            },
        }
    )


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"})


@app.route("/weather", methods=["GET"])
def weather_ui():
    """Browser UI: sync panel + semantic search (templates/weather.html)."""
    return render_template("weather.html")


# ---------------------------------------------------------------------------
# POST /weather/sync
# ---------------------------------------------------------------------------

@app.route("/weather/sync", methods=["POST"])
def weather_sync():
    body = request.get_json(silent=True) or {}
    locations = body.get("locations")
    limit = body.get("limit", 50)
    sources = body.get("sources") or list(VALID_SOURCE_TYPES)

    if not locations or not isinstance(locations, list):
        return jsonify({"error": "'locations' must be a non-empty list of strings"}), 400

    invalid_sources = [s for s in sources if s not in VALID_SOURCE_TYPES]
    if invalid_sources:
        return jsonify({"error": f"Invalid sources: {invalid_sources}. Must be from {VALID_SOURCE_TYPES}"}), 400

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return jsonify({"error": "'limit' must be an integer"}), 400
    limit = max(1, min(limit, 500))

    try:
        harvest_result = weather_client.harvest(locations, limit=limit, sources=sources)
        synced_count = lakebase.upsert_weather_documents(harvest_result["documents"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("weather_sync failed")
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "synced": synced_count,
            "locations": locations,
            "sources": sources,
            "by_source": harvest_result["by_source"],
            "skipped": harvest_result["skipped"],
        }
    )


# ---------------------------------------------------------------------------
# Retrieval (shared by POST and GET /weather/search)
# ---------------------------------------------------------------------------

# source_type is denormalized onto weather_embeddings, so a filtered search
# narrows on the join's driving table BEFORE joining weather_documents,
# rather than after.
SEARCH_SQL = """
SELECT d.id AS document_id, d.location, d.headline, d.event, d.severity,
       d.narrative_text, d.source_type, d.issued_at,
       e.chunk_text, e.chunk_index,
       1 - (e.embedding <=> %s::vector) AS similarity
FROM weather_embeddings e
JOIN weather_documents d ON d.id = e.document_id
{where_clause}
ORDER BY e.embedding <=> %s::vector
LIMIT %s;
"""


def run_similarity_search(query: str, top_k: int, source_type: str | None = None) -> list[dict]:
    query_vector = embed_query(query)

    where_clause = ""
    params = [query_vector]
    if source_type:
        where_clause = "WHERE e.source_type = %s"
        params.append(source_type)
    params.extend([query_vector, top_k])

    sql = SEARCH_SQL.format(where_clause=where_clause)

    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    return [
        {
            "location": row["location"],
            "headline": row["headline"],
            "event": row["event"],
            "severity": row["severity"],
            "source_type": row["source_type"],
            "issued_at": row["issued_at"].isoformat() if row["issued_at"] else None,
            "chunk_text": row["chunk_text"],
            "chunk_index": row["chunk_index"],
            "similarity": float(row["similarity"]),
        }
        for row in rows
    ]


def _validate_query_and_top_k(query, top_k_raw):
    """Shared validation/clamping for both POST and GET search handlers."""
    if not query or not isinstance(query, str) or not query.strip():
        return None, None, ("'query' is required and must be a non-empty string", 400)

    try:
        top_k = int(top_k_raw) if top_k_raw is not None else 5
    except (TypeError, ValueError):
        return None, None, ("'top_k' must be an integer", 400)

    top_k = max(1, min(top_k, 20))  # clamp to 1-20 per spec
    return query.strip(), top_k, None


def _validate_source_type(source_type):
    if source_type not in (None, "", *VALID_SOURCE_TYPES):
        return None, (f"'source_type' must be one of {VALID_SOURCE_TYPES}", 400)
    return (source_type or None), None


def _weather_embeddings_is_empty() -> bool:
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT EXISTS (SELECT 1 FROM weather_embeddings LIMIT 1) AS has_rows;")
            row = cur.fetchone()
    return not row["has_rows"]


# ---------------------------------------------------------------------------
# POST /weather/search  (never generates a summary)
# ---------------------------------------------------------------------------

@app.route("/weather/search", methods=["POST"])
def weather_search():
    body = request.get_json(silent=True) or {}
    query, top_k, error = _validate_query_and_top_k(body.get("query"), body.get("top_k"))
    if error:
        message, status = error
        return jsonify({"error": message}), status

    source_type, error = _validate_source_type(body.get("source_type"))
    if error:
        message, status = error
        return jsonify({"error": message}), status

    if _weather_embeddings_is_empty():
        return jsonify({"results": [], "message": "No weather data has been synced/embedded yet."})

    try:
        results = run_similarity_search(query, top_k, source_type=source_type)
    except Exception as exc:  # noqa: BLE001
        logger.exception("weather_search failed")
        return jsonify({"error": str(exc)}), 500

    return jsonify({"results": results, "count": len(results), "query": query})


# ---------------------------------------------------------------------------
# GET /weather/search  (summary only when explicitly requested)
# ---------------------------------------------------------------------------

def generate_rag_summary(query: str, results: list[dict]) -> str:
    """
    Small RAG summary: stitches the top result chunks into a prompt and
    asks an LLM (Anthropic API) for a short synthesis. Falls back to a
    plain extractive summary if no ANTHROPIC_API_KEY is configured.
    """
    if not results:
        return "No relevant weather information was found for this query."

    context = "\n\n".join(
        f"- [{r['source_type']}] {r['location']} — {r['headline']}: {r['chunk_text']}"
        for r in results
    )

    import os

    if not os.environ.get("ANTHROPIC_API_KEY"):
        top = results[0]
        return (
            f"Top match ({top['location']}, {top['source_type']}): "
            f"{top['headline']}. {top['chunk_text'][:280]}"
        )

    try:
        import anthropic

        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Question: {query}\n\n"
                        f"Relevant weather documents:\n{context}\n\n"
                        "Write a short (3-4 sentence) natural-language summary "
                        "answering the question using only the documents above."
                    ),
                }
            ],
        )
        return "".join(block.text for block in message.content if block.type == "text")
    except Exception as exc:  # noqa: BLE001
        logger.warning("RAG summary generation failed, falling back to extractive: %s", exc)
        top = results[0]
        return f"Top match ({top['location']}): {top['headline']}. {top['chunk_text'][:280]}"


@app.route("/weather/search", methods=["GET"])
def weather_search_rag():
    query, top_k, error = _validate_query_and_top_k(
        request.args.get("query"), request.args.get("top_k")
    )
    if error:
        message, status = error
        return jsonify({"error": message}), status

    source_type, error = _validate_source_type(request.args.get("source_type"))
    if error:
        message, status = error
        return jsonify({"error": message}), status

    wants_summary = request.args.get("summarize", "false").lower() in ("true", "1", "yes")

    if _weather_embeddings_is_empty():
        return jsonify(
            {"results": [], "count": 0, "query": query,
             "summary": "No weather data has been synced/embedded yet." if wants_summary else None}
        )

    try:
        results = run_similarity_search(query, top_k, source_type=source_type)
        summary = generate_rag_summary(query, results) if wants_summary else None
    except Exception as exc:  # noqa: BLE001
        logger.exception("weather_search_rag failed")
        return jsonify({"error": str(exc)}), 500

    return jsonify({"results": results, "count": len(results), "query": query, "summary": summary})


if __name__ == "__main__":
    import os

    port = int(os.environ.get("DATABRICKS_APP_PORT", 5000))
    app.run(host="0.0.0.0", port=port)