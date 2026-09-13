import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.models import Base


@pytest.mark.parametrize("existing", [False, True])
def test_portal_migration_preserves_old_messages_and_converges_on_fresh_db(tmp_path, existing):
    database = tmp_path / "migration.db"
    engine = create_engine(f"sqlite:///{database}")
    if existing:
        # Materialize the prior schema without either new delivery table.
        tables = [
            table
            for table in Base.metadata.sorted_tables
            if table.name not in {"portal_delivery", "portal_receipt"}
        ]
        Base.metadata.create_all(engine, tables=tables)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent (id,name,kind,role_title,job_description,"
                    "capabilities_json,status,created_at) VALUES "
                    "('chief','chief','hermes','','','[]','idle','2026-09-01 12:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO room (id,type,title,objective,lifecycle,created_at) VALUES "
                    "('room','dm','preserved','','open','2026-09-01 12:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO message (id,room_id,sender_id,body,mentions_json,created_at) "
                    "VALUES ('msg','room','chief','preserved message','[]','2026-09-01 12:00:00')"
                )
            )
    environment = {**os.environ, "MOATBOTS_DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    root = Path(__file__).parents[1]

    def alembic(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    if existing:
        alembic("stamp", "20260823_07")
    alembic("upgrade", "head")
    alembic("upgrade", "head")
    inspector = inspect(engine)
    assert {"portal_delivery", "portal_receipt"} <= set(inspector.get_table_names())
    assert any(
        index["name"] == "ix_portal_delivery_due"
        for index in inspector.get_indexes("portal_delivery")
    )
    assert inspector.get_foreign_keys("portal_delivery")[0]["referred_table"] == "message"
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20260905_08"
        assert connection.scalar(text("PRAGMA integrity_check")) == "ok"
        if existing:
            assert (
                connection.scalar(text("SELECT body FROM message WHERE id='msg'"))
                == "preserved message"
            )
    engine.dispose()
