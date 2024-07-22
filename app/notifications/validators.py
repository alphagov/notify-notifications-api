from flask import current_app
from gds_metrics.metrics import Histogram
from notifications_utils import SMS_CHAR_COUNT_LIMIT
from notifications_utils.clients.redis import (
    daily_limit_cache_key,
    rate_limit_cache_key,
)
from notifications_utils.recipient_validation.email_address import validate_and_format_email_address
from notifications_utils.recipient_validation.phone_number import (
    get_international_phone_info,
    validate_and_format_phone_number,
)
from notifications_utils.recipient_validation.postal_address import PostalAddress
from sqlalchemy.orm.exc import NoResultFound

from app import redis_store
from app.constants import (
    EMAIL_TYPE,
    INTERNATIONAL_LETTERS,
    INTERNATIONAL_SMS_TYPE,
    KEY_TYPE_TEAM,
    KEY_TYPE_TEST,
    LETTER_TYPE,
    SMS_TYPE,
)
from app.dao.service_email_reply_to_dao import dao_get_reply_to_by_id
from app.dao.service_letter_contact_dao import dao_get_letter_contact_by_id
from app.dao.service_sms_sender_dao import dao_get_service_sms_senders_by_id
from app.models import ServicePermission
from app.notifications.process_notifications import (
    create_content_for_notification,
)
from app.serialised_models import SerialisedTemplate
from app.service.utils import service_allowed_to_send_to
from app.utils import get_public_notify_type_text
from app.v2.errors import (
    BadRequestError,
    RateLimitError,
    TooManyRequestsError,
    ValidationError,
)

REDIS_EXCEEDED_RATE_LIMIT_DURATION_SECONDS = Histogram(
    "redis_exceeded_rate_limit_duration_seconds",
    "Time taken to check rate limit",
)


def check_service_over_api_rate_limit(service, key_type):
    if current_app.config["API_RATE_LIMIT_ENABLED"] and current_app.config["REDIS_ENABLED"]:
        cache_key = rate_limit_cache_key(service.id, key_type)
        rate_limit = service.rate_limit
        interval = 60
        with REDIS_EXCEEDED_RATE_LIMIT_DURATION_SECONDS.time():
            if redis_store.exceeded_rate_limit(cache_key, rate_limit, interval):
                current_app.logger.info("service %s has been rate limited for throughput", service.id)
                raise RateLimitError(rate_limit, interval, key_type)


def check_service_over_daily_message_limit(service, key_type, notification_type, num_notifications=1):
    if key_type == KEY_TYPE_TEST or not current_app.config["REDIS_ENABLED"]:
        return

    rate_limits = {
        EMAIL_TYPE: service.email_message_limit,
        SMS_TYPE: service.sms_message_limit,
        LETTER_TYPE: service.letter_message_limit,
    }

    limit_name = notification_type
    limit_value = rate_limits[notification_type]

    cache_key = daily_limit_cache_key(service.id, notification_type=notification_type)
    if (service_stats := redis_store.get(cache_key)) is None:
        # first message of the day, set the cache to 0 and the expiry to 24 hours
        redis_store.set(cache_key, 0, ex=86400)

        service_stats = 0

    if int(service_stats) + num_notifications > limit_value:
        current_app.logger.info(
            "service %s has been rate limited for %s daily use sent %s limit %s",
            service.id,
            int(service_stats),
            limit_name,
            limit_value,
        )
        raise TooManyRequestsError(limit_name, limit_value)


def check_rate_limiting(service, api_key, notification_type):
    check_service_over_api_rate_limit(service, api_key.key_type)
    check_service_over_daily_message_limit(service, api_key.key_type, notification_type=notification_type)


def check_template_is_for_notification_type(notification_type, template_type):
    if notification_type != template_type:
        message = f"{template_type} template is not suitable for {notification_type} notification"
        raise BadRequestError(fields=[{"template": message}], message=message)


def check_template_is_active(template):
    if template.archived:
        raise BadRequestError(fields=[{"template": "Template has been deleted"}], message="Template has been deleted")


def service_can_send_to_recipient(send_to, key_type, service, allow_guest_list_recipients=True):
    if not service_allowed_to_send_to(send_to, service, key_type, allow_guest_list_recipients):
        if key_type == KEY_TYPE_TEAM:
            message = "Can’t send to this recipient using a team-only API key"
        else:
            message = (
                "Can’t send to this recipient when service is in trial mode "
                "– see https://www.notifications.service.gov.uk/trial-mode"
            )
        raise BadRequestError(message=message)


def check_service_has_permission(service, permission):
    if not service.has_permission(permission):
        raise BadRequestError(
            message=f"Service is not allowed to send {get_public_notify_type_text(permission, plural=True)}"
        )


def check_if_service_can_send_files_by_email(service_contact_link, service_id):
    if not service_contact_link:
        raise BadRequestError(
            message=f"Send files by email has not been set up - add contact details for your service at "
            f"{current_app.config['ADMIN_BASE_URL']}/services/{service_id}/service-settings/send-files-by-email"
        )


def validate_and_format_recipient(send_to, key_type, service, notification_type, allow_guest_list_recipients=True):
    if send_to is None:
        raise BadRequestError(message="Recipient can't be empty")

    service_can_send_to_recipient(send_to, key_type, service, allow_guest_list_recipients)

    if notification_type == SMS_TYPE:
        international_phone_info = check_if_service_can_send_to_number(service, send_to)

        return validate_and_format_phone_number(number=send_to, international=international_phone_info.international)
    elif notification_type == EMAIL_TYPE:
        return validate_and_format_email_address(email_address=send_to)


def check_if_service_can_send_to_number(service, number):
    international_phone_info = get_international_phone_info(number)

    if service.permissions and isinstance(service.permissions[0], ServicePermission):
        permissions = [p.permission for p in service.permissions]
    else:
        permissions = service.permissions

    if (
        # if number is international and not a crown dependency
        international_phone_info.international
        and not international_phone_info.crown_dependency
    ) and INTERNATIONAL_SMS_TYPE not in permissions:
        raise BadRequestError(message="Cannot send to international mobile numbers")
    else:
        return international_phone_info


def check_is_message_too_long(template_with_content):
    if template_with_content.is_message_too_long():
        message = "Your message is too long. "
        if template_with_content.template_type == SMS_TYPE:
            message += (
                f"Text messages cannot be longer than {SMS_CHAR_COUNT_LIMIT} characters. "
                f"Your message is {template_with_content.content_count_without_prefix} characters long."
            )
        elif template_with_content.template_type == EMAIL_TYPE:
            message += (
                f"Emails cannot be longer than 2000000 bytes. "
                f"Your message is {template_with_content.content_size_in_bytes} bytes."
            )
        raise BadRequestError(message=message)


def check_notification_content_is_not_empty(template_with_content):
    if template_with_content.is_message_empty():
        message = "Your message is empty."
        raise BadRequestError(message=message)


def validate_template(template_id, personalisation, service, notification_type, check_char_count=True):
    try:
        template = SerialisedTemplate.from_id_and_service_id(template_id, service.id)
    except NoResultFound as e:
        message = "Template not found"
        raise BadRequestError(message=message, fields=[{"template": message}]) from e

    check_template_is_for_notification_type(notification_type, template.template_type)
    check_template_is_active(template)

    template_with_content = create_content_for_notification(template, personalisation)

    check_notification_content_is_not_empty(template_with_content)

    # validating the template in post_notifications happens before the file is uploaded for doc download,
    # which means the length of the message can be exceeded because it's including the file.
    # The document download feature is only available through the api.
    if check_char_count:
        check_is_message_too_long(template_with_content)

    check_template_can_contain_documents(notification_type, personalisation)

    return template, template_with_content


def check_reply_to(service_id, reply_to_id, type_):
    if type_ == EMAIL_TYPE:
        return check_service_email_reply_to_id(service_id, reply_to_id, type_)
    elif type_ == SMS_TYPE:
        return check_service_sms_sender_id(service_id, reply_to_id, type_)
    elif type_ == LETTER_TYPE:
        return check_service_letter_contact_id(service_id, reply_to_id, type_)


def check_service_email_reply_to_id(service_id, reply_to_id, notification_type):
    if reply_to_id:
        try:
            return dao_get_reply_to_by_id(reply_to_id=reply_to_id, service_id=service_id).email_address
        except NoResultFound as e:
            message = f"email_reply_to_id {reply_to_id} does not exist in database for service id {service_id}"
            raise BadRequestError(message=message) from e


def check_service_sms_sender_id(service_id, sms_sender_id, notification_type):
    if sms_sender_id:
        try:
            return dao_get_service_sms_senders_by_id(service_id, sms_sender_id).sms_sender
        except NoResultFound as e:
            message = f"sms_sender_id {sms_sender_id} does not exist in database for service id {service_id}"
            raise BadRequestError(message=message) from e


def check_service_letter_contact_id(service_id, letter_contact_id, notification_type):
    if letter_contact_id:
        try:
            return dao_get_letter_contact_by_id(service_id, letter_contact_id).contact_block
        except NoResultFound as e:
            message = f"letter_contact_id {letter_contact_id} does not exist in database for service id {service_id}"
            raise BadRequestError(message=message) from e


def validate_address(service, letter_data):
    address = PostalAddress.from_personalisation(
        letter_data,
        allow_international_letters=(INTERNATIONAL_LETTERS in str(service.permissions)),
    )
    if not address.has_enough_lines:
        raise ValidationError(message=f"Address must be at least {PostalAddress.MIN_LINES} lines")
    if address.has_too_many_lines:
        raise ValidationError(message=f"Address must be no more than {PostalAddress.MAX_LINES} lines")
    if address.has_invalid_country_for_bfpo_address:
        raise ValidationError(message="The last line of a BFPO address must not be a country.")
    if not address.has_valid_last_line:
        if address.allow_international_letters:
            raise ValidationError(message="Last line of address must be a real UK postcode or another country")
        raise ValidationError(message="Must be a real UK postcode")
    if address.has_invalid_characters:
        raise ValidationError(
            message="Address lines must not start with any of the following characters: @ ( ) = [ ] ” \\ / , < >"
        )
    if address.international:
        return address.postage
    else:
        return None


def check_template_can_contain_documents(template_type, personalisation):
    if template_type != EMAIL_TYPE and any(
        isinstance(v, dict) and "file" in v for v in (personalisation or {}).values()
    ):
        raise BadRequestError(message="Can only send a file by email")
