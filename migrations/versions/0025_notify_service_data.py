"""empty message

Revision ID: 0025_notify_service_data
Revises: 0024_add_research_mode_defaults
Create Date: 2016-06-01 14:17:01.963181

"""

import uuid

# revision identifiers, used by Alembic.
from datetime import datetime

from alembic import op

from app.hashing import hashpw

revision = "0025_notify_service_data"
down_revision = "0024_add_research_mode_defaults"


user_id = "6af522d0-2915-4e52-83a3-3690455a5fe6"
service_id = "d6aa2c68-a2d9-4437-ab19-3ae8eb202553"


def upgrade():
    password = hashpw(str(uuid.uuid4()))
    op.get_bind()
    user_insert = """INSERT INTO users (id, name, email_address, created_at, failed_login_count, _password, mobile_number, state, platform_admin)
                     VALUES ('{}', 'Notify service user', 'notify-service-user@digital.cabinet-office', '{}', 0,'{}', '+441234123412', 'active', False)
                  """
    op.execute(user_insert.format(user_id, datetime.utcnow(), password))
    service_history_insert = """INSERT INTO services_history (id, name, created_at, active, message_limit, restricted, research_mode, email_from, created_by_id, reply_to_email_address, version)
                        VALUES ('{}', 'Notify service', '{}', True, 1000, False, False, 'notify@digital.cabinet-office.gov.uk',
                        '{}', 'notify@digital.cabinet-office.gov.uk', 1)

                     """
    op.execute(service_history_insert.format(service_id, datetime.utcnow(), user_id))
    service_insert = """INSERT INTO services (id, name, created_at, active, message_limit, restricted, research_mode, email_from, created_by_id, reply_to_email_address, version)
                        VALUES ('{}', 'Notify service', '{}', True, 1000, False, False, 'notify@digital.cabinet-office.gov.uk',
                        '{}', 'notify@digital.cabinet-office.gov.uk', 1)
                    """
    op.execute(service_insert.format(service_id, datetime.utcnow(), user_id))
    user_to_service_insert = """INSERT INTO user_to_service (user_id, service_id) VALUES ('{}', '{}')"""
    op.execute(user_to_service_insert.format(user_id, service_id))

    template_history_insert = """INSERT INTO templates_history (id, name, template_type, created_at,
                                                                content, archived, service_id,
                                                                subject, created_by_id, version)
                                 VALUES ('{}', '{}', '{}', '{}', '{}', False, '{}', '{}', '{}', 1)
                              """
    template_insert = """INSERT INTO templates (id, name, template_type, created_at,
                                                content, archived, service_id, subject, created_by_id, version)
                                 VALUES ('{}', '{}', '{}', '{}', '{}', False, '{}', '{}', '{}', 1)
                              """
    email_verification_content = (
        "Dear ((name)),\n\n"
        "Complete your GOV.UK Notify registration at:\n\n((url))\n\n"
        "Thanks\n\nGOV.​UK Notify\nhttps://www.gov.uk/notify"
    )
    op.execute(
        template_history_insert.format(
            uuid.uuid4(),
            "Notify email verification code",
            "email",
            datetime.utcnow(),
            email_verification_content,
            service_id,
            "Complete your GOV.UK Notify registration",
            user_id,
        )
    )
    op.execute(
        template_insert.format(
            "ece42649-22a8-4d06-b87f-d52d5d3f0a27",
            "Notify email verification code",
            "email",
            datetime.utcnow(),
            email_verification_content,
            service_id,
            "Complete your GOV.UK Notify registration",
            user_id,
        )
    )

    invitation_subject = "((user_name)) has invited you to join ((service_name)) on GOV.UK Notify"
    invitation_content = (
        "((user_name)) has invited you to join the following GOV.UK Notify service:\n\n^((service_name))\n\n"
        "GOV.UK Notify makes it easy to keep people updated by helping you send text messages, emails and letters.\n\n"
        "Use this link to join the team:\n\n((url))\n\n"
        "This invitation will stop working at midnight tomorrow. This is to keep ‘((service_name))’ secure.\n\nThanks\n\n"
        "GOV.​UK Notify\nhttps://www.gov.uk/notify"
    )
    op.execute(
        template_history_insert.format(
            "4f46df42-f795-4cc4-83bb-65ca312f49cc",
            "Notify invitation email",
            "email",
            datetime.utcnow(),
            invitation_content,
            service_id,
            invitation_subject,
            user_id,
        )
    )
    op.execute(
        template_insert.format(
            "4f46df42-f795-4cc4-83bb-65ca312f49cc",
            "Notify invitation email",
            "email",
            datetime.utcnow(),
            invitation_content,
            service_id,
            invitation_subject,
            user_id,
        )
    )

    sms_code_content = "((verify_code)) is your Notify security code."
    op.execute(
        template_history_insert.format(
            "36fb0730-6259-4da1-8a80-c8de22ad4246",
            "Notify SMS verify code",
            "sms",
            datetime.utcnow(),
            sms_code_content,
            service_id,
            None,
            user_id,
        )
    )

    op.execute(
        template_insert.format(
            "36fb0730-6259-4da1-8a80-c8de22ad4246",
            "Notify SMS verify code",
            "sms",
            datetime.utcnow(),
            sms_code_content,
            service_id,
            None,
            user_id,
        )
    )

    password_reset_content = (
        "Dear ((user_name)),\n\n"
        "We received a request to reset your password on GOV.UK Notify.\n\n"
        "Reset your password at:\n\n"
        "((url))\n\n"
        "If you did not request this email, you can ignore it – your password has not been changed.\n\n"
        "Thanks\n\n"
        "GOV.​UK Notify\nhttps://www.gov.uk/notify"
    )

    op.execute(
        template_history_insert.format(
            "474e9242-823b-4f99-813d-ed392e7f1201",
            "Notify password reset email",
            "email",
            datetime.utcnow(),
            password_reset_content,
            service_id,
            "Reset your GOV.UK Notify password",
            user_id,
        )
    )
    op.execute(
        template_insert.format(
            "474e9242-823b-4f99-813d-ed392e7f1201",
            "Notify password reset email",
            "email",
            datetime.utcnow(),
            password_reset_content,
            service_id,
            "Reset your GOV.UK Notify password",
            user_id,
        )
    )


def downgrade():
    op.get_bind()
    op.execute("delete from templates where service_id = '{}'".format(service_id))
    op.execute("delete from templates_history where service_id = '{}'".format(service_id))
    op.execute("delete from user_to_service where service_id = '{}'".format(service_id))
    op.execute("delete from services_history where id = '{}'".format(service_id))
    op.execute("delete from services where id = '{}'".format(service_id))
    op.execute("delete from users where id = '{}'".format(user_id))
