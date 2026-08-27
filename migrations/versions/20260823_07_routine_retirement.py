"""Soft-retire routines while preserving trigger history."""

import sqlalchemy as sa
from alembic import op

revision = "20260823_07"
down_revision = "20260823_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("routine")}
    if "retired_at" not in columns:
        with op.batch_alter_table("routine") as batch:
            batch.add_column(sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("routine")}
    if "retired_at" in columns:
        with op.batch_alter_table("routine") as batch:
            batch.drop_column("retired_at")
