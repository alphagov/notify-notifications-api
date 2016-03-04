from flask import current_app
from app import db
from app.models import Notification, Job
from sqlalchemy import desc


def dao_create_notification(notification):
    if notification.job_id:
        db.session.query(Job).filter_by(
            id=notification.job_id
        ).update({Job.notifications_sent: Job.notifications_sent + 1})
    db.session.add(notification)
    db.session.commit()


def dao_update_notification(notification):
    db.session.add(notification)
    db.session.commit()


def get_notification_for_job(service_id, job_id, notification_id):
    return Notification.query.filter_by(service_id=service_id, job_id=job_id, id=notification_id).one()


def get_notifications_for_job(service_id, job_id, page=1):
    query = Notification.query.filter_by(service_id=service_id, job_id=job_id) \
        .order_by(desc(Notification.created_at)) \
        .paginate(
        page=page,
        per_page=current_app.config['PAGE_SIZE']
    )
    return query


def get_notification(service_id, notification_id):
    return Notification.query.filter_by(service_id=service_id, id=notification_id).one()


def get_notifications_for_service(service_id, page=1):
    query = Notification.query.filter_by(service_id=service_id).order_by(desc(Notification.created_at)).paginate(
        page=page,
        per_page=current_app.config['PAGE_SIZE']
    )
    return query
