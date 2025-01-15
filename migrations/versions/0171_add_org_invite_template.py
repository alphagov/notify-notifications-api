"""

Revision ID: 0171_add_org_invite_template
Revises: 0170_hidden_non_nullable
Create Date: 2018-02-16 14:16:43.618062

"""

from datetime import datetime

from alembic import op
from flask import current_app

revision = "0171_add_org_invite_template"
down_revision = "0170_hidden_non_nullable"


template_id = "203566f0-d835-47c5-aa06-932439c86573"


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

    template_content = (
        "((user_name)) has invited you to see how ((organisation_name)) is using GOV.UK Notify.\n\n"
        "You’ll get an overview of:\n\n"
        "* all the live services in your organisation\n\n"
        "* how much each service is spending\n\n"
        "* which team members belong to each service\n\n"
        "You’ll also be able to invite other colleagues to see this information.\n\n"
        "Use this link to accept the invitation:\n\n"
        "^ ((url))\n\n"
        "This invitation will stop working at midnight tomorrow. This is to keep ‘((organisation_name))’ secure.\n\n"
        "Thanks\n\n"
        "GOV.​UK Notify\nhttps://www.gov.uk/notify"
    )

    template_name = "Notify organisation invitation email"
    template_subject = "See how ((organisation_name)) is using GOV.UK Notify"

    op.execute(
        template_history_insert.format(
            template_id,
            template_name,
            "email",
            datetime.utcnow(),
            template_content,
            current_app.config["NOTIFY_SERVICE_ID"],
            template_subject,
            current_app.config["NOTIFY_USER_ID"],
            "normal",
        )
    )

    op.execute(
        template_insert.format(
            template_id,
            template_name,
            "email",
            datetime.utcnow(),
            template_content,
            current_app.config["NOTIFY_SERVICE_ID"],
            template_subject,
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
    #     """.format(template_id, False, datetime.utcnow(), current_app.config['NOTIFY_USER_ID'])
    # )

    # clean up constraints on org_to_service - service_id-org_id constraint is redundant
    op.drop_constraint(
        "organisation_to_service_service_id_organisation_id_key", "organisation_to_service", type_="unique"
    )


def downgrade():
    op.execute("DELETE FROM notifications WHERE template_id = '{}'".format(template_id))
    op.execute("DELETE FROM notification_history WHERE template_id = '{}'".format(template_id))
    op.execute("DELETE FROM template_redacted WHERE template_id = '{}'".format(template_id))
    op.execute("DELETE FROM templates_history WHERE id = '{}'".format(template_id))
    op.execute("DELETE FROM templates WHERE id = '{}'".format(template_id))
    op.create_unique_constraint(
        "organisation_to_service_service_id_organisation_id_key",
        "organisation_to_service",
        ["service_id", "organisation_id"],
    )
