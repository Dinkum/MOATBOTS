from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base


@event.listens_for(Engine, "connect")
def configure_sqlite(dbapi_connection: object, connection_record: object) -> None:
    del connection_record
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=2500")
    cursor.close()


def ensure_data_directory() -> None:
    Path("data").mkdir(mode=0o700, parents=True, exist_ok=True)


def make_engine(database_url: str):
    if database_url.startswith("sqlite") and "mode=memory" not in database_url:
        ensure_data_directory()
    return create_async_engine(database_url, pool_pre_ping=True)


def make_session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts "
                "USING fts5(body, content='message', content_rowid='rowid')"
            )
        )
        await connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS message_fts_ai AFTER INSERT ON message BEGIN "
                "INSERT INTO message_fts(rowid, body) VALUES (new.rowid, new.body); END"
            )
        )
        await connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS message_fts_ad AFTER DELETE ON message BEGIN "
                "INSERT INTO message_fts(message_fts, rowid, body) "
                "VALUES ('delete', old.rowid, old.body); END"
            )
        )
        await connection.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS message_fts_au AFTER UPDATE OF body ON message BEGIN "
                "INSERT INTO message_fts(message_fts, rowid, body) "
                "VALUES ('delete', old.rowid, old.body); "
                "INSERT INTO message_fts(rowid, body) VALUES (new.rowid, new.body); END"
            )
        )
        # Partial unique index is declared on the model; create_all emits it on SQLite.
        await connection.execute(text("SELECT 1"))


async def get_db(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
