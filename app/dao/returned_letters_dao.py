from datetime import datetime

from sqlalchemy import desc, func
from sqlalchemy.dialects.postgresql import insert

from app import db
from app.dao.dao_utils import autocommit
from app.models import (
    Job,
    Notification,
    NotificationHistory,
    ReturnedLetter,
    Template,
    User,
)
from app.utils import midnight_n_days_ago


def _get_notification_ids_for_references(references):
    notification_ids = (
        db.session.query(Notification.id, Notification.service_id).filter(Notification.reference.in_(references)).all()
    )

    notification_history_ids = (
        db.session.query(NotificationHistory.id, NotificationHistory.service_id)
        .filter(NotificationHistory.reference.in_(references))
        .all()
    )

    return notification_ids + notification_history_ids


@autocommit
def insert_returned_letters(references):
    data = _get_notification_ids_for_references(references)
    for row in data:
        table = ReturnedLetter.__table__

        stmt = (
            insert(table)
            .values(
                reported_at=datetime.utcnow().date(),
                service_id=row.service_id,
                notification_id=row.id,
                created_at=datetime.utcnow(),
            )
            .on_conflict_do_nothing(index_elements=[table.c.notification_id])
        )
        db.session.connection().execute(stmt)


def fetch_recent_returned_letter_count(service_id):
    return (
        db.session.query(
            func.count(ReturnedLetter.notification_id).label("returned_letter_count"),
        )
        .filter(
            ReturnedLetter.service_id == service_id,
            ReturnedLetter.reported_at > midnight_n_days_ago(7),
        )
        .one()
    )


def fetch_most_recent_returned_letter(service_id):
    return (
        db.session.query(
            ReturnedLetter.reported_at,
        )
        .filter(
            ReturnedLetter.service_id == service_id,
        )
        .order_by(desc(ReturnedLetter.reported_at))
        .first()
    )


def fetch_returned_letter_summary(service_id):
    return (
        db.session.query(
            func.count(ReturnedLetter.notification_id).label("returned_letter_count"), ReturnedLetter.reported_at
        )
        .filter(
            ReturnedLetter.service_id == service_id,
        )
        .group_by(ReturnedLetter.reported_at)
        .order_by(desc(ReturnedLetter.reported_at))
        .all()
    )


def fetch_returned_letters(service_id, report_date):
    results = []
    for table in [Notification, NotificationHistory]:
        query = (
            db.session.query(
                ReturnedLetter.notification_id,
                ReturnedLetter.reported_at,
                table.client_reference,
                table.created_at,
                Template.name.label("template_name"),
                table.template_id,
                table.template_version,
                Template.hidden,
                table.api_key_id,
                table.created_by_id,
                User.name.label("user_name"),
                User.email_address,
                Job.original_file_name,
                # row numbers in notifications db table start at 0, but in spreadsheet uploaded by service user
                # the recipient rows would start at row 2 (row 1 is column headers).
                (table.job_row_number + 2).label("job_row_number"),
            )
            .outerjoin(User, table.created_by_id == User.id)
            .outerjoin(Job, table.job_id == Job.id)
            .filter(
                ReturnedLetter.service_id == service_id,
                ReturnedLetter.reported_at == report_date,
                ReturnedLetter.notification_id == table.id,
                table.template_id == Template.id,
            )
            .order_by(desc(ReturnedLetter.reported_at), desc(table.created_at))
        )
        results = results + query.all()
    results = sorted(results, key=lambda i: i.created_at, reverse=True)
    return results
