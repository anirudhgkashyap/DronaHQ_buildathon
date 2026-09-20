"""Vector retrieval over ``KnowledgeChunk``, on either Postgres or SQLite.

The platform has to run in two places: production on Postgres with pgvector,
and a judge's laptop on a single SQLite file. Rather than making every agent
branch on the backend, the branch lives here and both paths return the exact
same shape — ``list[tuple[KnowledgeChunk, float]]``, best-first, score in 0..1.

* **Postgres**: a real ANN-capable query. ``embedding <=> :vec`` (cosine
  distance) is evaluated in the database, so only ``limit`` rows ever cross the
  wire and the work scales to a corpus far larger than memory.
* **SQLite**: the column is JSON, so candidates are filtered in SQL (the part
  that matters for correctness and isolation) and scored in numpy. Fine for a
  demo corpus; deliberately capped so a large import degrades loudly rather
  than exhausting memory.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import KnowledgeChunk
from .embedder import EMBEDDER_NAME, embed_query

logger = logging.getLogger(__name__)

# SQLite scores in Python, so the candidate set has to be bounded. 5k chunks is
# roughly 30MB of float32 at 1536 dimensions — comfortable, and far beyond any
# realistic demo corpus. Past that we take the most recent chunks and warn,
# because silently returning worse results is the failure nobody notices.
_SQLITE_MAX_CANDIDATES = 5000

_warned_embedder_mismatch = False


def _visibility_clause(campaign_id: Optional[str]):
    """Campaign isolation, expressed once.

    A campaign sees its own knowledge *plus* platform-wide knowledge
    (``campaign_id IS NULL``) — that is how the objection-handling playbook is
    shared by everyone while one campaign's confidential pricing sheet never
    leaks into another campaign's outreach.

    ``campaign_id=None`` means "no campaign filter" (admin / global search),
    not "platform-wide only". Callers that want the platform-wide corpus alone
    should pass ``doc_types`` or query the documents table directly.
    """
    if campaign_id is None:
        return None
    return or_(
        KnowledgeChunk.campaign_id == campaign_id,
        KnowledgeChunk.campaign_id.is_(None),
    )


def _build_filters(campaign_id: Optional[str], doc_types: Optional[list[str]]) -> list:
    filters = [KnowledgeChunk.embedding.is_not(None)]

    visibility = _visibility_clause(campaign_id)
    if visibility is not None:
        filters.append(visibility)

    if doc_types:
        filters.append(KnowledgeChunk.doc_type.in_(list(doc_types)))

    return filters


def _warn_on_space_mismatch(chunks: Sequence[KnowledgeChunk]) -> None:
    """Chunks embedded by a different backend live in a different vector space.

    Scores against them are noise, not similarity. This happens when an API key
    is added after documents were ingested offline; the fix is
    ``reindex_document``. Warn once rather than per-search so it is visible in
    logs without drowning them.
    """
    global _warned_embedder_mismatch
    if _warned_embedder_mismatch:
        return
    for chunk in chunks:
        stamped = (chunk.meta or {}).get("embedder")
        if stamped and stamped != EMBEDDER_NAME:
            logger.warning(
                "Knowledge chunk %s was embedded with %r but the active embedder is %r; "
                "similarity scores are unreliable until the document is reindexed.",
                chunk.id,
                stamped,
                EMBEDDER_NAME,
            )
            _warned_embedder_mismatch = True
            return


async def _search_postgres(
    session: AsyncSession,
    vector: list[float],
    filters: list,
    limit: int,
) -> list[tuple[KnowledgeChunk, float]]:
    # ``cosine_distance`` renders the pgvector ``<=>`` operator, so ordering and
    # limiting happen inside the database and an ivfflat/hnsw index can be used.
    distance = KnowledgeChunk.embedding.cosine_distance(vector)
    stmt = (
        select(KnowledgeChunk, distance.label("distance"))
        .where(*filters)
        .order_by(distance)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    results: list[tuple[KnowledgeChunk, float]] = []
    for chunk, raw_distance in rows:
        # Cosine distance is 0..2; similarity is 1 - d, clamped into 0..1 so
        # callers can apply a single relevance threshold on either backend.
        similarity = 1.0 - float(raw_distance)
        results.append((chunk, max(0.0, min(1.0, similarity))))
    return results


async def _search_sqlite(
    session: AsyncSession,
    vector: list[float],
    filters: list,
    limit: int,
) -> list[tuple[KnowledgeChunk, float]]:
    stmt = (
        select(KnowledgeChunk)
        .where(*filters)
        .order_by(KnowledgeChunk.created_at.desc())
        .limit(_SQLITE_MAX_CANDIDATES + 1)
    )
    candidates = list((await session.execute(stmt)).scalars().all())

    if len(candidates) > _SQLITE_MAX_CANDIDATES:
        logger.warning(
            "SQLite retrieval capped at %d of %d+ candidate chunks; move to "
            "Postgres + pgvector for a corpus this size.",
            _SQLITE_MAX_CANDIDATES,
            _SQLITE_MAX_CANDIDATES,
        )
        candidates = candidates[:_SQLITE_MAX_CANDIDATES]

    if not candidates:
        return []

    query_vec = np.asarray(vector, dtype=np.float32)
    dim = query_vec.shape[0]

    usable: list[KnowledgeChunk] = []
    rows: list[list[float]] = []
    for chunk in candidates:
        embedding = chunk.embedding
        # A row whose width does not match the live embedder cannot be scored
        # at all. Skipping is the honest answer; the alternative is a crash
        # mid-outreach.
        if not embedding or len(embedding) != dim:
            logger.debug("Skipping chunk %s: embedding width mismatch", chunk.id)
            continue
        usable.append(chunk)
        rows.append(embedding)

    if not usable:
        return []

    matrix = np.asarray(rows, dtype=np.float32)

    # Both embedders emit unit vectors, but normalising here anyway means a
    # chunk written by some other tool still ranks correctly instead of
    # dominating purely because its magnitude is large.
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0.0] = 1.0
    matrix = matrix / norms[:, None]

    query_norm = float(np.linalg.norm(query_vec))
    if query_norm > 0.0:
        query_vec = query_vec / query_norm

    scores = matrix @ query_vec
    top = np.argsort(-scores)[:limit]

    return [(usable[int(i)], float(max(0.0, min(1.0, scores[int(i)])))) for i in top]


async def search(
    session: AsyncSession,
    query: str,
    *,
    campaign_id: str | None = None,
    doc_types: list[str] | None = None,
    limit: int = 5,
) -> list[tuple[KnowledgeChunk, float]]:
    """Retrieve the chunks most relevant to ``query``.

    Returns ``(chunk, similarity)`` pairs sorted best-first, where similarity
    is in 0..1 and higher is better. The score is returned rather than hidden
    so callers can refuse to personalise on weak evidence — retrieving a bad
    chunk and citing it confidently is worse than retrieving nothing.
    """
    if not query or not query.strip() or limit <= 0:
        return []

    vector = await embed_query(query)
    filters = _build_filters(campaign_id, doc_types)

    if settings.is_postgres:
        results = await _search_postgres(session, vector, filters, limit)
    else:
        results = await _search_sqlite(session, vector, filters, limit)

    _warn_on_space_mismatch([chunk for chunk, _ in results])
    return results


async def upsert_chunks(session: AsyncSession, chunks: list[KnowledgeChunk]) -> None:
    """Insert new chunks, replace existing ones by id.

    Reindexing reuses chunk ids, so a plain ``add_all`` would raise on the
    second pass. Existing ids are found in a single SELECT and merged; the rest
    are added directly, which keeps the common case (a fresh document) at one
    round trip instead of one per chunk.

    The caller owns the transaction — this flushes but never commits, so a
    document and its chunks land atomically.
    """
    if not chunks:
        return

    ids = [chunk.id for chunk in chunks]
    existing_stmt = select(KnowledgeChunk.id).where(KnowledgeChunk.id.in_(ids))
    existing = set((await session.execute(existing_stmt)).scalars().all())

    fresh = [chunk for chunk in chunks if chunk.id not in existing]
    if fresh:
        session.add_all(fresh)

    for chunk in chunks:
        if chunk.id in existing:
            await session.merge(chunk)

    await session.flush()


__all__ = ["search", "upsert_chunks"]
