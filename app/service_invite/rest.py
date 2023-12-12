from flask import Blueprint, current_app, jsonify, request
from itsdangerous import BadData, SignatureExpired
from notifications_utils.url_safe_token import check_token, generate_token

from app.config import QueueNames
from app.constants import EMAIL_TYPE, KEY_TYPE_NORMAL
from app.dao.invited_user_dao import (
    get_invited_user_by_id,
    get_invited_user_by_service_and_id,
    get_invited_users_for_service,
    save_invited_user,
)
from app.dao.services_dao import dao_fetch_service_by_id
from app.dao.templates_dao import dao_get_template_by_id
from app.dao.users_dao import get_user_by_id
from app.errors import InvalidRequest, register_errors
from app.models import Service
from app.notifications.process_notifications import (
    persist_notification,
    send_notification_to_queue,
)
from app.schemas import invited_user_schema
from app.v2.errors import BadRequestError

service_invite = Blueprint("service_invite", __name__)

register_errors(service_invite)


@service_invite.route("/service/<service_id>/invite", methods=["POST"])
def create_invited_user(service_id):
    request_json = request.get_json()
    invited_user = invited_user_schema.load(request_json)
    save_invited_user(invited_user)

    template_id = current_app.config["INVITATION_EMAIL_TEMPLATE_ID"]

    template = dao_get_template_by_id(template_id)
    service = Service.query.get(current_app.config["NOTIFY_SERVICE_ID"])

    saved_notification = persist_notification(
        template_id=template.id,
        template_version=template.version,
        recipient=invited_user.email_address,
        service=service,
        personalisation={
            "user_name": invited_user.from_user.name,
            "service_name": invited_user.service.name,
            "url": invited_user_url(
                invited_user.id,
                request_json.get("invite_link_host"),
            ),
        },
        notification_type=EMAIL_TYPE,
        api_key_id=None,
        key_type=KEY_TYPE_NORMAL,
        reply_to_text=invited_user.from_user.email_address,
    )

    send_notification_to_queue(saved_notification, queue=QueueNames.NOTIFY)

    return jsonify(data=invited_user_schema.dump(invited_user)), 201


@service_invite.route("/service/<service_id>/invite", methods=["GET"])
def get_invited_users_by_service(service_id):
    invited_users = get_invited_users_for_service(service_id)
    return jsonify(data=invited_user_schema.dump(invited_users, many=True)), 200


@service_invite.route("/service/<service_id>/invite/<invited_user_id>", methods=["GET"])
def get_invited_user_by_service(service_id, invited_user_id):
    invited_user = get_invited_user_by_service_and_id(service_id, invited_user_id)
    return jsonify(data=invited_user_schema.dump(invited_user)), 200


@service_invite.route("/service/<service_id>/invite/<invited_user_id>", methods=["POST"])
def update_invited_user(service_id, invited_user_id):
    fetched = get_invited_user_by_service_and_id(service_id=service_id, invited_user_id=invited_user_id)

    current_data = dict(invited_user_schema.dump(fetched).items())
    current_data.update(request.get_json())
    update_dict = invited_user_schema.load(current_data)
    save_invited_user(update_dict)
    return jsonify(data=invited_user_schema.dump(fetched)), 200


def invited_user_url(invited_user_id, invite_link_host=None):
    token = generate_token(str(invited_user_id), current_app.config["SECRET_KEY"], current_app.config["DANGEROUS_SALT"])

    if invite_link_host is None:
        invite_link_host = current_app.config["ADMIN_BASE_URL"]

    return f"{invite_link_host}/invitation/{token}"


@service_invite.route("/invite/service/<uuid:invited_user_id>", methods=["GET"])
def get_invited_user(invited_user_id):
    invited_user = get_invited_user_by_id(invited_user_id)
    return jsonify(data=invited_user_schema.dump(invited_user)), 200


@service_invite.route("/invite/service/<token>", methods=["GET"])
@service_invite.route("/invite/service/check/<token>", methods=["GET"])
def validate_service_invitation_token(token):
    max_age_seconds = 60 * 60 * 24 * current_app.config["INVITATION_EXPIRATION_DAYS"]

    try:
        invited_user_id = check_token(
            token, current_app.config["SECRET_KEY"], current_app.config["DANGEROUS_SALT"], max_age_seconds
        )
    except SignatureExpired as e:
        errors = {
            "invitation": "Your invitation to GOV.UK Notify has expired. "
            "Please ask the person that invited you to send you another one"
        }
        raise InvalidRequest(errors, status_code=400) from e
    except BadData as e:
        errors = {"invitation": "Something’s wrong with this link. Make sure you’ve copied the whole thing."}
        raise InvalidRequest(errors, status_code=400) from e

    invited_user = get_invited_user_by_id(invited_user_id)
    return jsonify(data=invited_user_schema.dump(invited_user)), 200


@service_invite.route("/service/<service_id>/invite/request-for/<user_to_invite_id>", methods=["POST"])
def request_invite_to_service(service_id, user_to_invite_id):
    request_json = request.get_json()
    user_requesting_invite = get_user_by_id(user_to_invite_id)
    recipients_of_invite_request_ids = request_json["service_managers_ids"]
    recipients_of_invite_request = [get_user_by_id(recipient_id) for recipient_id in recipients_of_invite_request_ids]
    service = dao_fetch_service_by_id(service_id)
    reason_for_request = request_json["reason"]
    invite_link_host = request_json["invite_link_host"]
    accept_invite_request_url = f"{invite_link_host}/services/{service.id}/users/invite/{user_requesting_invite.id}"
    request_again_url = f"{invite_link_host}/services/{service.id}/join"

    if user_requesting_invite.services and service in user_requesting_invite.services:
        raise BadRequestError(400, "user-already-in-service")

    send_service_invite_request(
        user_requesting_invite, recipients_of_invite_request, service, reason_for_request, accept_invite_request_url
    )

    send_receipt_after_sending_request_invite_letter(
        user_requesting_invite,
        service=service,
        recipients_of_invite_request=recipients_of_invite_request,
        request_again_url=request_again_url,
    )

    return {}, 204


def send_service_invite_request(
    user_requesting_invite, recipients_of_invite_request, service, reason_for_request, accept_invite_request_url
):
    template_id = current_app.config["REQUEST_INVITE_TO_SERVICE_TEMPLATE_ID"]
    template = dao_get_template_by_id(template_id)
    notify_service = Service.query.get(current_app.config["NOTIFY_SERVICE_ID"])
    number_of_notifications_generated = 0
    for recipient in recipients_of_invite_request:
        if service in recipient.services:
            saved_notification = persist_notification(
                template_id=template.id,
                template_version=template.version,
                recipient="notify-join-service-request@digital.cabinet-office.gov.uk",
                service=notify_service,
                personalisation={
                    "name": recipient.name,
                    "requester_name": user_requesting_invite.name,
                    "requester_email": user_requesting_invite.email_address,
                    "service_name": service.name,
                    "reason_given": "yes" if reason_for_request else "no",
                    "reason": "\n".join(f"^ {line}" for line in reason_for_request.splitlines()),
                    "url": accept_invite_request_url,
                },
                notification_type=template.template_type,
                api_key_id=None,
                key_type=KEY_TYPE_NORMAL,
                reply_to_text=user_requesting_invite.email_address,
            )
            send_notification_to_queue(saved_notification, queue=QueueNames.NOTIFY)
            number_of_notifications_generated += 1

        else:
            # In a scenario were multiple service managers are listed, and the list contains an
            # invalid service manager, we would rather log the errors and not raise an exception so
            # that notifications can still be sent to the valid service managers
            current_app.logger.error(
                "request-to-join-service email not sent to user %s - they are not part of service %s",
                recipient.id,
                service.id,
            )

    if number_of_notifications_generated == 0:
        # If no notification is sent we want to raise an exception
        raise BadRequestError(400, "no-valid-service-managers-ids")


def send_receipt_after_sending_request_invite_letter(
    user_requesting_invite,
    *,
    service,
    recipients_of_invite_request,
    request_again_url,
):
    template_id = current_app.config["RECEIPT_FOR_REQUEST_INVITE_TO_SERVICE_TEMPLATE_ID"]
    template = dao_get_template_by_id(template_id)
    notify_service = Service.query.get(current_app.config["NOTIFY_SERVICE_ID"])

    saved_notification = persist_notification(
        template_id=template.id,
        template_version=template.version,
        recipient=user_requesting_invite.email_address,
        service=notify_service,
        personalisation={
            "name": user_requesting_invite.name,
            "service name": service.name,
            "service admin names": [f"{user.name} – {user.email_address}" for user in recipients_of_invite_request],
            "request again url": request_again_url,
        },
        notification_type=template.template_type,
        api_key_id=None,
        key_type=KEY_TYPE_NORMAL,
        reply_to_text=notify_service.get_default_reply_to_email_address(),
    )
    send_notification_to_queue(saved_notification, queue=QueueNames.NOTIFY)
