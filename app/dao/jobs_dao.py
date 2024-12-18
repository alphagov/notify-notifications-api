import uuid
from datetime import datetime, timedelta

from flask import current_app
from notifications_utils.clients.redis import RequestCache
from notifications_utils.letter_timings import (
    CANCELLABLE_JOB_LETTER_STATUSES,
    letter_can_be_cancelled,
)
from sqlalchemy import and_, asc, desc, func

from app import db, redis_store
from app.constants import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FINISHED,
    JOB_STATUS_PENDING,
    JOB_STATUS_SCHEDULED,
    LETTER_TYPE,
    NOTIFICATION_CANCELLED,
    NOTIFICATION_CREATED,
    NOTIFICATION_STATUS_TYPES_COMPLETED,
)
from app.dao.dao_utils import autocommit
from app.dao.fact_notification_status_dao import fetch_notification_statuses_for_job
from app.dao.templates_dao import dao_get_template_by_id
from app.models import (
    FactNotificationStatus,
    Job,
    Notification,
    ServiceDataRetention,
    Template,
)
from app.utils import midnight_n_days_ago


def dao_get_notification_outcomes_for_job(job_id):
    notification_statuses = (
        db.session.query(func.count(Notification.status).label("count"), Notification.status)
        .filter(Notification.job_id == job_id)
        .group_by(Notification.status)
        .all()
    )

    if not notification_statuses:
        notification_statuses = (
            db.session.query(
                FactNotificationStatus.notification_count.label("count"),
                FactNotificationStatus.notification_status.label("status"),
            )
            .filter(FactNotificationStatus.job_id == job_id)
            .all()
        )
    return notification_statuses


def dao_get_job_by_service_id_and_job_id(service_id, job_id):
    return Job.query.filter_by(service_id=service_id, id=job_id).one()


def dao_get_jobs_by_service_id(
    service_id,
    *,
    limit_days=None,
    page=1,
    page_size=50,
    statuses=None,
    contact_list_id=None,
):
    query_filter = [
        Job.service_id == service_id,
        Job.original_file_name != current_app.config["TEST_MESSAGE_FILENAME"],
        Job.original_file_name != current_app.config["ONE_OFF_MESSAGE_FILENAME"],
    ]
    if limit_days is not None:
        query_filter.append(Job.created_at >= midnight_n_days_ago(limit_days))
    if statuses is not None and statuses != [""]:
        query_filter.append(Job.job_status.in_(statuses))
    if contact_list_id is not None:
        query_filter.append(Job.contact_list_id == contact_list_id)
    return (
        Job.query.filter(*query_filter)
        .order_by(Job.processing_started.desc(), Job.created_at.desc())
        .paginate(page=page, per_page=page_size)
    )


def dao_get_scheduled_job_stats(
    service_id,
):
    return (
        db.session.query(
            func.count(Job.id),
            func.min(Job.scheduled_for),
        )
        .filter(
            Job.service_id == service_id,
            Job.job_status == JOB_STATUS_SCHEDULED,
        )
        .one()
    )


def dao_get_job_by_id(job_id):
    return Job.query.filter_by(id=job_id).one()


def dao_archive_job(job):
    job.archived = True
    db.session.add(job)
    db.session.commit()


def dao_set_scheduled_jobs_to_pending():
    """
    Sets all past scheduled jobs to pending, and then returns them for further processing.

    this is used in the run_scheduled_jobs task, so we put a FOR UPDATE lock on the job table for the duration of
    the transaction so that if the task is run more than once concurrently, one task will block the other select
    from completing until it commits.
    """
    jobs = (
        Job.query.filter(Job.job_status == JOB_STATUS_SCHEDULED, Job.scheduled_for < datetime.utcnow())
        .order_by(asc(Job.scheduled_for))
        .with_for_update()
        .all()
    )

    for job in jobs:
        job.job_status = JOB_STATUS_PENDING

    db.session.add_all(jobs)
    db.session.commit()

    return jobs


def dao_get_scheduled_job_by_id_and_service_id(job_id, service_id):
    """Fetch a scheduled job from the DB, taking an exclusive lock on the row so that it can't be edited until this
    transaction is committed or rolled back. This can be used to fetch the job in order to cancel it, while at the
    same time preventing that job from being picked up and processing started by celery."""
    return (
        Job.query.filter(
            Job.service_id == service_id,
            Job.id == job_id,
            Job.job_status == JOB_STATUS_SCHEDULED,
        )
        .with_for_update()
        .one()
    )


def dao_create_job(job):
    if not job.id:
        job.id = uuid.uuid4()
    db.session.add(job)
    db.session.commit()


def dao_update_job(job):
    db.session.add(job)
    db.session.commit()


def dao_get_jobs_older_than_data_retention(notification_types):
    flexible_data_retention = ServiceDataRetention.query.filter(
        ServiceDataRetention.notification_type.in_(notification_types)
    ).all()
    jobs = []
    today = datetime.utcnow().date()
    for f in flexible_data_retention:
        end_date = today - timedelta(days=f.days_of_retention)

        jobs.extend(
            Job.query.join(Template)
            .filter(
                func.coalesce(Job.scheduled_for, Job.created_at) < end_date,
                Job.archived == False,  # noqa
                Template.template_type == f.notification_type,
                Job.service_id == f.service_id,
            )
            .order_by(desc(Job.created_at))
            .all()
        )

    end_date = today - timedelta(days=7)
    for notification_type in notification_types:
        services_with_data_retention = [
            x.service_id for x in flexible_data_retention if x.notification_type == notification_type
        ]
        jobs.extend(
            Job.query.join(Template)
            .filter(
                func.coalesce(Job.scheduled_for, Job.created_at) < end_date,
                Job.archived == False,  # noqa
                Template.template_type == notification_type,
                Job.service_id.notin_(services_with_data_retention),
            )
            .order_by(desc(Job.created_at))
            .all()
        )

    return jobs


@autocommit
def dao_cancel_letter_job(job):
    number_of_notifications_cancelled = Notification.query.filter(Notification.job_id == job.id).update(
        {"status": NOTIFICATION_CANCELLED, "updated_at": datetime.utcnow(), "billable_units": 0}
    )
    job.job_status = JOB_STATUS_CANCELLED
    dao_update_job(job)
    return number_of_notifications_cancelled


def can_letter_job_be_cancelled(job):
    template = dao_get_template_by_id(job.template_id)
    if template.template_type != LETTER_TYPE:
        return False, "Only letter jobs can be cancelled through this endpoint. This is not a letter job."

    notifications = Notification.query.filter(Notification.job_id == job.id).all()
    count_notifications = len(notifications)
    if job.job_status != JOB_STATUS_FINISHED or count_notifications != job.notification_count:
        return False, "We are still processing these letters, please try again in a minute."
    count_cancellable_notifications = len([n for n in notifications if n.status in CANCELLABLE_JOB_LETTER_STATUSES])
    if count_cancellable_notifications != job.notification_count or not letter_can_be_cancelled(
        NOTIFICATION_CREATED, job.created_at
    ):
        return False, "It’s too late to cancel sending, these letters have already been sent."

    return True, None


def find_jobs_with_missing_rows():
    # Jobs can be a maximum of 100,000 rows. It typically takes 10 minutes to create all those notifications.
    # Using 20 minutes as a condition seems reasonable.
    ten_minutes_ago = datetime.utcnow() - timedelta(minutes=20)
    yesterday = datetime.utcnow() - timedelta(days=1)
    jobs_with_rows_missing = (
        db.session.query(Job)
        .filter(
            Job.job_status == JOB_STATUS_FINISHED,
            Job.processing_finished < ten_minutes_ago,
            Job.processing_finished > yesterday,
            Job.id == Notification.job_id,
        )
        .group_by(Job)
        .having(func.count(Notification.id) != Job.notification_count)
    )

    return jobs_with_rows_missing.all()


def find_missing_row_for_job(job_id, job_size):
    expected_row_numbers = db.session.query(func.generate_series(0, job_size - 1).label("row")).subquery()

    query = (
        db.session.query(Notification.job_row_number, expected_row_numbers.c.row.label("missing_row"))
        .outerjoin(
            Notification, and_(expected_row_numbers.c.row == Notification.job_row_number, Notification.job_id == job_id)
        )
        .filter(Notification.job_row_number == None)  # noqa
    )
    return query.all()


redis_cache = RequestCache(redis_store)


@redis_cache.set("job-{job_id}-notification-outcomes", ttl_in_seconds=timedelta(days=1).total_seconds())
def get_possibly_cached_notification_outcomes_for_job(
    job_id: uuid.UUID | str, notification_count: int | None, processing_started: datetime | None
):
    if processing_started is None:
        statuses = []
    elif processing_started.replace(tzinfo=None) < midnight_n_days_ago(3):
        # ft_notification_status table
        statuses = fetch_notification_statuses_for_job(job_id)
    else:
        # notifications table
        statuses = dao_get_notification_outcomes_for_job(job_id)

    return RequestCache.CacheResultWrapper(
        value=[{"status": status.status, "count": status.count} for status in statuses],
        # cache if all rows of the job are accounted for and no
        # notifications are in a state still likely to change
        cache_decision=bool(
            sum(status.count for status in statuses) == notification_count
            and all(status.status in NOTIFICATION_STATUS_TYPES_COMPLETED for status in statuses)
        ),
    )
