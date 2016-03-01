from datetime import datetime

from flask import (
    Blueprint,
    jsonify,
    request,
    current_app,
    url_for
)

from app import api_user, encryption, create_uuid
from app.authentication.auth import require_admin
from app.dao import (
    templates_dao,
    services_dao,
    notifications_dao
)
from app.schemas import (
    email_notification_schema,
    sms_template_notification_schema,
    notification_status_schema
)
from app.celery.tasks import send_sms, send_email
from sqlalchemy.orm.exc import NoResultFound

notifications = Blueprint('notifications', __name__)

from app.errors import register_errors

register_errors(notifications)

SMS_NOTIFICATION = 'sms'
EMAIL_NOTIFICATION = 'email'


@notifications.route('/notifications/<string:notification_id>', methods=['GET'])
def get_notifications(notification_id):
    try:
        notification = notifications_dao.get_notification(api_user['client'], notification_id)
        return jsonify({'notification': notification_status_schema.dump(notification).data}), 200
    except NoResultFound:
        return jsonify(result="error", message="not found"), 404


@notifications.route('/notifications', methods=['GET'])
def get_all_notifications():
    page = get_page_from_request()

    if not page:
        return jsonify(result="error", message="Invalid page"), 400

    all_notifications = notifications_dao.get_notifications_for_service(api_user['client'], page)

    return jsonify(
        notifications=notification_status_schema.dump(all_notifications.items, many=True).data,
        links=pagination_links(
            all_notifications,
            '.get_all_notifications',
            request.args
        )
    ), 200


@notifications.route('/service/<service_id>/notifications', methods=['GET'])
@require_admin()
def get_all_notifications_for_service(service_id):
    page = get_page_from_request()

    if not page:
        return jsonify(result="error", message="Invalid page"), 400

    all_notifications = notifications_dao.get_notifications_for_service(service_id, page)

    return jsonify(
        notifications=notification_status_schema.dump(all_notifications.items, many=True).data,
        links=pagination_links(
            all_notifications,
            '.get_all_notifications_for_service',
            request.args
        )
    ), 200


@notifications.route('/service/<service_id>/job/<job_id>/notifications', methods=['GET'])
@require_admin()
def get_all_notifications_for_service_job(service_id, job_id):
    page = get_page_from_request()

    if not page:
        return jsonify(result="error", message="Invalid page"), 400

    all_notifications = notifications_dao.get_notifications_for_job(service_id, job_id, page)

    return jsonify(
        notifications=notification_status_schema.dump(all_notifications.items, many=True).data,
        links=pagination_links(
            all_notifications,
            '.get_all_notifications_for_service_job',
            request.args
        )
    ), 200


def get_page_from_request():
    if 'page' in request.args:
        try:
            return int(request.args['page'])

        except ValueError:
            return None
    else:
        return 1


def pagination_links(pagination, endpoint, args):
    links = dict()
    if pagination.has_prev:
        links['prev'] = url_for(endpoint, **dict(list(args.items()) + [('page', pagination.prev_num)]))
    if pagination.has_next:
        links['next'] = url_for(endpoint, **dict(list(args.items()) + [('page', pagination.next_num)]))
        links['last'] = url_for(endpoint, **dict(list(args.items()) + [('page', pagination.pages)]))
    return links


@notifications.route('/notifications/sms', methods=['POST'])
def create_sms_notification():
    return send_notification(notification_type=SMS_NOTIFICATION)


@notifications.route('/notifications/email', methods=['POST'])
def create_email_notification():
    return send_notification(notification_type=EMAIL_NOTIFICATION)


def send_notification(notification_type):
    assert notification_type

    service_id = api_user['client']

    schema = sms_template_notification_schema if notification_type is SMS_NOTIFICATION else email_notification_schema

    notification, errors = schema.load(request.get_json())
    if errors:
        return jsonify(result="error", message=errors), 400

    template = templates_dao.dao_get_template_by_id_and_service_id(
        template_id=notification['template'],
        service_id=service_id
    )

    if not template:
        return jsonify(
            result="error",
            message={
                'template': ['Template {} not found for service {}'.format(notification['template'], service_id)]
            }
        ), 404

    service = services_dao.dao_fetch_service_by_id(api_user['client'])

    if service.restricted:
        if notification_type is SMS_NOTIFICATION:
            if notification['to'] not in [user.mobile_number for user in service.users]:
                return jsonify(
                    result="error", message={'to': ['Invalid phone number for restricted service']}), 400
        else:
            if notification['to'] not in [user.email_address for user in service.users]:
                return jsonify(
                    result="error", message={'to': ['Email address not permitted for restricted service']}), 400

    notification_id = create_uuid()

    if notification_type is SMS_NOTIFICATION:
        send_sms.apply_async((
            service_id,
            notification_id,
            encryption.encrypt(notification),
            str(datetime.utcnow())),
            queue='sms')
    else:
        send_email.apply_async((
            service_id,
            notification_id,
            template.subject,
            "{}@{}".format(service.email_from, current_app.config['NOTIFY_EMAIL_DOMAIN']),
            encryption.encrypt(notification),
            str(datetime.utcnow())),
            queue='email')
    return jsonify({'notification_id': notification_id}), 201
