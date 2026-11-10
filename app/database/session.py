from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(database_url: str, **kwargs) -> AsyncEngine:
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    kwargs.setdefault("pool_pre_ping", True)
    kwargs.setdefault("pool_size", 10)
    kwargs.setdefault("max_overflow", 5)
    return create_async_engine(database_url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
