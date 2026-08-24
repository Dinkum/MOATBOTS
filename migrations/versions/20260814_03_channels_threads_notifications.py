"""Add channels, message threads, and durable notification queues."""

from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint, inspect, text

revision = "20260814_03"
down_revision = "20260814_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    membership_columns = {column["name"] for column in inspector.get_columns("membership")}
    if "notification_level" not in membership_columns:
        with op.batch_alter_table("membership") as batch:
            batch.add_column(
                Column("notification_level", String(16), nullable=False, server_default="mentions")
            )

    message_columns = {column["name"] for column in inspector.get_columns("message")}
    if "parent_message_id" not in message_columns:
        with op.batch_alter_table("message") as batch:
            batch.add_column(
                Column(
                    "parent_message_id",
                    String(32),
                    ForeignKey("message.id", name="fk_message_parent"),
                    nullable=True,
                )
            )
            batch.create_index("ix_message_parent_message_id", ["parent_message_id"])

    if "notification" not in inspector.get_table_names():
        op.create_table(
            "notification",
            Column("id", String(32), primary_key=True),
            Column(
                "agent_id", String(32), ForeignKey("agent.id", ondelete="CASCADE"), nullable=False
            ),
            Column(
                "room_id", String(32), ForeignKey("room.id", ondelete="CASCADE"), nullable=False
            ),
            Column(
                "message_id",
                String(32),
                ForeignKey("message.id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("kind", String(16), nullable=False),
            Column("wake_event_id", String(32), nullable=True),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("delivered_at", DateTime(timezone=True), nullable=True),
            Column("read_at", DateTime(timezone=True), nullable=True),
            UniqueConstraint("agent_id", "message_id", name="uq_notification_agent_message"),
        )
        op.create_index(
            "ix_notification_queue",
            "notification",
            ["agent_id", "read_at", "created_at"],
        )

    bind.execute(text("UPDATE room SET type = 'channel' WHERE type = 'topic'"))
    bind.execute(
        text(
            "UPDATE membership SET notification_level = 'all' "
            "WHERE room_id IN (SELECT id FROM room WHERE type IN ('dm', 'group'))"
        )
    )
    bind.execute(
        text(
            "UPDATE membership SET notification_level = 'all' "
            "WHERE subscriptions_json LIKE '%\"message\"%'"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    bind.execute(text("UPDATE room SET type = 'topic' WHERE type = 'channel'"))
    if "notification" in inspector.get_table_names():
        op.drop_table("notification")
    message_columns = {column["name"] for column in inspector.get_columns("message")}
    if "parent_message_id" in message_columns:
        with op.batch_alter_table("message") as batch:
            batch.drop_index("ix_message_parent_message_id")
            batch.drop_column("parent_message_id")
    membership_columns = {column["name"] for column in inspector.get_columns("membership")}
    if "notification_level" in membership_columns:
        with op.batch_alter_table("membership") as batch:
            batch.drop_column("notification_level")
