from datetime import date, datetime, timedelta

from freezegun import freeze_time

from app.constants import JOB_STATUS_FINISHED, JOB_STATUS_PENDING, LETTER_TYPE
from tests.app.db import (
    create_ft_notification_status,
    create_job,
    create_notification,
    create_template,
)
from tests.conftest import set_config


def create_uploaded_letter(letter_template, service, status="created", created_at=None):
    return create_notification(
        template=letter_template,
        to_field="742 Evergreen Terrace",
        status=status,
        reference="dvla-reference",
        client_reference="file-name",
        one_off=True,
        created_by_id=service.users[0].id,
        created_at=created_at,
    )


def create_precompiled_template(service):
    return create_template(
        service,
        template_type=LETTER_TYPE,
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        content="",
        hidden=True,
        postage="second",
    )


@freeze_time("2020-02-02 14:00")
def test_get_uploads(admin_request, sample_template, mocker):
    letter_template = create_precompiled_template(sample_template.service)

    create_uploaded_letter(
        letter_template,
        sample_template.service,
        status="delivered",
        created_at=datetime.utcnow() - timedelta(minutes=4),
    )
    upload_2 = create_job(
        template=sample_template,
        processing_started=datetime.utcnow() - timedelta(minutes=3),
        job_status=JOB_STATUS_FINISHED,
    )
    create_uploaded_letter(
        letter_template,
        sample_template.service,
        status="delivered",
        created_at=datetime.utcnow() - timedelta(minutes=2),
    )
    upload_4 = create_job(
        template=sample_template,
        processing_started=datetime.utcnow() - timedelta(minutes=1),
        job_status=JOB_STATUS_FINISHED,
    )
    upload_5 = create_job(
        template=sample_template, processing_started=None, job_status=JOB_STATUS_PENDING, notification_count=10
    )

    service_id = sample_template.service.id

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    resp_json = admin_request.get("upload.get_uploads_by_service", service_id=service_id)
    data = resp_json["data"]
    assert len(data) == 4
    assert data[0] == {
        "id": str(upload_5.id),
        "original_file_name": "some.csv",
        "recipient": None,
        "notification_count": 10,
        "template_type": "sms",
        "created_at": upload_5.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "statistics": [],
        "upload_type": "job",
    }
    assert data[1] == {
        "id": None,
        "original_file_name": "Uploaded letters",
        "recipient": None,
        "notification_count": 2,
        "template_type": "letter",
        "created_at": upload_4.created_at.replace(hour=17, minute=30).strftime("%Y-%m-%d %H:%M:%S"),
        "statistics": [],
        "upload_type": "letter_day",
    }
    assert data[3] == {
        "id": str(upload_2.id),
        "original_file_name": "some.csv",
        "recipient": None,
        "notification_count": 1,
        "template_type": "sms",
        "created_at": upload_2.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "statistics": [],
        "upload_type": "job",
    }

    assert mock_redis_get.mock_calls == [
        mocker.call(f"job-{upload_5.id}-notification-outcomes"),
        mocker.call(f"job-{upload_4.id}-notification-outcomes"),
        mocker.call(f"job-{upload_2.id}-notification-outcomes"),
    ]


def test_get_uploads_should_return_statistics(admin_request, sample_template, mocker):
    now = datetime.utcnow()
    earlier = datetime.utcnow() - timedelta(days=1)
    job_1 = create_job(template=sample_template, job_status="pending")
    job_2 = create_job(sample_template, processing_started=earlier)
    for _ in range(3):
        create_notification(template=sample_template, job=job_2, status="created")

    job_3 = create_job(sample_template, processing_started=now)
    for _ in range(4):
        create_notification(template=sample_template, job=job_3, status="sending")

    letter_template = create_precompiled_template(sample_template.service)
    create_uploaded_letter(
        letter_template, sample_template.service, status="delivered", created_at=datetime.utcnow() - timedelta(days=3)
    )

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    resp_json = admin_request.get("upload.get_uploads_by_service", service_id=sample_template.service_id)["data"]
    assert len(resp_json) == 4
    assert resp_json[0]["id"] == str(job_1.id)
    assert resp_json[0]["statistics"] == []
    assert resp_json[1]["id"] == str(job_3.id)
    assert resp_json[1]["statistics"] == [{"status": "sending", "count": 4}]
    assert resp_json[2]["id"] == str(job_2.id)
    assert resp_json[2]["statistics"] == [{"status": "created", "count": 3}]
    assert resp_json[3]["id"] is None
    assert resp_json[3]["statistics"] == []

    assert mock_redis_get.mock_calls == [
        mocker.call(f"job-{job_1.id}-notification-outcomes"),
        mocker.call(f"job-{job_3.id}-notification-outcomes"),
        mocker.call(f"job-{job_2.id}-notification-outcomes"),
    ]


def test_get_uploads_should_paginate(admin_request, sample_template, mocker):
    for _ in range(10):
        create_job(sample_template)

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    with set_config(admin_request.app, "PAGE_SIZE", 2):
        resp_json = admin_request.get("upload.get_uploads_by_service", service_id=sample_template.service_id)

    assert len(resp_json["data"]) == 2
    assert resp_json["page_size"] == 2
    assert resp_json["total"] == 10
    assert "links" in resp_json
    assert set(resp_json["links"].keys()) == {"next", "last"}

    assert mock_redis_get.mock_calls == [
        mocker.call(f'job-{resp_json["data"][0]["id"]}-notification-outcomes'),
        mocker.call(f'job-{resp_json["data"][1]["id"]}-notification-outcomes'),
    ]


def test_get_uploads_accepts_page_parameter(admin_request, sample_template, mocker):
    for _ in range(10):
        create_job(sample_template)

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    with set_config(admin_request.app, "PAGE_SIZE", 2):
        resp_json = admin_request.get("upload.get_uploads_by_service", service_id=sample_template.service_id, page=2)

    assert len(resp_json["data"]) == 2
    assert resp_json["page_size"] == 2
    assert resp_json["total"] == 10
    assert "links" in resp_json
    assert set(resp_json["links"].keys()) == {"prev", "next", "last"}

    assert mock_redis_get.mock_calls == [
        mocker.call(f'job-{resp_json["data"][0]["id"]}-notification-outcomes'),
        mocker.call(f'job-{resp_json["data"][1]["id"]}-notification-outcomes'),
    ]


@freeze_time("2017-06-10 12:00")
def test_get_uploads_should_retrieve_from_ft_notification_status_for_old_jobs(admin_request, sample_template, mocker):
    # it's the 10th today, so 3 days should include all of 7th, 8th, 9th, and some of 10th.
    just_three_days_ago = datetime(2017, 6, 6, 22, 59, 59)
    not_quite_three_days_ago = just_three_days_ago + timedelta(seconds=1)

    job_1 = create_job(sample_template, created_at=just_three_days_ago, processing_started=just_three_days_ago)
    job_2 = create_job(sample_template, created_at=just_three_days_ago, processing_started=not_quite_three_days_ago)
    # is old but hasn't started yet (probably a scheduled job). We don't have any stats for this job yet.
    job_3 = create_job(sample_template, created_at=just_three_days_ago, processing_started=None)

    # some notifications created more than three days ago, some created after the midnight cutoff
    create_ft_notification_status(date(2017, 6, 6), job=job_1, notification_status="delivered", count=2)
    create_ft_notification_status(date(2017, 6, 7), job=job_1, notification_status="delivered", count=4)
    # job2's new enough
    create_notification(job=job_2, status="created", created_at=not_quite_three_days_ago)

    # this isn't picked up because the job is too new
    create_ft_notification_status(date(2017, 6, 7), job=job_2, notification_status="delivered", count=8)
    # this isn't picked up - while the job is old, it started in last 3 days so we look at notification table instead
    create_ft_notification_status(date(2017, 6, 7), job=job_3, notification_status="delivered", count=16)

    # this isn't picked up because we're using the ft status table for job_1 as it's old
    create_notification(job=job_1, status="created", created_at=not_quite_three_days_ago)

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    resp_json = admin_request.get("upload.get_uploads_by_service", service_id=sample_template.service_id)["data"]

    assert resp_json[0]["id"] == str(job_3.id)
    assert resp_json[0]["statistics"] == []
    assert resp_json[1]["id"] == str(job_2.id)
    assert resp_json[1]["statistics"] == [{"status": "created", "count": 1}]
    assert resp_json[2]["id"] == str(job_1.id)
    assert resp_json[2]["statistics"] == [{"status": "delivered", "count": 6}]

    assert mock_redis_get.mock_calls == [
        mocker.call(f'job-{resp_json[0]["id"]}-notification-outcomes'),
        mocker.call(f'job-{resp_json[1]["id"]}-notification-outcomes'),
        mocker.call(f'job-{resp_json[2]["id"]}-notification-outcomes'),
    ]


@freeze_time("2020-02-02 14:00")
def test_get_uploaded_letters_by_print_date(admin_request, sample_template):
    letter_template = create_precompiled_template(sample_template.service)

    letter_1 = create_uploaded_letter(
        letter_template,
        sample_template.service,
        status="delivered",
        created_at=datetime.utcnow() - timedelta(minutes=1),
    )
    letter_2 = create_uploaded_letter(
        letter_template,
        sample_template.service,
        status="delivered",
        created_at=datetime.utcnow() - timedelta(minutes=2),
    )

    service_id = sample_template.service.id

    resp_json = admin_request.get(
        "upload.get_uploaded_letter_by_service_and_print_day",
        service_id=service_id,
        letter_print_date="2020-02-02",
    )
    assert resp_json["total"] == 2
    assert resp_json["page_size"] == 50
    assert len(resp_json["notifications"]) == 2

    assert resp_json["notifications"][0]["id"] == str(letter_1.id)
    assert resp_json["notifications"][0]["created_at"] == letter_1.created_at.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    assert resp_json["notifications"][1]["id"] == str(letter_2.id)
    assert resp_json["notifications"][1]["created_at"] == letter_2.created_at.strftime("%Y-%m-%dT%H:%M:%S+00:00")


@freeze_time("2020-02-02 14:00")
def test_get_uploaded_letters_by_print_date_paginates(admin_request, sample_template):
    letter_template = create_precompiled_template(sample_template.service)

    for _ in range(101):
        create_uploaded_letter(
            letter_template,
            sample_template.service,
            status="delivered",
            created_at=datetime.utcnow() - timedelta(minutes=1),
        )

    service_id = sample_template.service.id

    resp_json = admin_request.get(
        "upload.get_uploaded_letter_by_service_and_print_day",
        service_id=service_id,
        letter_print_date="2020-02-02",
        page=2,
    )
    assert resp_json["total"] == 101
    assert resp_json["page_size"] == 50
    assert len(resp_json["notifications"]) == 50
    assert resp_json["links"]["prev"] == (f"/service/{service_id}/upload/uploaded-letters/2020-02-02?page=1")
    assert resp_json["links"]["next"] == (f"/service/{service_id}/upload/uploaded-letters/2020-02-02?page=3")


def test_get_uploaded_letters_by_print_date_404s_for_bad_date(
    admin_request,
    sample_service,
):
    admin_request.get(
        "upload.get_uploaded_letter_by_service_and_print_day",
        service_id=sample_service.id,
        letter_print_date="foo",
        _expected_status=400,
    )
