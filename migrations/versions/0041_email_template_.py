"""empty message

Revision ID: 0041_email_template
Revises: 0040_adjust_mmg_provider_rate
Create Date: 2016-07-07 16:02:06.241769

"""

# revision identifiers, used by Alembic.
from datetime import datetime

revision = "0041_email_template"
down_revision = "0040_adjust_mmg_provider_rate"

from alembic import op

user_id = "6af522d0-2915-4e52-83a3-3690455a5fe6"
service_id = "d6aa2c68-a2d9-4437-ab19-3ae8eb202553"


def upgrade():
    template_history_insert = """INSERT INTO templates_history (id, name, template_type, created_at,
                                                                content, archived, service_id,
                                                                subject, created_by_id, version)
                                 VALUES ('{}', '{}', '{}', '{}', '{}', False, '{}', '{}', '{}', 1)
                              """
    template_insert = """INSERT INTO templates (id, name, template_type, created_at,
                                                content, archived, service_id, subject, created_by_id, version)
                                 VALUES ('{}', '{}', '{}', '{}', '{}', False, '{}', '{}', '{}', 1)
                              """
    content = (
        "You already have a GOV.UK Notify account for this email address.\n\n"
        "Sign in to Notify at:\n\n((signin_url))\n\n"
        "If you’ve forgotten your password, you can reset it at:\n\n((forgot_password_url))\n\n"
        "If you did not try to register for a GOV.​UK Notify account recently, please let us know at:\n\n"
        "((feedback_url))\n\nThanks\n\n"
        "GOV.​UK Notify\nhttps://www.gov.uk/notify"
    )

    op.get_bind()
    op.execute(
        template_history_insert.format(
            "0880fbb1-a0c6-46f0-9a8e-36c986381ceb",
            "Your GOV.UK Notify account",
            "email",
            datetime.utcnow(),
            content,
            service_id,
            "Your GOV.UK Notify account",
            user_id,
        )
    )
    op.execute(
        template_insert.format(
            "0880fbb1-a0c6-46f0-9a8e-36c986381ceb",
            "Your GOV.UK Notify account",
            "email",
            datetime.utcnow(),
            content,
            service_id,
            "Your GOV.UK Notify account",
            user_id,
        )
    )


# If you are copying this migration, please remember about an insert to TemplateRedacted,
# which was not originally included here either by mistake or because it was before TemplateRedacted existed
# op.execute(
#     """
#         INSERT INTO template_redacted (template_id, redact_personalisation, updated_at, updated_by_id)
#         VALUES ('0880fbb1-a0c6-46f0-9a8e-36c986381ceb', '{}', '{}', '{}')
#         ;
#     """.format(False, datetime.utcnow(), user_id)
# )


def downgrade():
    op.execute("delete from notifications where template_id = '0880fbb1-a0c6-46f0-9a8e-36c986381ceb'")
    op.execute("delete from jobs where template_id = '0880fbb1-a0c6-46f0-9a8e-36c986381ceb'")
    op.execute("delete from template_statistics where template_id = '0880fbb1-a0c6-46f0-9a8e-36c986381ceb'")
    op.execute("delete from templates_history where id = '0880fbb1-a0c6-46f0-9a8e-36c986381ceb'")
    op.execute("delete from templates where id = '0880fbb1-a0c6-46f0-9a8e-36c986381ceb'")
