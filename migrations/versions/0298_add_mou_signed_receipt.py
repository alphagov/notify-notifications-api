"""

Revision ID: 0298_add_mou_signed_receipt
Revises: 0297_template_redacted_fix
Create Date: 2019-05-22 16:58:52.929661

"""

from alembic import op
from flask import current_app

revision = "0298_add_mou_signed_receipt"
down_revision = "0297_template_redacted_fix"


templates = [
    {
        "id": "4fd2e43c-309b-4e50-8fb8-1955852d9d71",
        "name": "MOU Signed By Receipt",
        "type": "email",
        "subject": "You’ve accepted the GOV.​UK Notify data sharing and financial agreement",
        "content_lines": [
            "Hi ((signed_by_name)),",
            "",
            "((org_name)) has accepted the GOV.​UK Notify data sharing and financial agreement. ",
            "",
            "If you need another copy of the agreement you can download it here: ((mou_link))",
            "",
            "Thanks,",
            "GOV.​UK Notify team",
            "",
            "https://www.gov.uk/notify",
        ],
    },
    {
        "id": "c20206d5-bf03-4002-9a90-37d5032d9e84",
        "name": "MOU Signed On Behalf Of Receipt - Signed by",
        "type": "email",
        "subject": "You’ve accepted the GOV.​UK Notify data sharing and financial agreement",
        "content_lines": [
            "Hi ((signed_by_name)),",
            "",
            "((org_name)) has accepted the GOV.​UK Notify data sharing and financial agreement. We’ve emailed ((on_behalf_of_name)) to let them know too.",
            "",
            "If you need another copy of the agreement you can download it here: ((mou_link))",
            "",
            "Thanks,",
            "GOV.​UK Notify team",
            "",
            "https://www.gov.uk/notify",
        ],
    },
    {
        "id": "522b6657-5ca5-4368-a294-6b527703bd0b",
        "name": "MOU Signed On Behalf Of Receipt - On Behalf Of",
        "type": "email",
        "subject": "((org_name)) has accepted the GOV.​UK Notify data sharing and financial agreement",
        "content_lines": [
            "Hi ((on_behalf_of_name)),",
            "",
            "((signed_by_name)) has accepted the GOV.​UK Notify data sharing and financial agreement on your behalf, for ((org_name)).",
            "",
            "GOV.​UK Notify lets teams in the public sector send emails, text messages and letters. It’s built and run by a team in the Government Digital Service (part of Cabinet Office).",
            "",
            "If you need another copy of the agreement you can download it here: ((mou_link))",
            "",
            "Thanks,",
            "GOV.​UK Notify team",
            "",
            "https://www.gov.uk/notify",
        ],
    },
    {
        "id": "d0e66c4c-0c50-43f0-94f5-f85b613202d4",
        "name": "MOU Signed Notify Team Alert",
        "type": "email",
        "subject": "Someone signed an MOU for an org on Notify",
        "content_lines": [
            "What’s up Notifiers,",
            "",
            "((signed_by_name)) just accepted the data sharing and financial agreement for ((org_name)).",
            "",
            "See how ((org_name)) is using Notify here: ((org_dashboard_link))",
        ],
    },
]


def upgrade():
    insert = """
        INSERT INTO {} (id, name, template_type, created_at, content, archived, service_id, subject,
        created_by_id, version, process_type, hidden)
        VALUES ('{}', '{}', '{}', current_timestamp, '{}', False, '{}', '{}', '{}', 1, '{}', false)
    """

    for template in templates:
        for table_name in "templates", "templates_history":
            op.execute(
                insert.format(
                    table_name,
                    template["id"],
                    template["name"],
                    template["type"],
                    "\n".join(template["content_lines"]),
                    current_app.config["NOTIFY_SERVICE_ID"],
                    template.get("subject"),
                    current_app.config["NOTIFY_USER_ID"],
                    "normal",
                )
            )

        op.execute(
            """
            INSERT INTO template_redacted
            (
                template_id,
                redact_personalisation,
                updated_at,
                updated_by_id
            ) VALUES ( '{}', false, current_timestamp, '{}' )
            """.format(
                template["id"], current_app.config["NOTIFY_USER_ID"]
            )
        )


def downgrade():
    for template in templates:
        op.execute("DELETE FROM notifications WHERE template_id = '{}'".format(template["id"]))
        op.execute("DELETE FROM notification_history WHERE template_id = '{}'".format(template["id"]))
        op.execute("DELETE FROM template_redacted WHERE template_id = '{}'".format(template["id"]))
        op.execute("DELETE FROM templates WHERE id = '{}'".format(template["id"]))
        op.execute("DELETE FROM templates_history WHERE id = '{}'".format(template["id"]))
