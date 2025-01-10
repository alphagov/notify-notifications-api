import json
import uuid
from collections import Counter
from datetime import datetime, timedelta
from functools import partial

import pytest
from freezegun import freeze_time
from sqlalchemy.exc import IntegrityError

from app.constants import EMAIL_TYPE, JOB_STATUS_FINISHED, LETTER_TYPE, SMS_TYPE
from app.dao.jobs_dao import (
    can_letter_job_be_cancelled,
    dao_cancel_letter_job,
    dao_create_job,
    dao_get_job_by_service_id_and_job_id,
    dao_get_jobs_by_service_id,
    dao_get_jobs_older_than_data_retention,
    dao_get_notification_outcomes_for_job,
    dao_get_scheduled_job_by_id_and_service_id,
    dao_set_scheduled_jobs_to_pending,
    dao_update_job,
    find_jobs_with_missing_rows,
    find_missing_row_for_job,
    get_possibly_cached_notification_outcomes_for_job,
)
from app.models import Job
from tests.app.db import (
    create_ft_notification_status,
    create_job,
    create_notification,
    create_service,
    create_service_contact_list,
    create_template,
)


def test_should_count_of_statuses_for_notifications_associated_with_job(sample_template, sample_job):
    create_notification(sample_template, job=sample_job, status="created")
    create_notification(sample_template, job=sample_job, status="created")
    create_notification(sample_template, job=sample_job, status="created")
    create_notification(sample_template, job=sample_job, status="sending")
    create_notification(sample_template, job=sample_job, status="delivered")

    results = dao_get_notification_outcomes_for_job(sample_job.id)
    assert {row.status: row.count for row in results} == {
        "created": 3,
        "sending": 1,
        "delivered": 1,
    }


def test_should_return_zero_length_array_if_no_notifications_for_job(sample_service, sample_job):
    assert len(dao_get_notification_outcomes_for_job(sample_service.id)) == 0


def test_should_return_notifications_only_for_this_job(sample_template):
    job_1 = create_job(sample_template)
    job_2 = create_job(sample_template)

    create_notification(sample_template, job=job_1, status="created")
    create_notification(sample_template, job=job_2, status="sent")

    results = dao_get_notification_outcomes_for_job(job_1.id)
    assert {row.status: row.count for row in results} == {"created": 1}


def test_create_sample_job(sample_template):
    assert Job.query.count() == 0

    job_id = uuid.uuid4()
    data = {
        "id": job_id,
        "service_id": sample_template.service.id,
        "template_id": sample_template.id,
        "template_version": sample_template.version,
        "original_file_name": "some.csv",
        "notification_count": 1,
        "created_by": sample_template.created_by,
    }

    job = Job(**data)
    dao_create_job(job)

    assert Job.query.count() == 1
    job_from_db = Job.query.get(job_id)
    assert job == job_from_db
    assert job_from_db.notifications_delivered == 0
    assert job_from_db.notifications_failed == 0


def test_get_job_by_id(sample_job):
    job_from_db = dao_get_job_by_service_id_and_job_id(sample_job.service.id, sample_job.id)
    assert sample_job == job_from_db


def test_get_jobs_for_service(sample_template):
    one_job = create_job(sample_template)

    other_service = create_service(service_name="other service")
    other_template = create_template(service=other_service)
    other_job = create_job(other_template)

    one_job_from_db = dao_get_jobs_by_service_id(one_job.service_id).items
    other_job_from_db = dao_get_jobs_by_service_id(other_job.service_id).items

    assert len(one_job_from_db) == 1
    assert one_job == one_job_from_db[0]

    assert len(other_job_from_db) == 1
    assert other_job == other_job_from_db[0]

    assert one_job_from_db != other_job_from_db


def test_get_jobs_for_service_with_limit_days_param(sample_template):
    one_job = create_job(sample_template)
    old_job = create_job(sample_template, created_at=datetime.now() - timedelta(days=8))

    jobs = dao_get_jobs_by_service_id(one_job.service_id).items

    assert len(jobs) == 2
    assert one_job in jobs
    assert old_job in jobs

    jobs_limit_days = dao_get_jobs_by_service_id(one_job.service_id, limit_days=7).items
    assert len(jobs_limit_days) == 1
    assert one_job in jobs_limit_days
    assert old_job not in jobs_limit_days


@freeze_time("2017-06-10")
def test_get_jobs_for_service_with_limit_days_edge_case(sample_template):
    one_job = create_job(sample_template)
    just_after_midnight_job = create_job(sample_template, created_at=datetime(2017, 6, 2, 23, 0, 1))
    just_before_midnight_job = create_job(sample_template, created_at=datetime(2017, 6, 2, 22, 59, 0))

    jobs_limit_days = dao_get_jobs_by_service_id(one_job.service_id, limit_days=7).items
    assert len(jobs_limit_days) == 2
    assert one_job in jobs_limit_days
    assert just_after_midnight_job in jobs_limit_days
    assert just_before_midnight_job not in jobs_limit_days


def test_get_jobs_for_service_in_processed_at_then_created_at_order(notify_db_session, sample_template):
    from_hour = partial(datetime, 2001, 1, 1)

    created_jobs = [
        create_job(sample_template, created_at=from_hour(2), processing_started=None),
        create_job(sample_template, created_at=from_hour(1), processing_started=None),
        create_job(sample_template, created_at=from_hour(1), processing_started=from_hour(4)),
        create_job(sample_template, created_at=from_hour(2), processing_started=from_hour(3)),
    ]

    jobs = dao_get_jobs_by_service_id(sample_template.service.id).items

    assert len(jobs) == len(created_jobs)

    for index in range(len(created_jobs)):
        assert jobs[index].id == created_jobs[index].id


def test_get_jobs_for_service_by_contact_list(sample_template):
    contact_list = create_service_contact_list()
    job_1 = create_job(sample_template)
    job_2 = create_job(sample_template, contact_list_id=contact_list.id)

    assert dao_get_jobs_by_service_id(sample_template.service.id).items == [
        job_2,
        job_1,
    ]

    assert dao_get_jobs_by_service_id(
        sample_template.service.id,
        contact_list_id=contact_list.id,
    ).items == [
        job_2,
    ]


def test_update_job(sample_job):
    assert sample_job.job_status == "pending"

    sample_job.job_status = "in progress"

    dao_update_job(sample_job)

    job_from_db = Job.query.get(sample_job.id)

    assert job_from_db.job_status == "in progress"


def test_set_scheduled_jobs_to_pending_gets_all_jobs_in_scheduled_state_before_now(sample_template):
    one_minute_ago = datetime.utcnow() - timedelta(minutes=1)
    one_hour_ago = datetime.utcnow() - timedelta(minutes=60)
    job_new = create_job(sample_template, scheduled_for=one_minute_ago, job_status="scheduled")
    job_old = create_job(sample_template, scheduled_for=one_hour_ago, job_status="scheduled")
    jobs = dao_set_scheduled_jobs_to_pending()
    assert len(jobs) == 2
    assert jobs[0].id == job_old.id
    assert jobs[1].id == job_new.id


def test_set_scheduled_jobs_to_pending_gets_ignores_jobs_not_scheduled(sample_template, sample_job):
    one_minute_ago = datetime.utcnow() - timedelta(minutes=1)
    job_scheduled = create_job(sample_template, scheduled_for=one_minute_ago, job_status="scheduled")
    jobs = dao_set_scheduled_jobs_to_pending()
    assert len(jobs) == 1
    assert jobs[0].id == job_scheduled.id


def test_set_scheduled_jobs_to_pending_gets_ignores_jobs_scheduled_in_the_future(sample_scheduled_job):
    jobs = dao_set_scheduled_jobs_to_pending()
    assert len(jobs) == 0


def test_set_scheduled_jobs_to_pending_updates_rows(sample_template):
    one_minute_ago = datetime.utcnow() - timedelta(minutes=1)
    one_hour_ago = datetime.utcnow() - timedelta(minutes=60)
    create_job(sample_template, scheduled_for=one_minute_ago, job_status="scheduled")
    create_job(sample_template, scheduled_for=one_hour_ago, job_status="scheduled")
    jobs = dao_set_scheduled_jobs_to_pending()
    assert len(jobs) == 2
    assert jobs[0].job_status == "pending"
    assert jobs[1].job_status == "pending"


def test_get_scheduled_job_gets_a_job_yet_to_send(sample_scheduled_job):
    result = dao_get_scheduled_job_by_id_and_service_id(sample_scheduled_job.id, sample_scheduled_job.service_id)
    assert result.id == sample_scheduled_job.id


def test_get_scheduled_job_gets_a_job_yet_to_send_even_if_scheduled_to_send_in_the_past(sample_scheduled_job):
    sample_scheduled_job.scheduled_for = datetime.now() - timedelta(hours=1)
    result = dao_get_scheduled_job_by_id_and_service_id(sample_scheduled_job.id, sample_scheduled_job.service_id)
    assert result.id == sample_scheduled_job.id


@freeze_time("2016-10-31 10:00:00")
def test_should_get_jobs_seven_days_old(sample_template):
    """
    Jobs older than seven days are deleted, but only two day's worth (two-day window)
    """
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    within_seven_days = seven_days_ago + timedelta(seconds=1)

    eight_days_ago = seven_days_ago - timedelta(days=1)

    nine_days_ago = eight_days_ago - timedelta(days=2)
    nine_days_one_second_ago = nine_days_ago - timedelta(seconds=1)

    create_job(sample_template, created_at=seven_days_ago)
    create_job(sample_template, created_at=within_seven_days)
    job_to_delete = create_job(sample_template, created_at=eight_days_ago)
    create_job(sample_template, created_at=nine_days_ago, archived=True)
    create_job(sample_template, created_at=nine_days_one_second_ago, archived=True)

    jobs = dao_get_jobs_older_than_data_retention(notification_types=[sample_template.template_type])

    assert len(jobs) == 1
    assert jobs[0].id == job_to_delete.id


def test_get_jobs_for_service_is_paginated(notify_db_session, sample_service, sample_template):
    with freeze_time("2015-01-01T00:00:00") as the_time:
        for _ in range(10):
            the_time.tick(timedelta(hours=1))
            create_job(sample_template)

    res = dao_get_jobs_by_service_id(sample_service.id, page=1, page_size=2)

    assert res.per_page == 2
    assert res.total == 10
    assert len(res.items) == 2
    assert res.items[0].created_at == datetime(2015, 1, 1, 10)
    assert res.items[1].created_at == datetime(2015, 1, 1, 9)

    res = dao_get_jobs_by_service_id(sample_service.id, page=2, page_size=2)

    assert len(res.items) == 2
    assert res.items[0].created_at == datetime(2015, 1, 1, 8)
    assert res.items[1].created_at == datetime(2015, 1, 1, 7)


@pytest.mark.parametrize(
    "file_name",
    [
        "Test message",
        "Report",
    ],
)
def test_get_jobs_for_service_doesnt_return_test_messages(
    sample_template,
    sample_job,
    file_name,
):
    create_job(
        sample_template,
        original_file_name=file_name,
    )

    jobs = dao_get_jobs_by_service_id(sample_job.service_id).items

    assert jobs == [sample_job]


@freeze_time("2016-10-31 10:00:00")
def test_should_get_jobs_seven_days_old_filters_type(sample_service):
    eight_days_ago = datetime.utcnow() - timedelta(days=8)
    letter_template = create_template(sample_service, template_type=LETTER_TYPE)
    sms_template = create_template(sample_service, template_type=SMS_TYPE)
    email_template = create_template(sample_service, template_type=EMAIL_TYPE)

    job_to_remain = create_job(letter_template, created_at=eight_days_ago)
    create_job(sms_template, created_at=eight_days_ago)
    create_job(email_template, created_at=eight_days_ago)

    jobs = dao_get_jobs_older_than_data_retention(notification_types=[EMAIL_TYPE, SMS_TYPE])

    assert len(jobs) == 2
    assert job_to_remain.id not in [job.id for job in jobs]


@freeze_time("2016-10-31 10:00:00")
def test_should_get_jobs_seven_days_old_by_scheduled_for_date(sample_service):
    six_days_ago = datetime.utcnow() - timedelta(days=6)
    eight_days_ago = datetime.utcnow() - timedelta(days=8)
    letter_template = create_template(sample_service, template_type=LETTER_TYPE)

    create_job(letter_template, created_at=eight_days_ago)
    create_job(letter_template, created_at=eight_days_ago, scheduled_for=eight_days_ago)
    job_to_remain = create_job(letter_template, created_at=eight_days_ago, scheduled_for=six_days_ago)

    jobs = dao_get_jobs_older_than_data_retention(notification_types=[LETTER_TYPE])

    assert len(jobs) == 2
    assert job_to_remain.id not in [job.id for job in jobs]


def assert_job_stat(job, result, sent, delivered, failed):
    assert result.job_id == job.id
    assert result.original_file_name == job.original_file_name
    assert result.created_at == job.created_at
    assert result.scheduled_for == job.scheduled_for
    assert result.template_id == job.template_id
    assert result.template_version == job.template_version
    assert result.job_status == job.job_status
    assert result.service_id == job.service_id
    assert result.notification_count == job.notification_count
    assert result.sent == sent
    assert result.delivered == delivered
    assert result.failed == failed


@freeze_time("2019-06-13 13:00")
def test_dao_cancel_letter_job_cancels_job_and_returns_number_of_cancelled_notifications(sample_letter_template):
    job = create_job(template=sample_letter_template, notification_count=1, job_status="finished")
    notification = create_notification(template=job.template, job=job, status="created")
    result = dao_cancel_letter_job(job)
    assert result == 1
    assert notification.status == "cancelled"
    assert job.job_status == "cancelled"


@freeze_time("2019-06-13 13:00")
def test_can_letter_job_be_cancelled_returns_true_if_job_can_be_cancelled(sample_letter_template):
    job = create_job(template=sample_letter_template, notification_count=1, job_status="finished")
    create_notification(template=job.template, job=job, status="created")
    result, errors = can_letter_job_be_cancelled(job)
    assert result
    assert not errors


@freeze_time("2019-06-13 13:00")
def test_can_letter_job_be_cancelled_returns_false_and_error_message_if_notification_status_sending(
    sample_letter_template,
):
    job = create_job(template=sample_letter_template, notification_count=2, job_status="finished")
    create_notification(template=job.template, job=job, status="sending")
    create_notification(template=job.template, job=job, status="created")
    result, errors = can_letter_job_be_cancelled(job)
    assert not result
    assert errors == "It’s too late to cancel sending, these letters have already been sent."


def test_can_letter_job_be_cancelled_returns_false_and_error_message_if_letters_already_sent_to_dvla(
    sample_letter_template,
):
    with freeze_time("2019-06-13 13:00"):
        job = create_job(template=sample_letter_template, notification_count=1, job_status="finished")
        letter = create_notification(template=job.template, job=job, status="created")

    with freeze_time("2019-06-13 17:32"):
        result, errors = can_letter_job_be_cancelled(job)
    assert not result
    assert errors == "It’s too late to cancel sending, these letters have already been sent."
    assert letter.status == "created"
    assert job.job_status == "finished"


@freeze_time("2019-06-13 13:00")
def test_can_letter_job_be_cancelled_returns_false_and_error_message_if_not_a_letter_job(sample_template):
    job = create_job(template=sample_template, notification_count=1, job_status="finished")
    create_notification(template=job.template, job=job, status="created")
    result, errors = can_letter_job_be_cancelled(job)
    assert not result
    assert errors == "Only letter jobs can be cancelled through this endpoint. This is not a letter job."


@freeze_time("2019-06-13 13:00")
def test_can_letter_job_be_cancelled_returns_false_and_error_message_if_job_not_finished(sample_letter_template):
    job = create_job(template=sample_letter_template, notification_count=1, job_status="in progress")
    create_notification(template=job.template, job=job, status="created")
    result, errors = can_letter_job_be_cancelled(job)
    assert not result
    assert errors == "We are still processing these letters, please try again in a minute."


@freeze_time("2019-06-13 13:00")
def test_can_letter_job_be_cancelled_returns_false_and_error_message_if_notifications_not_in_db_yet(
    sample_letter_template,
):
    job = create_job(template=sample_letter_template, notification_count=2, job_status="finished")
    create_notification(template=job.template, job=job, status="created")
    result, errors = can_letter_job_be_cancelled(job)
    assert not result
    assert errors == "We are still processing these letters, please try again in a minute."


def test_can_letter_job_be_cancelled_respects_bst(sample_letter_template):
    job = create_job(template=sample_letter_template, created_at=datetime(2020, 4, 9, 23, 30), job_status="finished")
    create_notification(template=job.template, job=job, status="created", created_at=datetime(2020, 4, 9, 23, 32))

    with freeze_time("2020-04-10 10:00"):
        result, errors = can_letter_job_be_cancelled(job)

    assert not errors
    assert result


def test_find_jobs_with_missing_rows(sample_email_template):
    healthy_job = create_job(
        template=sample_email_template,
        notification_count=3,
        job_status=JOB_STATUS_FINISHED,
        processing_finished=datetime.utcnow() - timedelta(minutes=20),
    )
    for i in range(3):
        create_notification(job=healthy_job, job_row_number=i)
    job_with_missing_rows = create_job(
        template=sample_email_template,
        notification_count=5,
        job_status=JOB_STATUS_FINISHED,
        processing_finished=datetime.utcnow() - timedelta(minutes=20),
    )
    for i in range(4):
        create_notification(job=job_with_missing_rows, job_row_number=i)

    results = find_jobs_with_missing_rows()

    assert len(results) == 1
    assert results[0] == job_with_missing_rows


def test_find_jobs_with_missing_rows_returns_nothing_for_a_job_completed_less_than_10_minutes_ago(
    sample_email_template,
):
    job = create_job(
        template=sample_email_template,
        notification_count=5,
        job_status=JOB_STATUS_FINISHED,
        processing_finished=datetime.utcnow() - timedelta(minutes=9),
    )
    for i in range(4):
        create_notification(job=job, job_row_number=i)

    results = find_jobs_with_missing_rows()

    assert len(results) == 0


def test_find_jobs_with_missing_rows_returns_nothing_for_a_job_completed_more_that_a_day_ago(sample_email_template):
    job = create_job(
        template=sample_email_template,
        notification_count=5,
        job_status=JOB_STATUS_FINISHED,
        processing_finished=datetime.utcnow() - timedelta(days=1),
    )
    for i in range(4):
        create_notification(job=job, job_row_number=i)

    results = find_jobs_with_missing_rows()

    assert len(results) == 0


@pytest.mark.parametrize("status", ["pending", "in progress", "cancelled", "scheduled"])
def test_find_jobs_with_missing_rows_doesnt_return_jobs_that_are_not_finished(sample_email_template, status):
    job = create_job(
        template=sample_email_template,
        notification_count=5,
        job_status=status,
        processing_finished=datetime.utcnow() - timedelta(minutes=11),
    )
    for i in range(4):
        create_notification(job=job, job_row_number=i)

    results = find_jobs_with_missing_rows()

    assert len(results) == 0


def test_find_missing_row_for_job(sample_email_template):
    job = create_job(
        template=sample_email_template,
        notification_count=5,
        job_status=JOB_STATUS_FINISHED,
        processing_finished=datetime.utcnow() - timedelta(minutes=11),
    )
    create_notification(job=job, job_row_number=0)
    create_notification(job=job, job_row_number=1)
    create_notification(job=job, job_row_number=3)
    create_notification(job=job, job_row_number=4)

    results = find_missing_row_for_job(job.id, 5)
    assert len(results) == 1
    assert results[0].missing_row == 2


def test_find_missing_row_for_job_more_than_one_missing_row(sample_email_template):
    job = create_job(
        template=sample_email_template,
        notification_count=5,
        job_status=JOB_STATUS_FINISHED,
        processing_finished=datetime.utcnow() - timedelta(minutes=11),
    )
    create_notification(job=job, job_row_number=0)
    create_notification(job=job, job_row_number=1)
    create_notification(job=job, job_row_number=4)

    results = find_missing_row_for_job(job.id, 5)
    assert len(results) == 2
    assert results[0].missing_row == 2
    assert results[1].missing_row == 3


def test_find_missing_row_for_job_return_none_when_row_isnt_missing(sample_email_template):
    job = create_job(
        template=sample_email_template,
        notification_count=5,
        job_status=JOB_STATUS_FINISHED,
        processing_finished=datetime.utcnow() - timedelta(minutes=11),
    )
    for i in range(5):
        create_notification(job=job, job_row_number=i)

    results = find_missing_row_for_job(job.id, 5)
    assert len(results) == 0


def test_unique_key_on_job_id_and_job_row_number(sample_email_template):
    job = create_job(template=sample_email_template)
    create_notification(job=job, job_row_number=0)
    with pytest.raises(expected_exception=IntegrityError):
        create_notification(job=job, job_row_number=0)


def test_unique_key_on_job_id_and_job_row_number_no_error_if_row_number_for_different_job(sample_email_template):
    job_1 = create_job(template=sample_email_template)
    job_2 = create_job(template=sample_email_template)
    create_notification(job=job_1, job_row_number=0)
    create_notification(job=job_2, job_row_number=0)


@pytest.mark.parametrize(
    [
        "notification_statuses",
        "notification_count",
        "processing_started",
        "create_notifications",
        "expect_redis_set",
        "expected_retval",
    ],
    (
        (
            (
                "permanent-failure",
                "delivered",
                "permanent-failure",
                "technical-failure",
                "technical-failure",
                "permanent-failure",
            ),
            6,
            datetime.fromisoformat("2020-02-10T09:00:00"),
            True,
            True,
            [
                {"status": "delivered", "count": 1},
                {"status": "permanent-failure", "count": 3},
                {"status": "technical-failure", "count": 2},
            ],
        ),
        (
            (
                "sent",
                "delivered",
                "delivered",
                "delivered",
            ),
            4,
            datetime.fromisoformat("2020-02-09T09:00:00"),
            True,
            True,
            [
                {"status": "delivered", "count": 3},
                {"status": "sent", "count": 1},
            ],
        ),
        (
            (
                "technical-failure",
                "delivered",
                "technical-failure",
            ),
            3,
            datetime.fromisoformat("2020-02-04T09:00:00"),  # so from ft_notification_status
            False,
            True,
            [
                {"status": "technical-failure", "count": 2},
                {"status": "delivered", "count": 1},
            ],
        ),
        (
            (
                "technical-failure",
                "delivered",
                "created",
                "technical-failure",
            ),
            4,
            datetime.fromisoformat("2020-02-09T23:59:58"),
            True,
            False,  # because non-complete status
            [
                {"status": "technical-failure", "count": 2},
                {"status": "created", "count": 1},
                {"status": "delivered", "count": 1},
            ],
        ),
        (
            (
                "sent",
                "delivered",
                "delivered",
            ),
            4,
            datetime.fromisoformat("2020-02-10T09:00:00"),
            True,
            False,  # because missing rows
            [
                {"status": "delivered", "count": 2},
                {"status": "sent", "count": 1},
            ],
        ),
        (
            (
                "delivered",
                "created",
                "delivered",
            ),
            3,
            None,
            True,
            False,  # because non-complete status
            [],
        ),
    ),
)
def test_get_possibly_cached_notification_outcomes_for_job_empty_cache(
    sample_email_template,
    notification_statuses,
    notification_count,
    processing_started,
    create_notifications,
    expect_redis_set,
    expected_retval,
    mocker,
):
    call_datetime = datetime.fromisoformat("2020-02-10T10:00:00")
    ft_status_bins = Counter()

    with freeze_time(processing_started) as frozen_time:
        job = create_job(template=sample_email_template, processing_started=processing_started)
        for i, status in enumerate(notification_statuses):
            frozen_time.tick()

            if create_notifications:
                create_notification(job=job, status=status, job_row_number=i)

            d = datetime.now().date()
            if d != call_datetime.date():
                ft_status_bins[d, status] += 1

    for (d, status), count in ft_status_bins.items():
        create_ft_notification_status(
            bst_date=d,
            notification_type="email",
            service=job.service,
            job=job,
            template=job.template,
            key_type="normal",
            notification_status=status,
            count=count,
        )

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mock_redis_set = mocker.patch("app.redis_store.set", return_value=None)

    with freeze_time(call_datetime):
        retval = get_possibly_cached_notification_outcomes_for_job(job.id, notification_count, processing_started)

    assert sorted(retval, key=lambda x: x["status"]) == sorted(expected_retval, key=lambda x: x["status"])

    if expect_redis_set:
        assert mock_redis_set.mock_calls == [
            mocker.call(
                f"job-{job.id}-notification-outcomes", json.dumps(retval), ex=timedelta(days=1).total_seconds()
            ),
        ]
    else:
        assert not mock_redis_set.mock_calls

    assert mock_redis_get.mock_calls == [
        mocker.call(f"job-{job.id}-notification-outcomes"),
    ]


def test_get_possibly_cached_notification_outcomes_for_job_present_cache(
    fake_uuid,
    mocker,
):
    mocker.patch(
        "app.dao.jobs_dao.fetch_notification_statuses_for_job",
        side_effect=AssertionError("fetch_notification_statuses_for_job call not expected"),
    )
    mocker.patch(
        "app.dao.jobs_dao.dao_get_notification_outcomes_for_job",
        side_effect=AssertionError("dao_get_notification_outcomes_for_job call not expected"),
    )

    mock_redis_get = mocker.patch(
        "app.redis_store.get", return_value=b'[{"status": "delivered", "count": 12}, {"status": "sent", "count": 34}]'
    )
    mocker.patch("app.redis_store.set", return_value=AssertionError("redis set call not expected"))

    retval = get_possibly_cached_notification_outcomes_for_job(fake_uuid, 46, datetime.now())

    assert retval == [{"status": "delivered", "count": 12}, {"status": "sent", "count": 34}]

    assert mock_redis_get.mock_calls == [
        mocker.call(f"job-{fake_uuid}-notification-outcomes"),
    ]
