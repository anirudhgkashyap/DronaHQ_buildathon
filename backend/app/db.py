"""Async database engine and session management.

Works against Postgres (production, with pgvector) and SQLite (local demo).
The vector store picks its backend from the same URL, so a laptop with no
Postgres still runs the full pipeline.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    if settings.is_postgres:
        return {"pool_size": 10, "max_overflow": 20, "pool_pre_ping": True}
    # SQLite needs no pooling args and rejects them.
    return {}


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    **_engine_kwargs(),
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, always closed."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """For background workers that live outside the request cycle."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def new_id(prefix: str) -> str:
    """Opaque, prefixed, sortable-enough identifiers.

    The frontend treats IDs as opaque strings, and a prefix makes logs and
    audit trails readable without a join.
    """
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


async def init_models() -> None:
    """Create tables and the pgvector extension when running on Postgres."""
    from sqlalchemy import text

    from . import models  # noqa: F401  (registers mappers)

    async with engine.begin() as conn:
        if settings.is_postgres:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
