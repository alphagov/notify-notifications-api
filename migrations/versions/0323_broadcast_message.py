"""

Revision ID: 0323_broadcast_message
Revises: 0322_broadcast_service_perm
Create Date: 2020-07-02 11:59:38.734650

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import column, func

revision = "0323_broadcast_message"
down_revision = "0322_broadcast_service_perm"


name = "template_type"
tmp_name = "tmp_" + name

old_options = ("sms", "email", "letter")
new_options = old_options + ("broadcast",)

new_type = sa.Enum(*new_options, name=name)
old_type = sa.Enum(*old_options, name=name)


STATUSES = [
    "draft",
    "pending-approval",
    "rejected",
    "broadcasting",
    "completed",
    "cancelled",
    "technical-failure",
]


def upgrade():
    op.execute(f"ALTER TYPE {name} RENAME TO {tmp_name}")
    new_type.create(op.get_bind())

    for table in ["templates", "templates_history", "service_contact_list"]:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN template_type TYPE {name} USING template_type::text::{name}")

    op.execute(f"DROP TYPE {tmp_name}")

    broadcast_status_type = op.create_table(
        "broadcast_status_type", sa.Column("name", sa.String(), nullable=False), sa.PrimaryKeyConstraint("name")
    )
    op.bulk_insert(broadcast_status_type, [{"name": state} for state in STATUSES])

    op.create_table(
        "broadcast_message",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True)),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("_personalisation", sa.String()),
        sa.Column("areas", postgresql.JSONB(none_as_null=True, astext_type=sa.Text())),
        sa.Column("status", sa.String()),
        sa.Column("starts_at", sa.DateTime()),
        sa.Column("finishes_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("approved_at", sa.DateTime()),
        sa.Column("cancelled_at", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime()),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True)),
        sa.Column("cancelled_by_id", postgresql.UUID(as_uuid=True)),
        sa.ForeignKeyConstraint(
            ["approved_by_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["cancelled_by_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["service_id"],
            ["services.id"],
        ),
        sa.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["templates_history.id", "templates_history.version"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column("templates", sa.Column("broadcast_data", postgresql.JSONB(none_as_null=True, astext_type=sa.Text())))
    op.add_column(
        "templates_history", sa.Column("broadcast_data", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()))
    )


def downgrade():
    op.execute(
        "DELETE FROM template_folder_map WHERE template_id IN (SELECT id FROM templates WHERE template_type = 'broadcast')"
    )
    op.execute(
        "DELETE FROM template_redacted WHERE template_id IN (SELECT id FROM templates WHERE template_type = 'broadcast')"
    )
    op.execute("DELETE FROM templates WHERE template_type = 'broadcast'")
    op.execute("DELETE FROM templates_history WHERE template_type = 'broadcast'")

    op.execute(f"ALTER TYPE {name} RENAME TO {tmp_name}")
    old_type.create(op.get_bind())

    for table in ["templates", "templates_history", "service_contact_list"]:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN template_type TYPE {name} USING template_type::text::{name}")
    op.execute(f"DROP TYPE {tmp_name}")

    op.drop_column("templates_history", "broadcast_data")
    op.drop_column("templates", "broadcast_data")
    op.drop_table("broadcast_message")
    op.drop_table("broadcast_status_type")
