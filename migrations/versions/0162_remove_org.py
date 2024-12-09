"""

Revision ID: 0162_remove_org
Revises: 0161_email_branding
Create Date: 2018-02-06 17:08:11.879844

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0162_remove_org"
down_revision = "0161_email_branding"


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("services", "organisation_id")
    op.drop_column("services_history", "organisation_id")

    op.drop_table("organisation")

    op.alter_column("service_email_branding", "email_branding_id", nullable=False)
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "services_history", sa.Column("organisation_id", postgresql.UUID(), autoincrement=False, nullable=True)
    )
    op.add_column("services", sa.Column("organisation_id", postgresql.UUID(), autoincrement=False, nullable=True))

    op.create_table(
        "organisation",
        sa.Column("id", postgresql.UUID(), autoincrement=False, nullable=False),
        sa.Column("colour", sa.VARCHAR(length=7), autoincrement=False, nullable=True),
        sa.Column("logo", sa.VARCHAR(length=255), autoincrement=False, nullable=True),
        sa.Column("name", sa.VARCHAR(length=255), autoincrement=False, nullable=True),
        sa.PrimaryKeyConstraint("id", name="organisation_pkey"),
    )

    op.create_index("ix_services_history_organisation_id", "services_history", ["organisation_id"], unique=False)
    op.create_foreign_key("services_organisation_id_fkey", "services", "organisation", ["organisation_id"], ["id"])
    op.create_index("ix_services_organisation_id", "services", ["organisation_id"], unique=False)

    op.alter_column("service_email_branding", "email_branding_id", nullable=True)
