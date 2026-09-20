"""Turning source documents into retrievable, embedded chunks.

Chunking is the part of RAG that quietly decides answer quality. A chunk split
mid-sentence retrieves as a fragment and gets pasted into an email as one, so
the splitter here respects paragraph boundaries first, sentence boundaries
second, and word boundaries always — it will overshoot ``target_chars`` before
it will cut a word in half.

Overlap exists for the same reason: the one sentence that ties a case study's
metric to the customer's name is frequently the sentence on the boundary, and
without overlap neither chunk carries the whole fact.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import new_id
from ..models import KnowledgeChunk, KnowledgeDocument
from .embedder import EMBEDDER_NAME, embed_texts
from .store import upsert_chunks

logger = logging.getLogger(__name__)

_PARAGRAPH_RE = re.compile(r"\n\s*\n")
# Split after terminal punctuation followed by whitespace. Not linguistically
# perfect (it will break on "Inc. announced"), but wrong-by-one-sentence is a
# far cheaper error here than a dependency on a sentence tokenizer.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WHITESPACE_RE = re.compile(r"[ \t]+")


def _normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    # Collapse runs of blank lines so paragraph detection is stable across
    # documents pasted from wildly different sources.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_on_words(text: str, target_chars: int) -> list[str]:
    """Last-resort split for a sentence longer than one chunk.

    Never cuts inside a word: a single token longer than ``target_chars`` (a
    long URL, a base64 blob) is emitted intact and overshoots on purpose.
    """
    pieces: list[str] = []
    current: list[str] = []
    length = 0

    for word in text.split():
        addition = len(word) + (1 if current else 0)
        if current and length + addition > target_chars:
            pieces.append(" ".join(current))
            current, length = [word], len(word)
        else:
            current.append(word)
            length += addition

    if current:
        pieces.append(" ".join(current))
    return pieces


def _split_units(text: str, target_chars: int) -> list[tuple[int, str]]:
    """Break the document into the smallest pieces worth keeping together.

    Each unit is ``(paragraph_index, text)``. The index is carried so that the
    packer can rejoin sentences from one paragraph with a space and separate
    different paragraphs with a blank line, which keeps a chunk readable when
    it is pasted into a prompt.
    """
    units: list[tuple[int, str]] = []

    for para_index, paragraph in enumerate(_PARAGRAPH_RE.split(text)):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= target_chars:
            units.append((para_index, paragraph))
            continue

        for sentence in _SENTENCE_RE.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= target_chars:
                units.append((para_index, sentence))
            else:
                units.extend((para_index, piece) for piece in _split_on_words(sentence, target_chars))

    return units


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """Prefix each chunk with the tail of its predecessor.

    The tail is advanced to the next whitespace so a chunk never opens on half
    a word, and is dropped entirely when the tail contains no whitespace at
    all (one enormous token), since a fragment of it would be useless anyway.
    """
    if overlap <= 0 or len(chunks) < 2:
        return chunks

    result = [chunks[0]]
    for previous, current in zip(chunks, chunks[1:]):
        tail = previous[-overlap:]
        match = re.search(r"\s", tail)
        tail = tail[match.end() :].strip() if match else ""
        result.append(f"{tail}\n\n{current}" if tail else current)
    return result


def chunk_text(text: str, target_chars: int = 900, overlap: int = 150) -> list[str]:
    """Split ``text`` into overlapping chunks on natural boundaries.

    ``target_chars`` is a target, not a ceiling: a chunk may exceed it by the
    overlap prefix, or by an unbreakable token. Returns ``[]`` for empty input
    rather than a list containing an empty string, because an empty chunk would
    be embedded, stored and retrieved as meaningless noise.
    """
    normalised = _normalise(text or "")
    if not normalised:
        return []

    target_chars = max(1, target_chars)
    units = _split_units(normalised, target_chars)
    if not units:
        return []

    chunks: list[str] = []
    buffer: list[tuple[int, str]] = []
    buffer_len = 0

    def flush() -> None:
        nonlocal buffer, buffer_len
        if not buffer:
            return
        rendered = buffer[0][1]
        for (prev_para, _), (para, unit) in zip(buffer, buffer[1:]):
            rendered += ("\n\n" if para != prev_para else " ") + unit
        chunks.append(rendered)
        buffer, buffer_len = [], 0

    for para_index, unit in units:
        separator = 0 if not buffer else 2
        if buffer and buffer_len + separator + len(unit) > target_chars:
            flush()
        buffer.append((para_index, unit))
        buffer_len += len(unit) + (0 if len(buffer) == 1 else separator)

    flush()
    return _apply_overlap(chunks, overlap)


def _build_chunks(
    *,
    document: KnowledgeDocument,
    texts: list[str],
    vectors: list[list[float]],
    base_meta: dict,
) -> list[KnowledgeChunk]:
    total = len(texts)
    chunks: list[KnowledgeChunk] = []

    for index, (content, vector) in enumerate(zip(texts, vectors)):
        meta = dict(base_meta)
        meta.update(
            {
                "chunk_index": index,
                "chunk_count": total,
                "chars": len(content),
                # Stamped so a later embedder change is detectable rather than
                # silently poisoning every similarity score. store.search warns
                # on a mismatch; reindex_document repairs it.
                "embedder": EMBEDDER_NAME,
                "embedding_dim": settings.EMBEDDING_DIM,
                "document_title": document.title,
            }
        )
        chunks.append(
            KnowledgeChunk(
                id=new_id("kchk"),
                document_id=document.id,
                # Denormalised from the document so retrieval can filter on
                # campaign and doc_type without a join on the hot path.
                campaign_id=document.campaign_id,
                doc_type=document.doc_type,
                title=document.title,
                content=content,
                embedding=[float(v) for v in vector],
                meta=meta,
            )
        )

    return chunks


async def ingest_document(
    session: AsyncSession,
    *,
    title: str,
    doc_type: str,
    content: str,
    campaign_id: str | None = None,
    source: str | None = None,
    meta: dict | None = None,
) -> KnowledgeDocument:
    """Persist a document and its embedded chunks.

    ``campaign_id=None`` makes the document platform-wide: every campaign can
    retrieve it. Anything campaign-specific must carry its campaign id, which
    is what keeps one campaign's knowledge out of another's outreach.

    Every chunk is embedded in a single batched ``embed_texts`` call — one
    request per document rather than per chunk, which is both cheaper and much
    harder to rate-limit.

    Flushes but does not commit: the caller decides the transaction boundary so
    a document can be ingested alongside other work atomically.
    """
    base_meta = dict(meta or {})

    document = KnowledgeDocument(
        id=new_id("kdoc"),
        campaign_id=campaign_id,
        title=title,
        doc_type=doc_type,
        source=source,
        content=content,
        meta=base_meta,
    )
    session.add(document)
    await session.flush()

    texts = chunk_text(content)
    if not texts:
        # An empty document is stored anyway: the upload is a real event the
        # user can see and fix, and silently discarding it looks like a bug.
        logger.warning("Document %s (%r) produced no chunks", document.id, title)
        return document

    vectors = await embed_texts(texts)
    chunks = _build_chunks(document=document, texts=texts, vectors=vectors, base_meta=base_meta)
    await upsert_chunks(session, chunks)

    logger.info(
        "Ingested document %s (%r, type=%s, campaign=%s) as %d chunk(s) via %s",
        document.id,
        title,
        doc_type,
        campaign_id or "platform",
        len(chunks),
        EMBEDDER_NAME,
    )
    return document


async def reindex_document(session: AsyncSession, document_id: str) -> int:
    """Re-chunk and re-embed an existing document. Returns the chunk count.

    This is the repair path for the one failure mode that is otherwise
    invisible: documents ingested offline hold ``local-hashed`` vectors, and
    once an OpenAI key is configured the query vector lives in a different
    space, so similarity becomes noise while still looking like a number.
    Reindexing rewrites every chunk with the currently active embedder.

    Old chunks are deleted rather than updated in place because the chunk
    boundaries themselves may change if ``chunk_text`` is tuned.
    """
    document = (
        await session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
        )
    ).scalar_one_or_none()

    if document is None:
        raise ValueError(f"Unknown knowledge document: {document_id}")

    await session.execute(
        delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
    )

    texts = chunk_text(document.content)
    if not texts:
        await session.flush()
        logger.warning("Reindex of document %s produced no chunks", document.id)
        return 0

    vectors = await embed_texts(texts)
    chunks = _build_chunks(
        document=document,
        texts=texts,
        vectors=vectors,
        base_meta=dict(document.meta or {}),
    )
    await upsert_chunks(session, chunks)

    logger.info(
        "Reindexed document %s into %d chunk(s) via %s", document.id, len(chunks), EMBEDDER_NAME
    )
    return len(chunks)


__all__ = ["chunk_text", "ingest_document", "reindex_document"]
