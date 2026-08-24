"""Remove the discarded temporary-agent concept."""

from alembic import op
from sqlalchemy import inspect

revision = "20260814_02"
down_revision = "20260814_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("agent")}
    if "ephemeral" in existing:
        with op.batch_alter_table("agent") as batch:
            batch.drop_column("ephemeral")


def downgrade() -> None:
    from sqlalchemy import Column, Integer

    existing = {column["name"] for column in inspect(op.get_bind()).get_columns("agent")}
    if "ephemeral" not in existing:
        with op.batch_alter_table("agent") as batch:
            batch.add_column(Column("ephemeral", Integer(), nullable=False, server_default="0"))
