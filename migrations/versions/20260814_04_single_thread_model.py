"""Remove the unused standalone thread model and tighten notification ownership."""

from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, String, inspect

revision = "20260814_04"
down_revision = "20260814_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if "notification" in inspector.get_table_names():
        columns = {column["name"]: column for column in inspector.get_columns("notification")}
        if any(columns[name]["nullable"] for name in ("agent_id", "room_id", "message_id")):
            with op.batch_alter_table("notification") as batch:
                for name in ("agent_id", "room_id", "message_id"):
                    batch.alter_column(name, existing_type=String(32), nullable=False)

    inspector = inspect(op.get_bind())
    message_columns = {column["name"] for column in inspector.get_columns("message")}
    if "thread_id" in message_columns:
        with op.batch_alter_table("message") as batch:
            batch.drop_column("thread_id")
    if "thread" in inspect(op.get_bind()).get_table_names():
        op.drop_table("thread")


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if "thread" not in inspector.get_table_names():
        op.create_table(
            "thread",
            Column("id", String(32), primary_key=True),
            Column("room_id", String(32), ForeignKey("room.id", ondelete="CASCADE")),
            Column("title", String(180), nullable=False),
            Column("created_at", DateTime(timezone=True), nullable=False),
        )
    message_columns = {column["name"] for column in inspect(op.get_bind()).get_columns("message")}
    if "thread_id" not in message_columns:
        with op.batch_alter_table("message") as batch:
            batch.add_column(
                Column("thread_id", String(32), ForeignKey("thread.id"), nullable=True)
            )
