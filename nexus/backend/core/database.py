"""
数据库连接管理
基于 SQLAlchemy 2.0 + aiosqlite（开发）/ asyncpg（生产）
"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from sqlalchemy import event, text
from sqlalchemy.orm import Session
import logging

from .config import settings

logger = logging.getLogger(__name__)


@event.listens_for(Session, "after_rollback")
def discard_rolled_back_anchors(session):
    session.info.pop("evidence_anchors", None)


class Base(DeclarativeBase):
    """所有 ORM 模型基类"""
    pass


# ============ 引擎 ============
_engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    pool_pre_ping=True,
    future=True,
)

if _engine.dialect.name == "sqlite":
    @event.listens_for(_engine.sync_engine, "connect")
    def configure_sqlite(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

# ============ 会话工厂 ============
_async_session_factory = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：获取数据库会话"""
    async with session_scope() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """独立上下文：获取数据库会话（用于非 FastAPI 场景）"""
    async with _async_session_factory() as session:
        try:
            # The demo uses one SQLite writer at a time, including the reads
            # preceding a money mutation. This also protects concurrent retries.
            if _engine.dialect.name == "sqlite":
                await session.execute(text("BEGIN IMMEDIATE"))
            yield session
            await session.commit()
            if session.info.get("evidence_anchors"):
                from ..services.evidence_service import publish_anchors
                try:
                    publish_anchors(session)
                except OSError:
                    # The business commit already happened. Never report a
                    # failed payment or retry it because checkpoint I/O failed.
                    # Verification explicitly reports an unanchored chain.
                    logger.exception("审计独立锚点写入失败；已提交业务不得重试执行")
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """初始化数据库表（仅开发模式）"""
    # 导入所有模型
    from .models import register_all_models
    register_all_models()

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(connection) -> None:
    """Give an existing SQLite file any columns added to a model since it was made.

    ``create_all`` creates missing *tables* but never touches one that already
    exists, so a newly added field would otherwise leave every older demo
    database querying a column that isn't there. Only additive changes are
    applied — a column may be added with a default, but nothing is dropped or
    retyped, because the data behind it is the customer's financial record.

    Real deployments run a migration tool; this project does not, so this stays
    deliberately small and idempotent.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # just created by create_all, already complete
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {column.type.compile(connection.dialect)}'
            if not column.nullable and column.default is None:
                # SQLite refuses NOT NULL without a default on an existing table.
                ddl += " NOT NULL DEFAULT 0"
            connection.execute(text(ddl))
            logger.info("已为 %s 补充字段 %s", table.name, column.name)


async def close_db() -> None:
    """关闭数据库连接"""
    await _engine.dispose()
