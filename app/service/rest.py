import itertools
import uuid
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request
from notifications_utils.letter_timings import (
    letter_can_be_cancelled,
    too_late_to_cancel_letter,
)
from notifications_utils.timezones import convert_utc_to_bst
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import NoResultFound
from werkzeug.datastructures import MultiDict

from app.aws import s3
from app.config import QueueNames
from app.constants import (
    EMAIL_TYPE,
    KEY_TYPE_NORMAL,
    LETTER_TYPE,
    MOBILE_TYPE,
    NOTIFICATION_CANCELLED,
    NOTIFICATION_TYPES,
    SERVICE_JOIN_REQUEST_APPROVED,
)
from app.dao import fact_billing_dao, fact_notification_status_dao, notifications_dao
from app.dao.annual_billing_dao import set_default_free_allowance_for_service
from app.dao.api_key_dao import (
    expire_api_key,
    get_model_api_keys,
    get_unsigned_secret,
    save_model_api_key,
)
from app.dao.dao_utils import dao_rollback, transaction
from app.dao.date_util import get_financial_year
from app.dao.fact_notification_status_dao import (
    fetch_monthly_template_usage_for_service,
    fetch_notification_status_for_service_by_month,
    fetch_notification_status_for_service_for_day,
    fetch_notification_status_for_service_for_today_and_7_previous_days,
    fetch_stats_for_all_services_by_date_range,
)
from app.dao.organisation_dao import dao_get_organisation_by_service_id
from app.dao.returned_letters_dao import (
    fetch_most_recent_returned_letter,
    fetch_recent_returned_letter_count,
    fetch_returned_letter_summary,
    fetch_returned_letters,
)
from app.dao.service_contact_list_dao import (
    dao_archive_contact_list,
    dao_get_contact_list_by_id,
    dao_get_contact_lists,
    save_service_contact_list,
)
from app.dao.service_data_retention_dao import (
    fetch_service_data_retention,
    fetch_service_data_retention_by_id,
    fetch_service_data_retention_by_notification_type,
    insert_service_data_retention,
    update_service_data_retention,
)
from app.dao.service_email_reply_to_dao import (
    add_reply_to_email_address_for_service,
    archive_reply_to_email_address,
    dao_get_reply_to_by_id,
    dao_get_reply_to_by_service_id,
    update_reply_to_email_address,
)
from app.dao.service_guest_list_dao import (
    dao_add_and_commit_guest_list_contacts,
    dao_fetch_service_guest_list,
    dao_remove_service_guest_list,
)
from app.dao.service_join_requests_dao import (
    dao_cancel_pending_service_join_requests,
    dao_create_service_join_request,
    dao_get_service_join_request_by_id,
    dao_update_service_join_request,
)
from app.dao.service_letter_contact_dao import (
    add_letter_contact_for_service,
    archive_letter_contact,
    dao_get_letter_contact_by_id,
    dao_get_letter_contacts_by_service_id,
    update_letter_contact,
)
from app.dao.service_sms_sender_dao import (
    archive_sms_sender,
    dao_add_sms_sender_for_service,
    dao_get_service_sms_senders_by_id,
    dao_get_sms_senders_by_service_id,
    dao_update_service_sms_sender,
)
from app.dao.services_dao import (
    dao_add_user_to_service,
    dao_archive_service,
    dao_create_service,
    dao_fetch_all_services,
    dao_fetch_all_services_by_user,
    dao_fetch_live_services_data,
    dao_fetch_service_by_id,
    dao_fetch_todays_stats_for_all_services,
    dao_fetch_todays_stats_for_service,
    dao_remove_user_from_service,
    dao_update_service,
    get_services_by_partial_name,
)
from app.dao.templates_dao import dao_get_template_by_id
from app.dao.unsubscribe_request_dao import (
    assign_unbatched_unsubscribe_requests_to_report_dao,
    create_unsubscribe_request_reports_dao,
    get_latest_unsubscribe_request_date_dao,
    get_unsubscribe_request_report_by_id_dao,
    get_unsubscribe_requests_data_for_download_dao,
    get_unsubscribe_requests_statistics_dao,
    update_unsubscribe_request_report_processed_by_date_dao,
)
from app.dao.users_dao import get_user_by_id, save_user_attribute
from app.errors import InvalidRequest, register_errors
from app.letters.utils import adjust_daily_service_limits_for_cancelled_letters, letter_print_day
from app.models import (
    EmailBranding,
    LetterBranding,
    Permission,
    Service,
    ServiceContactList,
    Template,
    UnsubscribeRequestReport,
)
from app.notifications.process_notifications import (
    persist_notification,
    send_notification_to_queue,
)
from app.one_click_unsubscribe.rest import create_unsubscribe_request_reports_summary
from app.schema_validation import validate
from app.schemas import (
    api_key_schema,
    detailed_service_schema,
    email_data_request_schema,
    notification_with_template_schema,
    notifications_filter_schema,
    service_schema,
)
from app.service import statistics
from app.service.send_notification import (
    send_one_off_notification,
    send_pdf_letter_notification,
)
from app.service.send_pdf_letter_schema import send_pdf_letter_request
from app.service.sender import send_notification_to_service_users
from app.service.service_contact_list_schema import (
    create_service_contact_list_schema,
)
from app.service.service_data_retention_schema import (
    add_service_data_retention_request,
    update_service_data_retention_request,
)
from app.service.service_join_request_schema import service_join_request_schema, service_join_request_update_schema
from app.service.service_senders_schema import (
    add_service_email_reply_to_request,
    add_service_letter_contact_block_request,
    add_service_sms_sender_request,
)
from app.service.utils import get_guest_list_objects
from app.user.users_schema import post_set_permissions_schema
from app.utils import (
    DATE_FORMAT,
    DATETIME_FORMAT_NO_TIMEZONE,
    get_prev_next_pagination_links,
    midnight_n_days_ago,
    utc_string_to_bst_string,
)
from app.v2.errors import ValidationError

service_blueprint = Blueprint("service", __name__)

register_errors(service_blueprint)


@service_blueprint.errorhandler(IntegrityError)
def handle_integrity_error(exc):
    """
    Handle integrity errors caused by the unique constraint on ix_organisation_name
    """
    if any(
        f'duplicate key value violates unique constraint "{constraint}"' in str(exc)
        for constraint in {"services_name_key", "services_normalised_service_name_key"}
    ):
        duplicate_name = exc.params.get("name") or exc.params.get("normalised_service_name")
        return (
            jsonify(
                result="error",
                message={"name": [f"Duplicate service name '{duplicate_name}'"]},
            ),
            400,
        )
    current_app.logger.exception(exc)
    return jsonify(result="error", message="Internal server error"), 500


@service_blueprint.route("", methods=["GET"])
def get_services():
    only_active = request.args.get("only_active") == "True"
    detailed = request.args.get("detailed") == "True"
    user_id = request.args.get("user_id", None)
    include_from_test_key = request.args.get("include_from_test_key", "True") != "False"

    # If start and end date are not set, we are expecting today's stats.
    today = str(datetime.utcnow().date())

    start_date = datetime.strptime(request.args.get("start_date", today), "%Y-%m-%d").date()
    end_date = datetime.strptime(request.args.get("end_date", today), "%Y-%m-%d").date()

    if user_id:
        services = dao_fetch_all_services_by_user(user_id, only_active)
    elif detailed:
        result = jsonify(
            data=get_detailed_services(
                start_date=start_date,
                end_date=end_date,
                only_active=only_active,
                include_from_test_key=include_from_test_key,
            )
        )
        return result
    else:
        services = dao_fetch_all_services(only_active)
    data = service_schema.dump(services, many=True)
    return jsonify(data=data)


@service_blueprint.route("/find-services-by-name", methods=["GET"])
def find_services_by_name():
    service_name = request.args.get("service_name")
    if not service_name:
        errors = {"service_name": ["Missing data for required field."]}
        raise InvalidRequest(errors, status_code=400)
    fetched_services = get_services_by_partial_name(service_name)
    data = [service.serialize_for_org_dashboard() for service in fetched_services]
    return jsonify(data=data), 200


@service_blueprint.route("/live-services-data", methods=["GET"])
def get_live_services_data():
    data = dao_fetch_live_services_data()
    return jsonify(data=data)


@service_blueprint.route("/<uuid:service_id>", methods=["GET"])
def get_service_by_id(service_id):
    if request.args.get("detailed") == "True":
        data = get_detailed_service(service_id, today_only=request.args.get("today_only") == "True")
    else:
        fetched = dao_fetch_service_by_id(service_id)

        data = service_schema.dump(fetched)
    return jsonify(data=data)


@service_blueprint.route("/<uuid:service_id>/statistics")
def get_service_notification_statistics(service_id):
    return jsonify(
        data=get_service_statistics(
            service_id, request.args.get("today_only") == "True", int(request.args.get("limit_days", 7))
        )
    )


@service_blueprint.route("", methods=["POST"])
def create_service():
    data = request.get_json()

    if not data.get("user_id"):
        errors = {"user_id": ["Missing data for required field."]}
        raise InvalidRequest(errors, status_code=400)
    data.pop("service_domain", None)

    # validate json with marshmallow
    service_schema.load(data)

    user = get_user_by_id(data.pop("user_id"))

    # unpack valid json into service object
    valid_service = Service.from_json(data)

    with transaction():
        dao_create_service(valid_service, user)
        set_default_free_allowance_for_service(service=valid_service, year_start=None)

    return jsonify(data=service_schema.dump(valid_service)), 201


@service_blueprint.route("/<uuid:service_id>", methods=["POST"])
def update_service(service_id):
    req_json = request.get_json()
    fetched_service = dao_fetch_service_by_id(service_id)
    # Capture the status change here as Marshmallow changes this later
    service_going_live = fetched_service.restricted and not req_json.get("restricted", True)

    current_data = dict(service_schema.dump(fetched_service).items())
    current_data.update(request.get_json())

    service = service_schema.load(current_data)

    if "email_branding" in req_json:
        email_branding_id = req_json["email_branding"]
        service.email_branding = None if not email_branding_id else EmailBranding.query.get(email_branding_id)
    if "letter_branding" in req_json:
        letter_branding_id = req_json["letter_branding"]
        service.letter_branding = None if not letter_branding_id else LetterBranding.query.get(letter_branding_id)

    dao_update_service(service)

    if service_going_live:
        send_notification_to_service_users(
            service_id=service_id,
            template_id=current_app.config["SERVICE_NOW_LIVE_TEMPLATE_ID"],
            personalisation={
                "service_name": current_data["name"],
            },
            include_user_fields=["name"],
        )

    return jsonify(data=service_schema.dump(fetched_service)), 200


@service_blueprint.route("/<uuid:service_id>/api-key", methods=["POST"])
def create_api_key(service_id=None):
    fetched_service = dao_fetch_service_by_id(service_id=service_id)
    valid_api_key = api_key_schema.load(request.get_json())
    valid_api_key.service = fetched_service
    save_model_api_key(valid_api_key)
    unsigned_api_key = get_unsigned_secret(valid_api_key.id)
    return jsonify(data=unsigned_api_key), 201


@service_blueprint.route("/<uuid:service_id>/api-key/revoke/<uuid:api_key_id>", methods=["POST"])
def revoke_api_key(service_id, api_key_id):
    expire_api_key(service_id=service_id, api_key_id=api_key_id)
    return jsonify(), 202


@service_blueprint.route("/<uuid:service_id>/api-keys", methods=["GET"])
@service_blueprint.route("/<uuid:service_id>/api-keys/<uuid:key_id>", methods=["GET"])
def get_api_keys(service_id, key_id=None):
    dao_fetch_service_by_id(service_id=service_id)

    try:
        if key_id:
            api_keys = [get_model_api_keys(service_id=service_id, id=key_id)]
        else:
            api_keys = get_model_api_keys(service_id=service_id)
    except NoResultFound as e:
        error = f"API key not found for id: {service_id}"
        raise InvalidRequest(error, status_code=404) from e

    return jsonify(apiKeys=api_key_schema.dump(api_keys, many=True)), 200


@service_blueprint.route("/<uuid:service_id>/users", methods=["GET"])
def get_users_for_service(service_id):
    fetched = dao_fetch_service_by_id(service_id)
    return jsonify(data=[x.serialize() for x in fetched.users])


@service_blueprint.route("/<uuid:service_id>/users/<user_id>", methods=["POST"])
def add_user_to_service(service_id, user_id):
    service = dao_fetch_service_by_id(service_id)
    user = get_user_by_id(user_id=user_id)

    if user in service.users:
        error = f"User id: {user_id} already part of service id: {service_id}"
        raise InvalidRequest(error, status_code=400)

    data = request.get_json()
    validate(data, post_set_permissions_schema)

    permissions = [
        Permission(service_id=service_id, user_id=user_id, permission=p["permission"]) for p in data["permissions"]
    ]
    folder_permissions = data.get("folder_permissions", [])

    dao_add_user_to_service(service, user, permissions, folder_permissions)
    data = service_schema.dump(service)
    return jsonify(data=data), 201


@service_blueprint.route("/<uuid:service_id>/users/<user_id>", methods=["DELETE"])
def remove_user_from_service(service_id, user_id):
    service = dao_fetch_service_by_id(service_id)
    user = get_user_by_id(user_id=user_id)
    if user not in service.users:
        error = "User not found"
        raise InvalidRequest(error, status_code=404)

    elif len(service.users) == 1:
        error = "You cannot remove the only user for a service"
        raise InvalidRequest(error, status_code=400)

    dao_remove_user_from_service(service, user)
    return jsonify({}), 204


# This is placeholder get method until more thought
# goes into how we want to fetch and view various items in history
# tables. This is so product owner can pass stories as done
@service_blueprint.route("/<uuid:service_id>/history", methods=["GET"])
def get_service_history(service_id):
    from app.models import ApiKey, Service, TemplateHistory
    from app.schemas import (
        api_key_history_schema,
        service_history_schema,
        template_history_schema,
    )

    service_history = Service.get_history_model().query.filter_by(id=service_id).all()
    service_data = service_history_schema.dump(service_history, many=True)
    api_key_history = ApiKey.get_history_model().query.filter_by(service_id=service_id).all()
    api_keys_data = api_key_history_schema.dump(api_key_history, many=True)

    template_history = TemplateHistory.query.filter_by(service_id=service_id).all()
    template_data = template_history_schema.dump(template_history, many=True)

    data = {
        "service_history": service_data,
        "api_key_history": api_keys_data,
        "template_history": template_data,
        "events": [],
    }

    return jsonify(data=data)


@service_blueprint.route("/<uuid:service_id>/notifications/csv", methods=["GET", "POST"])
def get_all_notifications_for_service_for_csv(service_id):
    data = notifications_filter_schema.load(request.args)

    older_than = data.get("older_than")
    page_size = data["page_size"] if "page_size" in data else current_app.config.get("PAGE_SIZE")
    limit_days = data.get("limit_days")

    current_notifications_batch = notifications_dao.get_notifications_for_service(
        service_id,
        filter_dict=data,
        older_than=older_than,
        page_size=page_size,
        count_pages=False,
        limit_days=limit_days,
        error_out=False,
        with_personalisation=False,
        include_jobs=True,
        include_from_test_key=False,
        include_one_off=True,
    )

    kwargs = request.args.to_dict()
    kwargs["service_id"] = service_id

    notifications = [notification.serialize_for_csv() for notification in current_notifications_batch.items]

    return (
        jsonify(
            notifications=notifications,
            page_size=page_size,
        ),
        200,
    )


@service_blueprint.route("/<uuid:service_id>/notifications", methods=["GET", "POST"])
def get_all_notifications_for_service(service_id):
    if request.method == "GET":
        data = notifications_filter_schema.load(request.args)
    elif request.method == "POST":
        # Must transform request.get_json() to MultiDict as NotificationsFilterSchema expects a MultiDict.
        # Unlike request.args, request.get_json() does not return a MultiDict but instead just a dict.
        data = notifications_filter_schema.load(MultiDict(request.get_json()))

    if data.get("to"):
        notification_type = data.get("template_type")[0] if data.get("template_type") else None
        return search_for_notification_by_to_field(
            service_id=service_id,
            search_term=data["to"],
            statuses=data.get("status"),
            notification_type=notification_type,
        )

    page = data.get("page", 1)

    page_size = data["page_size"] if "page_size" in data else current_app.config.get("PAGE_SIZE")
    limit_days = data.get("limit_days")
    include_jobs = data.get("include_jobs", True)
    include_from_test_key = data.get("include_from_test_key", False)
    include_one_off = data.get("include_one_off", True)

    current_notifications_batch = notifications_dao.get_notifications_for_service(
        service_id,
        filter_dict=data,
        page=page,
        page_size=page_size,
        count_pages=False,
        limit_days=limit_days,
        include_jobs=include_jobs,
        include_from_test_key=include_from_test_key,
        include_one_off=include_one_off,
    )

    kwargs = request.args.to_dict()
    kwargs["service_id"] = service_id

    notifications = notification_with_template_schema.dump(current_notifications_batch.items, many=True)

    # We try and get the next page of results to work out if we need provide a pagination link to the next page
    # in our response if it exists. Note, this could be done instead by changing `count_pages` in the previous
    # call to be True which will enable us to use Flask-Sqlalchemy to tell if there is a next page of results but
    # this way is much more performant for services with many results (unlike Flask SqlAlchemy, this approach
    # doesn't do an additional query to count all the results of which there could be millions but instead only
    # asks for a single extra page of results).
    next_notifications_batch = notifications_dao.get_notifications_for_service(
        service_id,
        filter_dict=data,
        page=page + 1,
        page_size=page_size,
        count_pages=False,
        limit_days=limit_days,
        include_jobs=include_jobs,
        include_from_test_key=include_from_test_key,
        include_one_off=include_one_off,
        error_out=False,  # False so that if there are no results, it doesn't end in aborting with a 404
    )

    # count_pages is not being used for whether to count the number of pages, but instead as a flag
    # for whether to show pagination links
    count_pages = data.get("count_pages", True)

    links = {}
    if count_pages:
        links = get_prev_next_pagination_links(
            page, len(next_notifications_batch.items), ".get_all_notifications_for_service", **kwargs
        )

    return (
        jsonify(
            notifications=notifications,
            page_size=page_size,
            links=links,
        ),
        200,
    )


@service_blueprint.route("/<uuid:service_id>/notifications/count", methods=["GET"])
def count_notifications_for_service(service_id):
    data = notifications_filter_schema.load(request.args)
    limit_days = data.get("limit_days")
    multidict = MultiDict(data)

    template_types = multidict.getlist("template_type")

    # set default values for template_types and limit_days
    if not limit_days:
        limit_days = 7

    if not template_types:
        template_types = NOTIFICATION_TYPES

    notification_count = fact_billing_dao.get_count_of_notifications_sent(
        service_id=service_id,
        template_types=template_types,
        limit_days=limit_days,
    )

    return jsonify({"notifications_sent_count": notification_count}), 200


@service_blueprint.route("/<uuid:service_id>/notifications/<uuid:notification_id>", methods=["GET"])
def get_notification_for_service(service_id, notification_id):
    notification = notifications_dao.get_notification_with_personalisation(
        service_id,
        notification_id,
        key_type=None,
    )
    return (
        jsonify(
            notification_with_template_schema.dump(notification),
        ),
        200,
    )


@service_blueprint.route("/<uuid:service_id>/notifications/<uuid:notification_id>/cancel", methods=["POST"])
def cancel_notification_for_service(service_id, notification_id):
    notification = notifications_dao.get_notification_by_id(notification_id, service_id)

    if not notification:
        raise InvalidRequest("Notification not found", status_code=404)
    elif notification.notification_type != LETTER_TYPE:
        raise InvalidRequest("Notification cannot be cancelled - only letters can be cancelled", status_code=400)
    elif not letter_can_be_cancelled(notification.status, notification.created_at):
        print_day = letter_print_day(notification.created_at)
        if too_late_to_cancel_letter(notification.created_at):
            message = f"It’s too late to cancel this letter. Printing started {print_day} at 5.30pm"
        elif notification.status == "cancelled":
            message = "This letter has already been cancelled."
        else:
            message = (
                f"We could not cancel this letter. "
                f"Letter status: {notification.status}, created_at: {notification.created_at}"
            )
        raise InvalidRequest(message, status_code=400)

    updated_notification = notifications_dao.update_notification_status_by_id(
        notification_id,
        NOTIFICATION_CANCELLED,
    )
    adjust_daily_service_limits_for_cancelled_letters(service_id, 1, notification.created_at)

    return jsonify(notification_with_template_schema.dump(updated_notification)), 200


def search_for_notification_by_to_field(service_id, search_term, statuses, notification_type):
    results = notifications_dao.dao_get_notifications_by_recipient_or_reference(
        service_id=service_id,
        search_term=search_term,
        statuses=statuses,
        notification_type=notification_type,
        page=1,
        page_size=current_app.config["PAGE_SIZE"],
    )

    # We try and get the next page of results to work out if we need provide a pagination link to the next page
    # in our response. Note, this was previously be done by having
    # notifications_dao.dao_get_notifications_by_recipient_or_reference use count=False when calling
    # Flask-Sqlalchemys `paginate'. But instead we now use this way because it is much more performant for
    # services with many results (unlike using Flask SqlAlchemy `paginate` with `count=True`, this approach
    # doesn't do an additional query to count all the results of which there could be millions but instead only
    # asks for a single extra page of results).
    next_page_of_pagination = notifications_dao.dao_get_notifications_by_recipient_or_reference(
        service_id=service_id,
        search_term=search_term,
        statuses=statuses,
        notification_type=notification_type,
        page=2,
        page_size=current_app.config["PAGE_SIZE"],
        error_out=False,  # False so that if there are no results, it doesn't end in aborting with a 404
    )

    return (
        jsonify(
            notifications=notification_with_template_schema.dump(results.items, many=True),
            links=get_prev_next_pagination_links(
                1,
                len(next_page_of_pagination.items),
                ".get_all_notifications_for_service",
                statuses=statuses,
                notification_type=notification_type,
                service_id=service_id,
            ),
        ),
        200,
    )


@service_blueprint.route("/<uuid:service_id>/notifications/monthly", methods=["GET"])
def get_monthly_notification_stats(service_id):
    # check service_id validity
    dao_fetch_service_by_id(service_id)

    try:
        year = int(request.args.get("year", "NaN"))
    except ValueError as e:
        raise InvalidRequest("Year must be a number", status_code=400) from e

    start_date, end_date = get_financial_year(year)

    data = statistics.create_empty_monthly_notification_status_stats_dict(year)

    stats = fetch_notification_status_for_service_by_month(start_date, end_date, service_id)
    statistics.add_monthly_notification_status_stats(data, stats)

    now = datetime.utcnow()
    if end_date > now:
        todays_deltas = fetch_notification_status_for_service_for_day(convert_utc_to_bst(now), service_id=service_id)
        statistics.add_monthly_notification_status_stats(data, todays_deltas)

    return jsonify(data=data)


def get_detailed_service(service_id, today_only=False):
    service = dao_fetch_service_by_id(service_id)

    service.statistics = get_service_statistics(service_id, today_only)
    return detailed_service_schema.dump(service)


def get_service_statistics(service_id, today_only, limit_days=7):
    # today_only flag is used by the send page to work out if the service will exceed their daily usage by sending a job
    if today_only:
        stats = dao_fetch_todays_stats_for_service(service_id)
    else:
        stats = fetch_notification_status_for_service_for_today_and_7_previous_days(service_id, limit_days=limit_days)

    return statistics.format_statistics(stats)


def get_detailed_services(start_date, end_date, only_active=False, include_from_test_key=True):
    if start_date == datetime.utcnow().date():
        stats = dao_fetch_todays_stats_for_all_services(
            include_from_test_key=include_from_test_key, only_active=only_active
        )
    else:
        stats = fetch_stats_for_all_services_by_date_range(
            start_date=start_date,
            end_date=end_date,
            include_from_test_key=include_from_test_key,
        )
    results = []
    for _service_id, rows in itertools.groupby(stats, lambda x: x.service_id):
        rows = list(rows)
        s = statistics.format_statistics(rows)
        results.append(
            {
                "id": str(rows[0].service_id),
                "name": rows[0].name,
                "notification_type": rows[0].notification_type,
                "restricted": rows[0].restricted,
                "active": rows[0].active,
                "created_at": rows[0].created_at,
                "statistics": s,
            }
        )
    return results


@service_blueprint.route("/<uuid:service_id>/guest-list", methods=["GET"])
def get_guest_list(service_id):
    service = dao_fetch_service_by_id(service_id)

    if not service:
        raise InvalidRequest("Service does not exist", status_code=404)

    guest_list = dao_fetch_service_guest_list(service.id)
    return jsonify(
        email_addresses=[item.recipient for item in guest_list if item.recipient_type == EMAIL_TYPE],
        phone_numbers=[item.recipient for item in guest_list if item.recipient_type == MOBILE_TYPE],
    )


@service_blueprint.route("/<uuid:service_id>/guest-list", methods=["PUT"])
def update_guest_list(service_id):
    # doesn't commit so if there are any errors, we preserve old values in db
    dao_remove_service_guest_list(service_id)
    try:
        guest_list_objects = get_guest_list_objects(service_id, request.get_json())
    except ValueError as e:
        current_app.logger.exception(e)
        dao_rollback()
        msg = f"{str(e)} is not a valid email address or phone number"
        raise InvalidRequest(msg, 400) from e
    else:
        dao_add_and_commit_guest_list_contacts(guest_list_objects)
        return "", 204


@service_blueprint.route("/<uuid:service_id>/archive", methods=["POST"])
def archive_service(service_id):
    """
    When a service is archived the service is made inactive, templates are archived and api keys are revoked.
    There is no coming back from this operation.
    :param service_id:
    :return:
    """
    service = dao_fetch_service_by_id(service_id)

    if service.active:
        dao_archive_service(service.id)

    return "", 204


@service_blueprint.route("/<uuid:service_id>/notifications/templates_usage/monthly", methods=["GET"])
def get_monthly_template_usage(service_id):
    try:
        start_date, end_date = get_financial_year(int(request.args.get("year", "NaN")))
        data = fetch_monthly_template_usage_for_service(start_date=start_date, end_date=end_date, service_id=service_id)
        stats = []
        for i in data:
            stats.append(
                {
                    "template_id": str(i.template_id),
                    "name": i.name,
                    "type": i.template_type,
                    "month": i.month,
                    "year": i.year,
                    "count": i.count,
                    "is_precompiled_letter": i.is_precompiled_letter,
                }
            )

        return jsonify(stats=stats), 200
    except ValueError as e:
        raise InvalidRequest("Year must be a number", status_code=400) from e


@service_blueprint.route("/<uuid:service_id>/send-notification", methods=["POST"])
def create_one_off_notification(service_id):
    resp = send_one_off_notification(service_id, request.get_json())
    return jsonify(resp), 201


@service_blueprint.route("/<uuid:service_id>/send-pdf-letter", methods=["POST"])
def create_pdf_letter(service_id):
    data = validate(request.get_json(), send_pdf_letter_request)
    resp = send_pdf_letter_notification(service_id, data)
    return jsonify(resp), 201


@service_blueprint.route("/<uuid:service_id>/email-reply-to", methods=["GET"])
def get_email_reply_to_addresses(service_id):
    result = dao_get_reply_to_by_service_id(service_id)
    return jsonify([i.serialize() for i in result]), 200


@service_blueprint.route("/<uuid:service_id>/email-reply-to/<uuid:reply_to_id>", methods=["GET"])
def get_email_reply_to_address(service_id, reply_to_id):
    result = dao_get_reply_to_by_id(reply_to_id=reply_to_id, service_id=service_id)
    return jsonify(result.serialize()), 200


@service_blueprint.route("/<uuid:service_id>/email-reply-to/verify", methods=["POST"])
def verify_reply_to_email_address(service_id):
    email_address = email_data_request_schema.load(request.get_json())

    check_if_reply_to_address_already_in_use(service_id, email_address["email"])
    template = dao_get_template_by_id(current_app.config["REPLY_TO_EMAIL_ADDRESS_VERIFICATION_TEMPLATE_ID"])
    notify_service = Service.query.get(current_app.config["NOTIFY_SERVICE_ID"])
    saved_notification = persist_notification(
        template_id=template.id,
        template_version=template.version,
        recipient=email_address["email"],
        service=notify_service,
        personalisation="",
        notification_type=template.template_type,
        api_key_id=None,
        key_type=KEY_TYPE_NORMAL,
        reply_to_text=notify_service.get_default_reply_to_email_address(),
    )

    send_notification_to_queue(saved_notification, queue=QueueNames.NOTIFY)

    return jsonify(data={"id": saved_notification.id}), 201


@service_blueprint.route("/<uuid:service_id>/email-reply-to", methods=["POST"])
def add_service_reply_to_email_address(service_id):
    # validate the service exists, throws ResultNotFound exception.
    dao_fetch_service_by_id(service_id)
    form = validate(request.get_json(), add_service_email_reply_to_request)
    check_if_reply_to_address_already_in_use(service_id, form["email_address"])
    new_reply_to = add_reply_to_email_address_for_service(
        service_id=service_id, email_address=form["email_address"], is_default=form.get("is_default", True)
    )
    return jsonify(data=new_reply_to.serialize()), 201


@service_blueprint.route("/<uuid:service_id>/email-reply-to/<uuid:reply_to_email_id>", methods=["POST"])
def update_service_reply_to_email_address(service_id, reply_to_email_id):
    # validate the service exists, throws ResultNotFound exception.
    dao_fetch_service_by_id(service_id)
    form = validate(request.get_json(), add_service_email_reply_to_request)
    new_reply_to = update_reply_to_email_address(
        service_id=service_id,
        reply_to_id=reply_to_email_id,
        email_address=form["email_address"],
        is_default=form.get("is_default", True),
    )
    return jsonify(data=new_reply_to.serialize()), 200


@service_blueprint.route("/<uuid:service_id>/email-reply-to/<uuid:reply_to_email_id>/archive", methods=["POST"])
def delete_service_reply_to_email_address(service_id, reply_to_email_id):
    archived_reply_to = archive_reply_to_email_address(service_id, reply_to_email_id)

    return jsonify(data=archived_reply_to.serialize()), 200


@service_blueprint.route("/<uuid:service_id>/letter-contact", methods=["GET"])
def get_letter_contacts(service_id):
    result = dao_get_letter_contacts_by_service_id(service_id)
    return jsonify([i.serialize() for i in result]), 200


@service_blueprint.route("/<uuid:service_id>/letter-contact/<uuid:letter_contact_id>", methods=["GET"])
def get_letter_contact_by_id(service_id, letter_contact_id):
    result = dao_get_letter_contact_by_id(service_id=service_id, letter_contact_id=letter_contact_id)
    return jsonify(result.serialize()), 200


@service_blueprint.route("/<uuid:service_id>/letter-contact", methods=["POST"])
def add_service_letter_contact(service_id):
    # validate the service exists, throws ResultNotFound exception.
    dao_fetch_service_by_id(service_id)
    form = validate(request.get_json(), add_service_letter_contact_block_request)
    new_letter_contact = add_letter_contact_for_service(
        service_id=service_id, contact_block=form["contact_block"], is_default=form.get("is_default", True)
    )
    return jsonify(data=new_letter_contact.serialize()), 201


@service_blueprint.route("/<uuid:service_id>/letter-contact/<uuid:letter_contact_id>", methods=["POST"])
def update_service_letter_contact(service_id, letter_contact_id):
    # validate the service exists, throws ResultNotFound exception.
    dao_fetch_service_by_id(service_id)
    form = validate(request.get_json(), add_service_letter_contact_block_request)
    new_reply_to = update_letter_contact(
        service_id=service_id,
        letter_contact_id=letter_contact_id,
        contact_block=form["contact_block"],
        is_default=form.get("is_default", True),
    )
    return jsonify(data=new_reply_to.serialize()), 200


@service_blueprint.route("/<uuid:service_id>/letter-contact/<uuid:letter_contact_id>/archive", methods=["POST"])
def delete_service_letter_contact(service_id, letter_contact_id):
    archived_letter_contact = archive_letter_contact(service_id, letter_contact_id)

    return jsonify(data=archived_letter_contact.serialize()), 200


@service_blueprint.route("/<uuid:service_id>/sms-sender", methods=["POST"])
def add_service_sms_sender(service_id):
    dao_fetch_service_by_id(service_id)
    form = validate(request.get_json(), add_service_sms_sender_request)
    sms_sender = form.get("sms_sender")

    new_sms_sender = dao_add_sms_sender_for_service(
        service_id=service_id, sms_sender=sms_sender, is_default=form["is_default"]
    )
    return jsonify(new_sms_sender.serialize()), 201


@service_blueprint.route("/<uuid:service_id>/sms-sender/<uuid:sms_sender_id>", methods=["POST"])
def update_service_sms_sender(service_id, sms_sender_id):
    form = validate(request.get_json(), add_service_sms_sender_request)

    sms_sender_to_update = dao_get_service_sms_senders_by_id(service_id=service_id, service_sms_sender_id=sms_sender_id)
    if sms_sender_to_update.inbound_number_id and form["sms_sender"] != sms_sender_to_update.sms_sender:
        raise InvalidRequest(f"You can not change the inbound number for service {service_id}", status_code=400)

    new_sms_sender = dao_update_service_sms_sender(
        service_id=service_id,
        service_sms_sender_id=sms_sender_id,
        is_default=form["is_default"],
        sms_sender=form["sms_sender"],
    )
    return jsonify(new_sms_sender.serialize()), 200


@service_blueprint.route("/<uuid:service_id>/sms-sender/<uuid:sms_sender_id>/archive", methods=["POST"])
def delete_service_sms_sender(service_id, sms_sender_id):
    sms_sender = archive_sms_sender(service_id, sms_sender_id)

    return jsonify(data=sms_sender.serialize()), 200


@service_blueprint.route("/<uuid:service_id>/sms-sender/<uuid:sms_sender_id>", methods=["GET"])
def get_service_sms_sender_by_id(service_id, sms_sender_id):
    sms_sender = dao_get_service_sms_senders_by_id(service_id=service_id, service_sms_sender_id=sms_sender_id)
    return jsonify(sms_sender.serialize()), 200


@service_blueprint.route("/<uuid:service_id>/sms-sender", methods=["GET"])
def get_service_sms_senders_for_service(service_id):
    sms_senders = dao_get_sms_senders_by_service_id(service_id=service_id)
    return jsonify([sms_sender.serialize() for sms_sender in sms_senders]), 200


@service_blueprint.route("/<uuid:service_id>/organisation", methods=["GET"])
def get_organisation_for_service(service_id):
    organisation = dao_get_organisation_by_service_id(service_id=service_id)
    return jsonify(organisation.serialize() if organisation else {}), 200


@service_blueprint.route("/<uuid:service_id>/data-retention", methods=["GET"])
def get_data_retention_for_service(service_id):
    data_retention_list = fetch_service_data_retention(service_id)
    return jsonify([data_retention.serialize() for data_retention in data_retention_list]), 200


@service_blueprint.route("/<uuid:service_id>/data-retention/notification-type/<notification_type>", methods=["GET"])
def get_data_retention_for_service_notification_type(service_id, notification_type):
    data_retention = fetch_service_data_retention_by_notification_type(service_id, notification_type)
    return jsonify(data_retention.serialize() if data_retention else {}), 200


@service_blueprint.route("/<uuid:service_id>/data-retention/<uuid:data_retention_id>", methods=["GET"])
def get_data_retention_for_service_by_id(service_id, data_retention_id):
    data_retention = fetch_service_data_retention_by_id(service_id, data_retention_id)
    return jsonify(data_retention.serialize() if data_retention else {}), 200


@service_blueprint.route("/<uuid:service_id>/data-retention", methods=["POST"])
def create_service_data_retention(service_id):
    form = validate(request.get_json(), add_service_data_retention_request)
    try:
        new_data_retention = insert_service_data_retention(
            service_id=service_id,
            notification_type=form.get("notification_type"),
            days_of_retention=form.get("days_of_retention"),
        )
    except IntegrityError as e:
        raise InvalidRequest(
            message="Service already has data retention for {} notification type".format(form.get("notification_type")),
            status_code=400,
        ) from e

    return jsonify(result=new_data_retention.serialize()), 201


@service_blueprint.route("/<uuid:service_id>/data-retention/<uuid:data_retention_id>", methods=["POST"])
def modify_service_data_retention(service_id, data_retention_id):
    form = validate(request.get_json(), update_service_data_retention_request)

    update_count = update_service_data_retention(
        service_data_retention_id=data_retention_id,
        service_id=service_id,
        days_of_retention=form.get("days_of_retention"),
    )
    if update_count == 0:
        raise InvalidRequest(
            message=f"The service data retention for id: {data_retention_id} was not found for service: {service_id}",
            status_code=404,
        )

    return "", 204


@service_blueprint.route("/monthly-data-by-service")
def get_monthly_notification_data_by_service():
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    rows = fact_notification_status_dao.fetch_monthly_notification_statuses_per_service(start_date, end_date)

    serialized_results = [
        [
            str(row.date_created),
            str(row.service_id),
            row.service_name,
            row.notification_type,
            row.count_sending,
            row.count_delivered,
            row.count_technical_failure,
            row.count_temporary_failure,
            row.count_permanent_failure,
            row.count_sent,
        ]
        for row in rows
    ]
    return jsonify(serialized_results)


def check_if_reply_to_address_already_in_use(service_id, email_address):
    existing_reply_to_addresses = dao_get_reply_to_by_service_id(service_id)
    if email_address in [i.email_address for i in existing_reply_to_addresses]:
        raise InvalidRequest(
            f"‘{email_address}’ is already a reply-to email address for this service.", status_code=409
        )


@service_blueprint.route("/<uuid:service_id>/returned-letter-statistics", methods=["GET"])
def returned_letter_statistics(service_id):
    most_recent = fetch_most_recent_returned_letter(service_id)

    if not most_recent:
        return jsonify(
            {
                "returned_letter_count": 0,
                "most_recent_report": None,
            }
        )

    most_recent_reported_at = datetime.combine(most_recent.reported_at, datetime.min.time())

    if most_recent_reported_at < midnight_n_days_ago(7):
        return jsonify(
            {
                "returned_letter_count": 0,
                "most_recent_report": most_recent.reported_at.strftime(DATETIME_FORMAT_NO_TIMEZONE),
            }
        )

    count = fetch_recent_returned_letter_count(service_id)

    return jsonify(
        {
            "returned_letter_count": count.returned_letter_count,
            "most_recent_report": most_recent.reported_at.strftime(DATETIME_FORMAT_NO_TIMEZONE),
        }
    )


@service_blueprint.route("/<uuid:service_id>/returned-letter-summary", methods=["GET"])
def returned_letter_summary(service_id):
    results = fetch_returned_letter_summary(service_id)

    json_results = [
        {"returned_letter_count": x.returned_letter_count, "reported_at": x.reported_at.strftime(DATE_FORMAT)}
        for x in results
    ]

    return jsonify(json_results)


@service_blueprint.route("/<uuid:service_id>/returned-letters", methods=["GET"])
def get_returned_letters(service_id):
    results = fetch_returned_letters(service_id=service_id, report_date=request.args.get("reported_at"))

    json_results = [
        {
            "notification_id": x.notification_id,
            # client reference can only be added on API letters
            "client_reference": x.client_reference if x.api_key_id else None,
            "reported_at": x.reported_at.strftime(DATE_FORMAT),
            "created_at": x.created_at.strftime(DATETIME_FORMAT_NO_TIMEZONE),
            # it doesn't make sense to show hidden/precompiled templates
            "template_name": x.template_name if not x.hidden else None,
            "template_id": x.template_id if not x.hidden else None,
            "template_version": x.template_version if not x.hidden else None,
            "user_name": x.user_name or "API",
            "email_address": x.email_address or "API",
            "original_file_name": x.original_file_name,
            "job_row_number": x.job_row_number,
            # the file name for a letter uploaded via the UI
            "uploaded_letter_file_name": x.client_reference if x.hidden and not x.api_key_id else None,
        }
        for x in results
    ]

    return jsonify(sorted(json_results, key=lambda i: i["created_at"], reverse=True))


@service_blueprint.route("/<uuid:service_id>/unsubscribe-request-reports-summary", methods=["GET"])
def get_unsubscribe_request_reports_summary(service_id):
    """
    This returns report summaries for both batched and un-batched unsubscribe requests.

    In the case of un-batched unsubscribe requests:
    - is_a_batched_result has a value of False.
    - the latest_timestamp value is the date of the newest unsubscribe request in the report
    - the earliest_timestamp value is the date of the oldest unsubscribe request in the report

    parameter: uuid service_id

    return: reports_summary = []

    """
    return jsonify(create_unsubscribe_request_reports_summary(service_id))


@service_blueprint.route("/<uuid:service_id>/unsubscribe-request-statistics", methods=["GET"])
def get_unsubscribe_requests_statistics(service_id):
    data = {}
    if unsubscribe_statistics := get_unsubscribe_requests_statistics_dao(service_id):
        data = {
            "unsubscribe_requests_count": unsubscribe_statistics.unsubscribe_requests_count,
            "datetime_of_latest_unsubscribe_request": unsubscribe_statistics.datetime_of_latest_unsubscribe_request,
        }
    elif latest_unsubscribe_request := get_latest_unsubscribe_request_date_dao(service_id):
        data = {
            "unsubscribe_requests_count": 0,
            "datetime_of_latest_unsubscribe_request": latest_unsubscribe_request.datetime_of_latest_unsubscribe_request,
        }

    return jsonify(data), 200


@service_blueprint.route("/<uuid:service_id>/process-unsubscribe-request-report/<uuid:batch_id>", methods=["POST"])
def process_unsubscribe_request_report(service_id, batch_id):
    """
    This endpoint processes unsubscribe_request_reports by updating the processed_by_service_at
    field
    """
    if data := request.get_json():
        report_has_been_processed = data["report_has_been_processed"]
    else:
        raise InvalidRequest(
            message={"marked_as_completed": "missing data for required field"},
            status_code=400,
        )
    if report := get_unsubscribe_request_report_by_id_dao(batch_id):
        update_unsubscribe_request_report_processed_by_date_dao(report, report_has_been_processed)
    else:
        raise InvalidRequest(
            message={"batch_id": f"No UnsubscribeRequestReport found for id:{batch_id}"},
            status_code=400,
        )

    return "", 204


@service_blueprint.route("/<uuid:service_id>/create-unsubscribe-request-report", methods=["POST"])
def create_unsubscribe_request_report(service_id):
    summary_data = request.get_json()
    if summary_data:
        unsubscribe_request_report = UnsubscribeRequestReport(
            id=uuid.uuid4(),
            count=summary_data["count"],
            earliest_timestamp=summary_data["earliest_timestamp"],
            latest_timestamp=summary_data["latest_timestamp"],
            service_id=service_id,
        )
        create_unsubscribe_request_reports_dao(unsubscribe_request_report)
        assign_unbatched_unsubscribe_requests_to_report_dao(
            report_id=unsubscribe_request_report.id,
            service_id=unsubscribe_request_report.service_id,
            earliest_timestamp=unsubscribe_request_report.earliest_timestamp,
            latest_timestamp=unsubscribe_request_report.latest_timestamp,
        )
        return (
            jsonify(
                {
                    "report_id": unsubscribe_request_report.id,
                }
            ),
            201,
        )
    else:
        raise InvalidRequest(
            message={"summary_data": "summary data needed to create an unsubscribe request report is missing"},
            status_code=400,
        )


@service_blueprint.route("/<uuid:service_id>/unsubscribe-request-report/<uuid:batch_id>", methods=["GET"])
def get_unsubscribe_request_report_for_download(service_id, batch_id):
    if report := get_unsubscribe_request_report_by_id_dao(batch_id):
        data = {
            "batch_id": report.id,
            "earliest_timestamp": report.earliest_timestamp,
            "latest_timestamp": report.latest_timestamp,
            "unsubscribe_requests": [
                {
                    "email_address": unsubscribe_request.email_address,
                    "template_name": unsubscribe_request.template_name,
                    "original_file_name": unsubscribe_request.original_file_name,
                    "template_sent_at": utc_string_to_bst_string(unsubscribe_request.template_sent_at),
                    "unsubscribe_request_received_at": utc_string_to_bst_string(
                        unsubscribe_request.unsubscribe_request_received_at
                    ),
                }
                for unsubscribe_request in get_unsubscribe_requests_data_for_download_dao(service_id, report.id)
            ],
        }
        return jsonify(data), 200
    else:
        raise InvalidRequest(
            message=f"No report available for {batch_id}",
            status_code=400,
        )


@service_blueprint.route("/<uuid:service_id>/contact-list", methods=["GET"])
def get_contact_list(service_id):
    contact_lists = dao_get_contact_lists(service_id)

    return jsonify([x.serialize() for x in contact_lists])


@service_blueprint.route("/<uuid:service_id>/contact-list/<uuid:contact_list_id>", methods=["GET"])
def get_contact_list_by_id(service_id, contact_list_id):
    contact_list = dao_get_contact_list_by_id(service_id=service_id, contact_list_id=contact_list_id)

    return jsonify(contact_list.serialize())


@service_blueprint.route("/<uuid:service_id>/contact-list/<uuid:contact_list_id>", methods=["DELETE"])
def delete_contact_list_by_id(service_id, contact_list_id):
    contact_list = dao_get_contact_list_by_id(
        service_id=service_id,
        contact_list_id=contact_list_id,
    )
    dao_archive_contact_list(contact_list)
    s3.remove_contact_list_from_s3(service_id, contact_list_id)

    return "", 204


@service_blueprint.route("/<uuid:service_id>/contact-list", methods=["POST"])
def create_contact_list(service_id):
    service_contact_list = validate(request.get_json(), create_service_contact_list_schema)
    service_contact_list["created_by_id"] = service_contact_list.pop("created_by")
    service_contact_list["created_at"] = datetime.utcnow()
    service_contact_list["service_id"] = str(service_id)
    list_to_save = ServiceContactList(**service_contact_list)

    save_service_contact_list(list_to_save)

    return jsonify(list_to_save.serialize()), 201


@service_blueprint.route("<uuid:service_id>/service-join-request", methods=["POST"])
def create_service_join_request(service_id: uuid.UUID):
    data = request.get_json()

    try:
        validate(data, service_join_request_schema)
    except ValidationError as err:
        raise InvalidRequest(message=err.messages, status_code=400) from err

    new_request = dao_create_service_join_request(
        requester_id=data["requester_id"],
        service_id=service_id,
        contacted_user_ids=data["contacted_user_ids"],
    )

    return jsonify({"service_join_request_id": str(new_request.id)}), 201


@service_blueprint.route("/service-join-request/<uuid:request_id>", methods=["GET"])
def get_service_join_request(request_id: uuid.UUID):
    service_join_request = dao_get_service_join_request_by_id(request_id)

    if not service_join_request:
        raise InvalidRequest(message=f"Service join request with ID {request_id} not found.", status_code=404)

    return jsonify(service_join_request.serialize()), 200


@service_blueprint.route("/update-service-join-request-status/<uuid:request_id>", methods=["POST"])
def update_service_join_request(request_id: uuid.UUID):
    data = request.get_json()

    validate(data, service_join_request_update_schema)

    status = data["status"]
    status_changed_by_id = data["status_changed_by_id"]
    reason = data.get("reason", None)

    updated_request = dao_update_service_join_request(request_id, status, status_changed_by_id, reason)

    if updated_request is None:
        return jsonify({"message": "Service join request not found"}), 404

    if status == SERVICE_JOIN_REQUEST_APPROVED:
        permissions = data.get("permissions", None)

        if permissions:
            permissions = [
                Permission(service_id=updated_request.service_id, user_id=updated_request.requester_id, permission=p)
                for p in permissions
            ]

        requester_user = get_user_by_id(updated_request.requester_id)
        approver_user = get_user_by_id(updated_request.status_changed_by_id)
        service = dao_fetch_service_by_id(updated_request.service_id)
        folder_permissions = data.get("folder_permissions", [])

        dao_add_user_to_service(service, requester_user, permissions, folder_permissions)

        updated_auth_type = data.get("auth_type")
        _update_auth_type(updated_auth_type, requester_user)

        send_service_join_request_decision_email(
            requester_email_address=requester_user.email_address,
            template=dao_get_template_by_id(current_app.config["SERVICE_JOIN_REQUEST_APPROVED_TEMPLATE_ID"]),
            personalisation=create_personalisation(requester_user.name, approver_user.name, service.name, service.id),
        )

        dao_cancel_pending_service_join_requests(requester_user.id, approver_user.id, service.id)

    return jsonify(updated_request.serialize()), 200


def create_personalisation(requester_name: str, approver_name: str, service_name: str, service_id: uuid):
    admin_base_url = current_app.config["ADMIN_BASE_URL"]
    return {
        "requester_name": requester_name,
        "approver_name": approver_name,
        "service_name": service_name,
        "dashboard_url": f"{admin_base_url}/services/{service_id}",
    }


def send_service_join_request_decision_email(requester_email_address: str, template: Template, personalisation: dict):
    notify_service = dao_fetch_service_by_id(current_app.config["NOTIFY_SERVICE_ID"])

    saved_notification = persist_notification(
        template_id=template.id,
        template_version=template.version,
        recipient=requester_email_address,
        service=notify_service,
        personalisation=personalisation,
        notification_type=EMAIL_TYPE,
        api_key_id=None,
        key_type=KEY_TYPE_NORMAL,
        reply_to_text=notify_service.get_default_reply_to_email_address(),
    )

    send_notification_to_queue(saved_notification, queue=QueueNames.NOTIFY)


def _update_auth_type(updated_auth_type, user):
    if updated_auth_type and updated_auth_type != user.auth_type:
        if updated_auth_type == "sms_auth" and not user.mobile_number:
            return

        save_user_attribute(user, update_dict={"auth_type": updated_auth_type})
