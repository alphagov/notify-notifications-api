"""empty message

Revision ID: 0082_add_go_live_template
Revises: 0081_noti_status_as_enum
Create Date: 2017-05-10 16:06:04.070874

"""

# revision identifiers, used by Alembic.
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from flask import current_app

revision = "0082_add_go_live_template"
down_revision = "0081_noti_status_as_enum"

template_id = "618185c6-3636-49cd-b7d2-6f6f5eb3bdde"


def upgrade():
    template_insert = """
        INSERT INTO templates (id, name, template_type, created_at, content, archived, service_id, subject, created_by_id, version, process_type)
        VALUES ('{}', '{}', '{}', '{}', '{}', False, '{}', '{}', '{}', 1, '{}')
    """
    template_history_insert = """
        INSERT INTO templates_history (id, name, template_type, created_at, content, archived, service_id, subject, created_by_id, version, process_type)
        VALUES ('{}', '{}', '{}', '{}', '{}', False, '{}', '{}', '{}', 1, '{}')
    """

    template_content = """Dear ((name)),

The following GOV.​UK Notify service is now live:

^((service name))

This email includes important information about:

* things you need to do now
* what to do if you have a problem


---

#Things you need to do now

##If you send text messages or letters

You must send us a purchase order before you spend any money on text messages or letters.

[Find out how to raise a purchase order](https://www.notifications.service.gov.uk/pricing/how-to-pay)

##If you send emails

Check if you need to add unsubscribe links to your email templates.

[See our guidance for more information](https://www.notifications.service.gov.uk/using-notify/unsubscribe-links)

##If you use our API

You can now send messages to anyone by creating a live API key.

[See our API documentation for instructions](https://www.notifications.service.gov.uk/documentation)

---

#If you have a problem

##Before you contact the team

Subscribe to our status page to get email updates: https://status.notifications.service.gov.uk

If the status page shows a problem, we’re already working on a solution – you do not need to contact us.

##How to contact the team

[Use the support page to report a problem or ask a question](https://www.notifications.service.gov.uk/support).

If it’s an emergency we’ll reply within 30 minutes.

For everything else, we’ll reply by the end of the next working day.

Our working days are Monday to Friday, 9:30am to 5:30pm, excluding bank holidays.

##What counts as an emergency?

It’s only an emergency if you get:

* a ‘technical difficulties’ error when you try to send a message
* a 500 response code when you try to send messages using the API

##If you have an out-of-hours emergency

You should still [use the support page](https://www.notifications.service.gov.uk/support).

If you cannot use the support page, email:
ooh-gov-uk-notify-support@digital.cabinet-office.gov.uk

You must only use this email address for out-of-hours emergencies.

Do not share this email address with people outside your team.

---

Thanks

GOV.​UK Notify
https://www.gov.uk/notify
"""

    template_name = "Automated \"You''re now live\" message"
    template_subject = "((service name)) is now live on GOV.UK Notify"

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


def downgrade():
    op.execute("DELETE FROM notifications WHERE template_id = '{}'".format(template_id))
    op.execute("DELETE FROM notification_history WHERE template_id = '{}'".format(template_id))
    op.execute("DELETE FROM templates_history WHERE id = '{}'".format(template_id))
    op.execute("DELETE FROM templates WHERE id = '{}'".format(template_id))
