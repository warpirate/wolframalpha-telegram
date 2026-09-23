"""Embeddings and similarity search over everything the user has saved."""

from __future__ import annotations

import logging

import db
from ai_client import AIError, NebiusClient
from config import NEBIUS_EMBED_MODEL

logger = logging.getLogger(__name__)

EMBED_DIMENSIONS = 1024  # must match vector(1024) in db_pg.SCHEMA_LIBRARY
EMBED_BATCH = 32
CHUNK_CHARS = 800
CHUNK_OVERLAP = 150
MIN_SCORE = 0.45  # below this a hit is noise, not context
DUPLICATE_SCORE = 0.97
CHAPTER_MATCH_SCORE = 0.5

_SOURCE_LABELS = {"page": "Book page", "pyq": "PYQ", "chapter": "Chapter"}


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on line or word boundaries into overlapping chunks of at most `size` chars."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut == -1:
                cut = text.rfind(" ", start + size // 2, end)
            if cut != -1:
                end = cut
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        next_start = max(end - overlap, start + 1)
        space = text.find(" ", next_start, end)
        start = space + 1 if space != -1 else next_start  # never start mid-word
    return chunks


async def embed(client: NebiusClient, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        vectors.extend(
            await client.embed(texts[i : i + EMBED_BATCH], NEBIUS_EMBED_MODEL, EMBED_DIMENSIONS)
        )
    return vectors


async def index_texts(
    client: NebiusClient,
    user_id: int,
    source: str,
    source_id: int,
    texts: list[str],
    subject: str,
    topic: str,
    batch_id: str,
) -> int:
    """Embed and store texts for one saved item. Returns how many were indexed."""
    texts = [t for t in texts if t.strip()]
    if not texts:
        return 0
    vectors = await embed(client, texts)
    await db.add_chunks(user_id, source, source_id, list(zip(texts, vectors)), subject, topic, batch_id)
    return len(texts)


async def search(
    client: NebiusClient,
    user_id: int,
    query: str,
    k: int = 6,
    sources: list[str] | None = None,
    min_score: float = MIN_SCORE,
) -> list[dict]:
    """Closest saved chunks to the query. Empty on embedding failure - answers still work."""
    if not query.strip():
        return []
    try:
        [vector] = await embed(client, [query[:2000]])
    except AIError as exc:
        logger.warning("Embedding the query failed: %s", exc.detail)
        return []
    hits = await db.search_chunks(user_id, vector, k, sources)
    return [hit for hit in hits if hit["score"] >= min_score]


def render_context(hits: list[dict]) -> str:
    blocks = []
    for number, hit in enumerate(hits, 1):
        label = _SOURCE_LABELS.get(hit["source"], hit["source"])
        topic = f" · {hit['topic']}" if hit.get("topic") else ""
        blocks.append(f"[{number}] {label}{topic}\n{hit['text']}")
    return "\n\n".join(blocks)
