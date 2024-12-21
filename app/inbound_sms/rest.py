from flask import Blueprint, current_app, jsonify, request
from notifications_utils.recipient_validation.phone_number import try_validate_and_format_phone_number

from app.constants import INBOUND_SMS_TYPE
from app.dao.inbound_numbers_dao import (
    archive_or_release_inbound_number_for_service,
    dao_get_inbound_number_for_service,
)
from app.dao.inbound_sms_dao import (
    dao_count_inbound_sms_for_service,
    dao_get_inbound_sms_by_id,
    dao_get_inbound_sms_for_service,
    dao_get_most_recent_inbound_usage_date,
    dao_get_paginated_most_recent_inbound_sms_by_user_number_for_service,
)
from app.dao.service_data_retention_dao import fetch_service_data_retention_by_notification_type
from app.dao.service_permissions_dao import (
    dao_remove_service_permission,
)
from app.dao.service_sms_sender_dao import dao_remove_inbound_sms_senders
from app.errors import register_errors
from app.inbound_sms.inbound_sms_schemas import get_inbound_sms_for_service_schema, remove_capability_schema
from app.schema_validation import validate

inbound_sms = Blueprint("inbound_sms", __name__, url_prefix="/service/<uuid:service_id>/inbound-sms")

register_errors(inbound_sms)


@inbound_sms.route("", methods=["POST"])
def post_inbound_sms_for_service(service_id):
    form = validate(request.get_json(), get_inbound_sms_for_service_schema)
    user_number = form.get("phone_number")

    if user_number:
        # we use this to normalise to an international phone number - but this may fail if it's an alphanumeric
        user_number = try_validate_and_format_phone_number(user_number, international=True)

    inbound_data_retention = fetch_service_data_retention_by_notification_type(service_id, "sms")
    limit_days = inbound_data_retention.days_of_retention if inbound_data_retention else 7

    results = dao_get_inbound_sms_for_service(service_id, user_number=user_number, limit_days=limit_days)
    return jsonify(data=[row.serialize() for row in results])


@inbound_sms.route("/most-recent", methods=["GET"])
def get_most_recent_inbound_sms_for_service(service_id):
    # used on the service inbox page
    page = request.args.get("page", 1)

    inbound_data_retention = fetch_service_data_retention_by_notification_type(service_id, "sms")
    limit_days = inbound_data_retention.days_of_retention if inbound_data_retention else 7

    # get most recent message for each user for service
    results = dao_get_paginated_most_recent_inbound_sms_by_user_number_for_service(service_id, int(page), limit_days)
    return jsonify(data=[row.serialize() for row in results.items], has_next=results.has_next)


@inbound_sms.route("/summary")
def get_inbound_sms_summary_for_service(service_id):
    # this is for the dashboard, so always limit to 7 days, even if they have a longer data retention
    count = dao_count_inbound_sms_for_service(service_id, limit_days=7)
    most_recent = dao_get_inbound_sms_for_service(service_id, limit=1)

    return jsonify(count=count, most_recent=most_recent[0].created_at.isoformat() if most_recent else None)


@inbound_sms.route("/<uuid:inbound_sms_id>", methods=["GET"])
def get_inbound_by_id(service_id, inbound_sms_id):
    message = dao_get_inbound_sms_by_id(service_id, inbound_sms_id)

    return jsonify(message.serialize()), 200


@inbound_sms.route("/most-recent-usage", methods=["GET"])
def get_most_recent_inbound_usage_date(service_id):
    inbound = dao_get_inbound_number_for_service(service_id)

    if not inbound:
        return jsonify(message="inbound not found"), 404

    most_recent_date = dao_get_most_recent_inbound_usage_date(service_id, inbound)

    return jsonify(most_recent_date=most_recent_date), 200


@inbound_sms.route("/remove-capability", methods=["POST"])
def remove_inbound_sms_capability(service_id):
    data = request.get_json()
    validate(data, remove_capability_schema)

    archive = data["archive"]

    try:
        dao_remove_service_permission(service_id, INBOUND_SMS_TYPE)
        dao_remove_inbound_sms_senders(service_id)
        archive_or_release_inbound_number_for_service(service_id, archive)

        return jsonify({}), 200
    except Exception as e:
        current_app.logger.error("error removing inbound SMS capability for service %s: %s", service_id, e)
        return jsonify({}), 500
