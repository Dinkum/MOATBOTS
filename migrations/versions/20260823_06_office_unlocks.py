"""Add leased wakes, routines, human requests, artifacts, and demonstrations."""

import sqlalchemy as sa
from alembic import op

revision = "20260823_06"
down_revision = "20260814_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    wake_columns = {column["name"] for column in inspector.get_columns("wake_event")}
    wake_additions = {
        "execution_lane",
        "attempts",
        "lease_expires_at",
    } - wake_columns
    if wake_additions:
        with op.batch_alter_table("wake_event") as batch:
            if "execution_lane" in wake_additions:
                batch.add_column(
                    sa.Column(
                        "execution_lane", sa.String(16), nullable=False, server_default="shared"
                    )
                )
            if "attempts" in wake_additions:
                batch.add_column(
                    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
                )
            if "lease_expires_at" in wake_additions:
                batch.add_column(
                    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
                )
                batch.create_index("ix_wake_event_lease_expires_at", ["lease_expires_at"])
    routine_columns = {column["name"] for column in inspector.get_columns("routine")}
    routine_additions = {
        "trigger_type",
        "max_runs",
        "run_count",
        "expires_at",
        "updated_at",
    } - routine_columns
    if routine_additions:
        with op.batch_alter_table("routine") as batch:
            if "trigger_type" in routine_additions:
                batch.add_column(
                    sa.Column("trigger_type", sa.String(16), nullable=False, server_default="event")
                )
            if "max_runs" in routine_additions:
                batch.add_column(sa.Column("max_runs", sa.Integer(), nullable=True))
            if "run_count" in routine_additions:
                batch.add_column(
                    sa.Column("run_count", sa.Integer(), nullable=False, server_default="0")
                )
            if "expires_at" in routine_additions:
                batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
            if "updated_at" in routine_additions:
                batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE routine SET updated_at = created_at WHERE updated_at IS NULL")

    if "routine_run" not in tables:
        _create_routine_run()
    if "artifact" not in tables:
        _create_artifact()
    if "human_request" not in tables:
        _create_human_request()
    if "demonstration" not in tables:
        _create_demonstration()

    run_columns = {column["name"] for column in inspector.get_columns("run")}
    run_additions = {"provider", "model"} - run_columns
    with op.batch_alter_table("run") as batch:
        batch.alter_column("turns_used", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("cost_usd", existing_type=sa.Float(), nullable=True)
        if "provider" in run_additions:
            batch.add_column(
                sa.Column("provider", sa.String(80), nullable=False, server_default="")
            )
        if "model" in run_additions:
            batch.add_column(sa.Column("model", sa.String(160), nullable=False, server_default=""))

    _create_search()


def _create_routine_run() -> None:
    op.create_table(
        "routine_run",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "routine_id",
            sa.String(32),
            sa.ForeignKey("routine.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "wake_event_id",
            sa.String(32),
            sa.ForeignKey("wake_event.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_routine_run_routine_id", "routine_run", ["routine_id"])


def _create_artifact() -> None:
    op.create_table(
        "artifact",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "message_id",
            sa.String(32),
            sa.ForeignKey("message.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("label", sa.String(180), nullable=False),
        sa.Column("mime_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifact_message_id", "artifact", ["message_id"])


def _create_human_request() -> None:
    op.create_table(
        "human_request",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("agent_id", sa.String(32), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("room_id", sa.String(32), sa.ForeignKey("room.id"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("options_json", sa.Text(), nullable=False),
        sa.Column("secret_name", sa.String(80), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_human_request_agent_id", "human_request", ["agent_id"])
    op.create_index("ix_human_request_queue", "human_request", ["status", "created_at"])


def _create_demonstration() -> None:
    op.create_table(
        "demonstration",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("agent_id", sa.String(32), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("recording_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("learned_reference", sa.Text(), nullable=False),
        sa.Column("verification_note", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_demonstration_agent_id", "demonstration", ["agent_id"])


def _create_search() -> None:
    op.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS message_fts "
        "USING fts5(body, content='message', content_rowid='rowid')"
    )
    op.execute("INSERT INTO message_fts(rowid, body) SELECT rowid, body FROM message")
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS message_fts_ai AFTER INSERT ON message BEGIN "
        "INSERT INTO message_fts(rowid, body) VALUES (new.rowid, new.body); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS message_fts_ad AFTER DELETE ON message BEGIN "
        "INSERT INTO message_fts(message_fts, rowid, body) "
        "VALUES ('delete', old.rowid, old.body); END"
    )
    op.execute(
        "CREATE TRIGGER IF NOT EXISTS message_fts_au AFTER UPDATE OF body ON message BEGIN "
        "INSERT INTO message_fts(message_fts, rowid, body) "
        "VALUES ('delete', old.rowid, old.body); "
        "INSERT INTO message_fts(rowid, body) VALUES (new.rowid, new.body); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS message_fts_au")
    op.execute("DROP TRIGGER IF EXISTS message_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS message_fts_ai")
    op.execute("DROP TABLE IF EXISTS message_fts")
    with op.batch_alter_table("run") as batch:
        batch.drop_column("model")
        batch.drop_column("provider")
        batch.alter_column("cost_usd", existing_type=sa.Float(), nullable=False)
        batch.alter_column("turns_used", existing_type=sa.Integer(), nullable=False)
    op.drop_table("demonstration")
    op.drop_table("human_request")
    op.drop_table("artifact")
    op.drop_table("routine_run")
    with op.batch_alter_table("routine") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("expires_at")
        batch.drop_column("run_count")
        batch.drop_column("max_runs")
        batch.drop_column("trigger_type")
    with op.batch_alter_table("wake_event") as batch:
        batch.drop_index("ix_wake_event_lease_expires_at")
        batch.drop_column("lease_expires_at")
        batch.drop_column("attempts")
        batch.drop_column("execution_lane")
