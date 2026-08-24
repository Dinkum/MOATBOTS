"""Create the schema and add durable organization identity fields."""

from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, String, Text, inspect, text

from app.models import Base

revision = "20260814_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "agent" not in inspector.get_table_names():
        Base.metadata.create_all(bind)
        return

    existing = {column["name"] for column in inspector.get_columns("agent")}
    additions = {"role_title", "job_description", "reports_to_id", "retired_at"} - existing
    if additions:
        with op.batch_alter_table("agent") as batch:
            if "role_title" in additions:
                batch.add_column(
                    Column("role_title", String(120), nullable=False, server_default="")
                )
            if "job_description" in additions:
                batch.add_column(
                    Column("job_description", Text(), nullable=False, server_default="")
                )
            if "reports_to_id" in additions:
                batch.add_column(
                    Column(
                        "reports_to_id",
                        String(32),
                        ForeignKey("agent.id", name="fk_agent_reports_to"),
                        nullable=True,
                    )
                )
            if "retired_at" in additions:
                batch.add_column(Column("retired_at", DateTime(timezone=True), nullable=True))

    chief_id = bind.execute(text("SELECT id FROM agent WHERE name = 'chief'")).scalar()
    if chief_id:
        bind.execute(
            text(
                "UPDATE agent SET role_title = 'Chief of Staff', "
                "job_description = :description WHERE id = :chief_id"
            ),
            {
                "chief_id": chief_id,
                "description": (
                    "Runs the organization, assigns work, and decides when the team needs a new "
                    "specialist. Reuses qualified teammates before making a durable hire."
                ),
            },
        )
        bind.execute(
            text(
                "UPDATE agent SET reports_to_id = :chief_id "
                "WHERE kind = 'hermes' AND id != :chief_id AND reports_to_id IS NULL"
            ),
            {"chief_id": chief_id},
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("agent")}
    with op.batch_alter_table("agent") as batch:
        for column in ("retired_at", "reports_to_id", "job_description", "role_title"):
            if column in existing:
                batch.drop_column(column)
