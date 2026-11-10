"""Async database access."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base


class Database:
    def __init__(self, url: str) -> None:
        options: dict[str, object] = {}
        if not url.startswith("sqlite"):
            options["pool_pre_ping"] = True  # drop dead connections after DB restarts
        self.engine: AsyncEngine = create_async_engine(url, **options)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def create_all(self) -> None:
        """Create tables if missing. Fine for a demo; use Alembic migrations in real deployments."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
