"""Link agent messages to the wake turn that produced them."""

from alembic import op
from sqlalchemy import Column, ForeignKey, String, inspect

revision = "20260814_05"
down_revision = "20260814_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("message")}
    if "source_wake_event_id" not in columns:
        with op.batch_alter_table("message") as batch:
            batch.add_column(
                Column(
                    "source_wake_event_id",
                    String(32),
                    ForeignKey(
                        "wake_event.id",
                        name="fk_message_source_wake_event_id",
                        ondelete="SET NULL",
                    ),
                    nullable=True,
                )
            )
            batch.create_index(
                "ix_message_source_wake_event_id",
                ["source_wake_event_id"],
                unique=False,
            )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("message")}
    if "source_wake_event_id" in columns:
        with op.batch_alter_table("message") as batch:
            batch.drop_index("ix_message_source_wake_event_id")
            batch.drop_column("source_wake_event_id")
