"""The knowledge base and its retrieval endpoint.

Ingesting chunks and embeds a document; searching runs the same vector query
the personalisation agent runs before it writes a word. That last point is why
``POST /knowledge/search`` exists as a first-class endpoint rather than a
debug hook: "the RAG is real" is a claim, and this turns it into something a
reviewer can type a query into and watch return scored, cited chunks from the
corpus that was actually uploaded.

Visibility follows one rule, enforced in ``rag/store.py``: a campaign sees its
own documents plus platform-wide ones (``campaign_id`` null). One campaign's
confidential material never reaches another campaign's outreach.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from .. import audit
from ..models import KnowledgeChunk, KnowledgeDocument
from ..rag import store as rag_store
from ..rag.embedder import EMBEDDER_NAME
from ..rag.ingest import ingest_document
from ..serializers import iso, serialize_chunk
from .deps import SessionDep, UserDep, actor, clamp, load_campaign, load_document

router = APIRouter(tags=["knowledge"])

DocType = Literal[
    "product",
    "case_study",
    "playbook",
    "icp_definition",
    "objection_handling",
    "example_email",
    "voice_script",
]


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    doc_type: DocType
    content: str = Field(min_length=1)
    #: Null means platform-wide: every campaign may retrieve it.
    campaign_id: Optional[str] = None
    source: Optional[str] = Field(default=None, max_length=400)
    meta: dict[str, Any] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    campaign_id: Optional[str] = None
    doc_types: Optional[list[DocType]] = None
    limit: int = Field(default=5, ge=1, le=25)


def serialize_document(document: KnowledgeDocument, chunk_count: int = 0) -> dict:
    return {
        "id": document.id,
        "campaign_id": document.campaign_id,
        "title": document.title,
        "doc_type": document.doc_type,
        "source": document.source,
        "chunk_count": chunk_count,
        "chars": len(document.content or ""),
        "meta": document.meta,
        "created_at": iso(document.created_at),
    }


@router.get("/knowledge")
async def list_documents(
    session: SessionDep,
    _: UserDep,
    campaign_id: Annotated[Optional[str], Query()] = None,
    doc_type: Annotated[Optional[str], Query()] = None,
    include_platform: Annotated[bool, Query()] = True,
) -> dict:
    """List documents with their chunk counts.

    Chunk counts come from one grouped query over the whole result set, not a
    count per document. ``include_platform`` mirrors retrieval visibility, so
    what this page lists for a campaign is what that campaign can actually
    retrieve.
    """
    clauses = []
    if campaign_id:
        await load_campaign(session, campaign_id)
        if include_platform:
            clauses.append(
                (KnowledgeDocument.campaign_id == campaign_id)
                | (KnowledgeDocument.campaign_id.is_(None))
            )
        else:
            clauses.append(KnowledgeDocument.campaign_id == campaign_id)
    if doc_type:
        clauses.append(KnowledgeDocument.doc_type == doc_type)

    documents = (
        await session.execute(
            select(KnowledgeDocument).where(*clauses).order_by(KnowledgeDocument.created_at.desc())
        )
    ).scalars().all()

    counts: dict[str, int] = {}
    if documents:
        rows = await session.execute(
            select(KnowledgeChunk.document_id, func.count(KnowledgeChunk.id))
            .where(KnowledgeChunk.document_id.in_([d.id for d in documents]))
            .group_by(KnowledgeChunk.document_id)
        )
        counts = {document_id: int(count or 0) for document_id, count in rows}

    return {
        "items": [serialize_document(d, counts.get(d.id, 0)) for d in documents],
        "total": len(documents),
        "embedder": EMBEDDER_NAME,
    }


@router.post("/knowledge", status_code=201)
async def create_document(
    body: DocumentCreate, session: SessionDep, user: UserDep
) -> dict:
    """Ingest a document: chunk it, embed it, store it.

    Embedding happens in one batched call for the whole document, so uploading
    a long playbook is one provider request rather than forty.
    """
    if body.campaign_id:
        await load_campaign(session, body.campaign_id)

    document = await ingest_document(
        session,
        title=body.title.strip(),
        doc_type=body.doc_type,
        content=body.content,
        campaign_id=body.campaign_id,
        source=body.source,
        meta=body.meta,
    )

    chunk_count = int(
        (
            await session.execute(
                select(func.count(KnowledgeChunk.id)).where(
                    KnowledgeChunk.document_id == document.id
                )
            )
        ).scalar()
        or 0
    )

    await audit.record(
        session,
        entity_type="knowledge_document",
        entity_id=document.id,
        event_type="document_ingested",
        severity="success",
        message=(
            f"{document.title} ({document.doc_type}) added to the knowledge base by "
            f"{actor(user)} as {chunk_count} chunk(s)."
        ),
        campaign_id=body.campaign_id,
        actor=actor(user),
        payload={"doc_type": document.doc_type, "chunks": chunk_count, "embedder": EMBEDDER_NAME},
    )
    await session.commit()
    return serialize_document(document, chunk_count)


@router.delete("/knowledge/{document_id}")
async def delete_document(document_id: str, session: SessionDep, user: UserDep) -> dict:
    """Remove a document and everything retrieval could return from it.

    Chunks are deleted explicitly rather than relying on ``ON DELETE CASCADE``,
    because SQLite only enforces foreign keys when the pragma is on and a
    stranded chunk would keep being cited in outreach long after the document
    it came from was withdrawn.
    """
    document = await load_document(session, document_id)
    title, campaign_id = document.title, document.campaign_id

    removed = await session.execute(
        delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
    )
    await session.delete(document)

    await audit.record(
        session,
        entity_type="knowledge_document",
        entity_id=document_id,
        event_type="document_deleted",
        severity="warning",
        message=f"{title} removed from the knowledge base by {actor(user)}.",
        campaign_id=campaign_id,
        actor=actor(user),
        payload={"chunks_removed": removed.rowcount or 0},
    )
    await session.commit()
    return {"deleted": document_id, "chunks_removed": removed.rowcount or 0}


@router.post("/knowledge/search")
async def search_knowledge(body: SearchRequest, session: SessionDep, _: UserDep) -> dict:
    """Run the retrieval the agents run, and show the scores.

    Identical code path to the one ``agents/base.py`` calls before every
    customer-facing decision — same visibility rules, same embedder, same
    ranking. Scores come back so a weak match is visibly weak: retrieving a bad
    chunk and citing it confidently is worse than retrieving nothing.
    """
    if body.campaign_id:
        await load_campaign(session, body.campaign_id)

    results = await rag_store.search(
        session,
        body.query,
        campaign_id=body.campaign_id,
        doc_types=list(body.doc_types) if body.doc_types else None,
        limit=clamp(body.limit, 5, 25),
    )

    return {
        "query": body.query,
        "embedder": EMBEDDER_NAME,
        "items": [serialize_chunk(chunk, score) for chunk, score in results],
        "total": len(results),
    }


__all__ = ["router", "serialize_document"]
