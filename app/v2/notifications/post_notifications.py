import base64
import functools
import uuid
from datetime import datetime

from flask import abort, current_app, jsonify, request
from gds_metrics import Histogram
from notifications_utils.recipient_validation.phone_number import try_validate_and_format_phone_number

from app import (
    api_user,
    authenticated_service,
    document_download_client,
    notify_celery,
)
from app.celery.letters_pdf_tasks import (
    get_pdf_for_templated_letter,
    sanitise_letter,
)
from app.celery.research_mode_tasks import create_fake_letter_callback
from app.clients.document_download import DocumentDownloadError
from app.config import QueueNames, TaskNames
from app.constants import (
    DEFAULT_DOCUMENT_DOWNLOAD_RETENTION_PERIOD,
    EMAIL_TYPE,
    KEY_TYPE_TEAM,
    KEY_TYPE_TEST,
    LETTER_TYPE,
    NOTIFICATION_CREATED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_PENDING_VIRUS_CHECK,
    NOTIFICATION_SENDING,
    SMS_TYPE,
)
from app.dao.dao_utils import transaction
from app.dao.templates_dao import get_precompiled_letter_template
from app.letters.utils import upload_letter_pdf
from app.notifications.process_letter_notifications import (
    create_letter_notification,
)
from app.notifications.process_notifications import (
    persist_notification,
    send_notification_to_queue_detached,
    simulated_recipient,
)
from app.notifications.validators import (
    check_if_service_can_send_files_by_email,
    check_is_message_too_long,
    check_rate_limiting,
    check_service_email_reply_to_id,
    check_service_has_permission,
    check_service_sms_sender_id,
    validate_address,
    validate_and_format_recipient,
    validate_template,
)
from app.schema_validation import validate
from app.v2.errors import BadRequestError
from app.v2.notifications import v2_notification_blueprint
from app.v2.notifications.create_response import (
    create_post_email_response_from_notification,
    create_post_letter_response_from_notification,
    create_post_sms_response_from_notification,
)
from app.v2.notifications.notification_schemas import (
    post_email_request,
    post_letter_request,
    post_precompiled_letter_request,
    post_sms_request,
    send_a_file_validation,
)
from app.v2.utils import get_valid_json

POST_NOTIFICATION_JSON_PARSE_DURATION_SECONDS = Histogram(
    "post_notification_json_parse_duration_seconds",
    "Time taken to parse and validate post request json",
)


@v2_notification_blueprint.route(f"/{LETTER_TYPE}", methods=["POST"])
def post_precompiled_letter_notification():
    request_json = get_valid_json()
    if "content" not in (request_json or {}):
        return post_notification(LETTER_TYPE)

    form = validate(request_json, post_precompiled_letter_request)

    check_service_has_permission(authenticated_service, LETTER_TYPE)

    check_rate_limiting(authenticated_service, api_user, notification_type=LETTER_TYPE)

    template = get_precompiled_letter_template(authenticated_service.id)

    # For precompiled letters the to field will be set to Provided as PDF until the validation passes,
    # then the address of the letter will be set as the to field
    form["personalisation"] = {"address_line_1": "Provided as PDF"}

    notification = process_letter_notification(
        letter_data=form,
        api_key=api_user,
        service=authenticated_service,
        template=template,
        template_with_content=None,  # not required for precompiled
        reply_to_text="",  # not required for precompiled
        precompiled=True,
    )

    return jsonify(notification), 201


@v2_notification_blueprint.route("/<notification_type>", methods=["POST"])
def post_notification(notification_type):
    with POST_NOTIFICATION_JSON_PARSE_DURATION_SECONDS.time():
        request_json = get_valid_json()

        if notification_type == EMAIL_TYPE:
            form = validate(request_json, post_email_request)
        elif notification_type == SMS_TYPE:
            form = validate(request_json, post_sms_request)
        elif notification_type == LETTER_TYPE:
            form = validate(request_json, post_letter_request)
        else:
            abort(404)

    check_service_has_permission(authenticated_service, notification_type)

    check_rate_limiting(authenticated_service, api_user, notification_type=notification_type)

    template, template_with_content = validate_template(
        form["template_id"],
        form.get("personalisation", {}),
        authenticated_service,
        notification_type,
        check_char_count=False,
    )

    reply_to = get_reply_to_text(notification_type, form, template)

    if notification_type == LETTER_TYPE:
        notification = process_letter_notification(
            letter_data=form,
            api_key=api_user,
            service=authenticated_service,
            template=template,
            template_with_content=template_with_content,
            reply_to_text=reply_to,
        )
    else:
        notification = process_sms_or_email_notification(
            form=form,
            notification_type=notification_type,
            template=template,
            template_with_content=template_with_content,
            template_process_type=template.process_type,
            service=authenticated_service,
            reply_to_text=reply_to,
            unsubscribe_link=form.get("one_click_unsubscribe_url", None),
        )

    return jsonify(notification), 201


def process_sms_or_email_notification(
    *,
    form,
    notification_type,
    template,
    template_with_content,
    template_process_type,
    service,
    reply_to_text=None,
    unsubscribe_link=None,
):
    notification_id = uuid.uuid4()
    form_send_to = form["email_address"] if notification_type == EMAIL_TYPE else form["phone_number"]

    send_to = validate_and_format_recipient(
        send_to=form_send_to, key_type=api_user.key_type, service=service, notification_type=notification_type
    )

    # Do not persist or send notification to the queue if it is a simulated recipient
    simulated = simulated_recipient(send_to, notification_type)

    personalisation, document_download_count = process_document_uploads(
        form.get("personalisation"),
        service,
        send_to=send_to,
        simulated=simulated,
    )
    if document_download_count:
        # We changed personalisation which means we need to update the content
        template_with_content.values = personalisation

    # validate content length after url is replaced in personalisation.
    check_is_message_too_long(template_with_content)

    response = create_response_for_post_notification(
        notification_id=notification_id,
        client_reference=form.get("reference", None),
        template_id=template.id,
        template_version=template.version,
        service_id=service.id,
        notification_type=notification_type,
        reply_to=reply_to_text,
        unsubscribe_link=unsubscribe_link,
        template_with_content=template_with_content,
    )

    persist_notification(
        notification_id=notification_id,
        template_id=template.id,
        template_version=template.version,
        recipient=form_send_to,
        service=service,
        personalisation=personalisation,
        notification_type=notification_type,
        api_key_id=api_user.id,
        key_type=api_user.key_type,
        client_reference=form.get("reference", None),
        simulated=simulated,
        reply_to_text=reply_to_text,
        unsubscribe_link=unsubscribe_link,
        document_download_count=document_download_count,
    )

    if not simulated:
        send_notification_to_queue_detached(
            key_type=api_user.key_type,
            notification_type=notification_type,
            notification_id=notification_id,
        )
    else:
        current_app.logger.debug("POST simulated notification for id: %s", notification_id)

    return response


def process_document_uploads(personalisation_data, service, send_to: str, simulated=False):
    """
    Returns modified personalisation dict and a count of document uploads. If there are no document uploads, returns
    a count of `None` rather than `0`.
    """
    file_keys = [k for k, v in (personalisation_data or {}).items() if isinstance(v, dict) and "file" in v]
    if not file_keys:
        return personalisation_data, None

    # Make sure that all data for file uploads matches our expected schema.
    # We can't (feasibly) do this at the start of the request because the JSON Schema required would throw error
    # messages which aren't user-friendly (without deeply introspecting the JSON Schema validation results in a way
    # that is worse than doing the extra validation step here).
    for file_key in file_keys:
        validate(personalisation_data[file_key], send_a_file_validation)

    personalisation_data = personalisation_data.copy()

    check_if_service_can_send_files_by_email(
        service_contact_link=authenticated_service.contact_link, service_id=authenticated_service.id
    )

    for key in file_keys:
        if simulated:
            personalisation_data[key] = (
                document_download_client.get_upload_url_for_simulated_email(service.id) + "/test-document"
            )
        else:
            confirm_email = personalisation_data[key].get("confirm_email_before_download", True)

            retention_period = (
                personalisation_data[key].get("retention_period") or DEFAULT_DOCUMENT_DOWNLOAD_RETENTION_PERIOD
            )

            filename = personalisation_data[key].get("filename")

            try:
                personalisation_data[key] = document_download_client.upload_document(
                    service.id,
                    personalisation_data[key]["file"],
                    personalisation_data[key].get("is_csv"),
                    confirmation_email=send_to if confirm_email is not False else None,
                    retention_period=retention_period,
                    filename=filename,
                )
            except DocumentDownloadError as e:
                raise BadRequestError(message=e.message, status_code=e.status_code) from e

    return personalisation_data, len(file_keys)


def process_letter_notification(
    *, letter_data, api_key, service, template, template_with_content, reply_to_text, precompiled=False
):
    if api_key.key_type == KEY_TYPE_TEAM:
        raise BadRequestError(message="Cannot send letters with a team api key", status_code=403)

    if service.restricted and api_key.key_type != KEY_TYPE_TEST:
        raise BadRequestError(message="Cannot send letters when service is in trial mode", status_code=403)

    if precompiled:
        return process_precompiled_letter_notifications(
            letter_data=letter_data, api_key=api_key, service=service, template=template, reply_to_text=reply_to_text
        )

    postage = validate_address(service, letter_data["personalisation"])

    test_key = api_key.key_type == KEY_TYPE_TEST

    status = NOTIFICATION_CREATED
    updated_at = None
    if test_key:
        # if we don't want to actually send the letter, then start it off in SENDING so we don't pick it up
        if not current_app.config["SEND_LETTERS_ENABLED"]:
            status = NOTIFICATION_SENDING
        # mark test letter as delivered and do not create a fake response later
        else:
            status = NOTIFICATION_DELIVERED
            updated_at = datetime.utcnow()

    queue = QueueNames.CREATE_LETTERS_PDF if not test_key else QueueNames.RESEARCH_MODE

    notification = create_letter_notification(
        letter_data=letter_data,
        service=service,
        template=template,
        api_key=api_key,
        status=status,
        reply_to_text=reply_to_text,
        updated_at=updated_at,
        postage=postage,
    )

    get_pdf_for_templated_letter.apply_async([str(notification.id)], queue=queue)

    if test_key and not current_app.config["SEND_LETTERS_ENABLED"]:
        create_fake_letter_callback.apply_async(
            [notification.id, notification.billable_units, notification.postage],
            queue=queue,
        )

    resp = create_response_for_post_notification(
        notification_id=notification.id,
        client_reference=notification.client_reference,
        template_id=notification.template_id,
        template_version=notification.template_version,
        notification_type=notification.notification_type,
        reply_to=reply_to_text,
        service_id=notification.service_id,
        template_with_content=template_with_content,
    )
    return resp


def process_precompiled_letter_notifications(*, letter_data, api_key, service, template, reply_to_text):
    try:
        status = NOTIFICATION_PENDING_VIRUS_CHECK
        letter_content = base64.b64decode(letter_data["content"])
    except ValueError as e:
        raise BadRequestError(message="Cannot decode letter content (invalid base64 encoding)", status_code=400) from e

    with transaction():
        notification = create_letter_notification(
            letter_data=letter_data,
            service=service,
            template=template,
            api_key=api_key,
            status=status,
            reply_to_text=reply_to_text,
        )
        filename = upload_letter_pdf(notification, letter_content, precompiled=True)

    resp = {"id": notification.id, "reference": notification.client_reference, "postage": notification.postage}

    # call task to add the filename to anti virus queue
    if current_app.config["ANTIVIRUS_ENABLED"]:
        current_app.logger.info("Calling task scan-file for %s", filename)
        notify_celery.send_task(
            name=TaskNames.SCAN_FILE,
            kwargs={"filename": filename},
            queue=QueueNames.ANTIVIRUS,
        )
    else:
        # stub out antivirus in dev
        sanitise_letter.apply_async([filename], queue=QueueNames.LETTERS)

    return resp


def get_reply_to_text(notification_type, form, template):
    reply_to = None
    if notification_type == EMAIL_TYPE:
        service_email_reply_to_id = form.get("email_reply_to_id", None)
        reply_to = (
            check_service_email_reply_to_id(str(authenticated_service.id), service_email_reply_to_id, notification_type)
            or template.reply_to_text
        )

    elif notification_type == SMS_TYPE:
        service_sms_sender_id = form.get("sms_sender_id", None)
        sms_sender_id = check_service_sms_sender_id(
            str(authenticated_service.id), service_sms_sender_id, notification_type
        )
        if sms_sender_id:
            reply_to = try_validate_and_format_phone_number(sms_sender_id)
        else:
            reply_to = template.reply_to_text

    elif notification_type == LETTER_TYPE:
        reply_to = template.reply_to_text

    return reply_to


def create_response_for_post_notification(
    notification_id,
    client_reference,
    template_id,
    template_version,
    service_id,
    notification_type,
    reply_to,
    template_with_content,
    unsubscribe_link=None,
):
    if notification_type == SMS_TYPE:
        create_resp_partial = functools.partial(
            create_post_sms_response_from_notification,
            from_number=reply_to,
        )
    elif notification_type == EMAIL_TYPE:
        create_resp_partial = functools.partial(
            create_post_email_response_from_notification,
            subject=template_with_content.subject,
            email_from=f"{authenticated_service.email_sender_local_part}@{current_app.config['NOTIFY_EMAIL_DOMAIN']}",
            unsubscribe_link=unsubscribe_link,
        )
    elif notification_type == LETTER_TYPE:
        create_resp_partial = functools.partial(
            create_post_letter_response_from_notification,
            subject=template_with_content.subject,
        )
    response = create_resp_partial(
        notification_id,
        client_reference,
        template_id,
        template_version,
        service_id,
        url_root=request.url_root,
        content=template_with_content.content_with_placeholders_filled_in,
    )
    return response
