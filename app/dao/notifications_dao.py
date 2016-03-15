from flask import current_app
from app import db
from app.models import Notification, Job, NotificationStatistics, TEMPLATE_TYPE_SMS, TEMPLATE_TYPE_EMAIL
from sqlalchemy import desc
from datetime import datetime, timedelta
from app.clients.sms.firetext import FiretextResponses

def dao_get_notification_statistics_for_service(service_id):
    return NotificationStatistics.query.filter_by(
        service_id=service_id
    ).order_by(desc(NotificationStatistics.day)).all()


def dao_get_notification_statistics_for_service_and_day(service_id, day):
    return NotificationStatistics.query.filter_by(
        service_id=service_id,
        day=day
    ).order_by(desc(NotificationStatistics.day)).first()


def dao_create_notification(notification, notification_type):
    try:
        if notification.job_id:
            db.session.query(Job).filter_by(
                id=notification.job_id
            ).update({
                Job.notifications_sent: Job.notifications_sent + 1,
                Job.updated_at: datetime.utcnow()
            })

        update_count = db.session.query(NotificationStatistics).filter_by(
            day=notification.created_at.strftime('%Y-%m-%d'),
            service_id=notification.service_id
        ).update(update_query(notification_type, 'requested'))

        if update_count == 0:
            stats = NotificationStatistics(
                day=notification.created_at.strftime('%Y-%m-%d'),
                service_id=notification.service_id,
                sms_requested=1 if notification_type == TEMPLATE_TYPE_SMS else 0,
                emails_requested=1 if notification_type == TEMPLATE_TYPE_EMAIL else 0
            )
            db.session.add(stats)
        db.session.add(notification)
        db.session.commit()
    except:
        db.session.rollback()
        raise


def update_query(notification_type, status):
    print(notification_type)
    print(status)
    mapping = {
        'sms': {
            'requested': NotificationStatistics.sms_requested,
            'success': NotificationStatistics.sms_delivered,
            'failure': NotificationStatistics.sms_error
        },
        'email': {
            'requested': NotificationStatistics.emails_requested,
            'success': NotificationStatistics.emails_delivered,
            'failure': NotificationStatistics.emails_error
        }
    }
    return {
        mapping[notification_type][status]: mapping[notification_type][status] + 1
    }


def dao_update_notification(notification):
    notification.updated_at = datetime.utcnow()
    db.session.add(notification)
    db.session.commit()


def update_notification_status_by_id(notification_id, status):
    count = db.session.query(Notification).filter_by(
        id=notification_id
    ).update({
        Notification.status: status
    })
    if count == 1:
        notification = Notification.query.get(notification_id)
        db.session.query(NotificationStatistics).filter_by(
            day=notification.created_at.strftime('%Y-%m-%d'),
            service_id=notification.service_id
        ).update(update_query(notification.template.template_type, FiretextResponses.response_code_to_notify_stats(status)))

    db.session.commit()
    return count


def update_notification_status_by_reference(reference, status):
    count = db.session.query(Notification).filter_by(
        reference=reference
    ).update({
        Notification.status: status
    })
    db.session.commit()
    return count


def update_notification_reference_by_id(id, reference):
    count = db.session.query(Notification).filter_by(
        id=id
    ).update({
        Notification.reference: reference
    })
    db.session.commit()
    return count


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


def get_notification_by_id(notification_id):
    return Notification.query.filter_by(id=notification_id).first()


def get_notifications_for_service(service_id, page=1):
    query = Notification.query.filter_by(service_id=service_id).order_by(desc(Notification.created_at)).paginate(
        page=page,
        per_page=current_app.config['PAGE_SIZE']
    )
    return query


def delete_successful_notifications_created_more_than_a_day_ago():
    deleted = db.session.query(Notification).filter(
        Notification.created_at < datetime.utcnow() - timedelta(days=1),
        Notification.status == 'sent'
    ).delete()
    db.session.commit()
    return deleted


def delete_failed_notifications_created_more_than_a_week_ago():
    deleted = db.session.query(Notification).filter(
        Notification.created_at < datetime.utcnow() - timedelta(days=7),
        Notification.status == 'failed'
    ).delete()
    db.session.commit()
    return deleted
