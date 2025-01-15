"""

Revision ID: 0294_add_verify_reply_to
Revises: 0293_drop_complaint_fk
Create Date: 2019-05-22 16:58:52.929661

"""

from datetime import datetime

from alembic import op
from flask import current_app

revision = "0294_add_verify_reply_to"
down_revision = "0293_drop_complaint_fk"

email_template_id = "a42f1d17-9404-46d5-a647-d013bdfca3e1"


def upgrade():
    template_insert = """
        INSERT INTO templates (id, name, template_type, created_at, content, archived, service_id, subject,
        created_by_id, version, process_type, hidden)
        VALUES ('{}', '{}', '{}', '{}', '{}', False, '{}', '{}', '{}', 1, '{}', false)
    """
    template_history_insert = """
        INSERT INTO templates_history (id, name, template_type, created_at, content, archived, service_id, subject,
        created_by_id, version, process_type, hidden)
        VALUES ('{}', '{}', '{}', '{}', '{}', False, '{}', '{}', '{}', 1, '{}', false)
    """

    email_template_content = (
        "This address has been added as a reply-to email address for a GOV.​UK Notify service.\n\n"
        "Any replies to emails sent from that Notify service will come to this address.\n\n"
        "We sent this email to check the address is valid.\n\n"
        "You do not need to reply.\n\n"
        "Thanks\n\n"
        "GOV.​UK Notify\nhttps://www.gov.uk/notify"
    )

    email_template_name = "Verify email reply-to address for a service"
    email_template_subject = "Your GOV.UK Notify reply-to email address"

    op.execute(
        template_history_insert.format(
            email_template_id,
            email_template_name,
            "email",
            datetime.utcnow(),
            email_template_content,
            current_app.config["NOTIFY_SERVICE_ID"],
            email_template_subject,
            current_app.config["NOTIFY_USER_ID"],
            "normal",
        )
    )

    op.execute(
        template_insert.format(
            email_template_id,
            email_template_name,
            "email",
            datetime.utcnow(),
            email_template_content,
            current_app.config["NOTIFY_SERVICE_ID"],
            email_template_subject,
            current_app.config["NOTIFY_USER_ID"],
            "normal",
        )
    )


# If you are copying this migration, please remember about an insert to TemplateRedacted,
# which was not originally included here either by mistake or because it was before TemplateRedacted existed
# op.execute(
#     """
#         INSERT INTO template_redacted (template_id, redact_personalisation, updated_at, updated_by_id)
#         VALUES ('{}', '{}', '{}', '{}')
#         ;
#     """.format(email_template_id, False, datetime.utcnow(), current_app.config['NOTIFY_USER_ID'])
# )


def downgrade():
    op.execute("DELETE FROM notifications WHERE template_id = '{}'".format(email_template_id))
    op.execute("DELETE FROM notification_history WHERE template_id = '{}'".format(email_template_id))
    op.execute("DELETE FROM template_redacted WHERE template_id = '{}'".format(email_template_id))
    op.execute("DELETE FROM templates_history WHERE id = '{}'".format(email_template_id))
    op.execute("DELETE FROM templates WHERE id = '{}'".format(email_template_id))
