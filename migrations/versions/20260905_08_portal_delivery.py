"""Persist outbound delivery and deduplicate inbound portal updates."""

import sqlalchemy as sa
from alembic import op

revision = "20260905_08"
down_revision = "20260823_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "portal_delivery" not in tables:
        op.create_table(
            "portal_delivery",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "message_id",
                sa.String(32),
                sa.ForeignKey("message.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("portal_key", sa.String(80), nullable=False),
            sa.Column("recipient", sa.String(80), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("next_part", sa.Integer(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
            sa.Column("last_error", sa.String(240), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
        )
        op.create_index(
            "ix_portal_delivery_due", "portal_delivery", ["status", "portal_key", "next_attempt_at"]
        )
    if "portal_receipt" not in tables:
        op.create_table(
            "portal_receipt",
            sa.Column("id", sa.String(160), primary_key=True),
            sa.Column("peer", sa.String(80), nullable=False),
            sa.Column("received_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("portal_receipt")
    op.drop_table("portal_delivery")
