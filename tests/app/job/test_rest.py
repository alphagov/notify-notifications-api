import json
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import ANY

import pytest
import pytz
from freezegun import freeze_time

import app.celery.tasks
from app.celery.tasks import process_job
from app.constants import JOB_STATUS_PENDING, JOB_STATUS_TYPES
from app.dao.templates_dao import dao_update_template
from tests import create_admin_authorization_header
from tests.app.db import (
    create_ft_notification_status,
    create_job,
    create_notification,
    create_service,
    create_service_contact_list,
    create_template,
)
from tests.conftest import set_config


def test_get_job_with_invalid_service_id_returns404(client, sample_service):
    path = f"/service/{sample_service.id}/job"
    auth_header = create_admin_authorization_header()
    response = client.get(path, headers=[auth_header])
    assert response.status_code == 200
    resp_json = json.loads(response.get_data(as_text=True))
    assert len(resp_json["data"]) == 0


def test_get_job_with_invalid_job_id_returns404(client, sample_template):
    service_id = sample_template.service.id
    path = "/service/{}/job/{}".format(service_id, "bad-id")
    auth_header = create_admin_authorization_header()
    response = client.get(path, headers=[auth_header])
    assert response.status_code == 404
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["result"] == "error"
    assert resp_json["message"] == "No result found"


def test_get_job_with_unknown_id_returns404(client, sample_template, fake_uuid):
    service_id = sample_template.service.id
    path = f"/service/{service_id}/job/{fake_uuid}"
    auth_header = create_admin_authorization_header()
    response = client.get(path, headers=[auth_header])
    assert response.status_code == 404
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json == {"message": "No result found", "result": "error"}


def test_cancel_job(client, sample_scheduled_job):
    job_id = str(sample_scheduled_job.id)
    service_id = sample_scheduled_job.service.id
    path = f"/service/{service_id}/job/{job_id}/cancel"
    auth_header = create_admin_authorization_header()
    response = client.post(path, headers=[auth_header])
    assert response.status_code == 200
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["data"]["id"] == job_id
    assert resp_json["data"]["job_status"] == "cancelled"


def test_cant_cancel_normal_job(client, sample_job, mocker):
    job_id = str(sample_job.id)
    service_id = sample_job.service.id
    mock_update = mocker.patch("app.dao.jobs_dao.dao_update_job")
    path = f"/service/{service_id}/job/{job_id}/cancel"
    auth_header = create_admin_authorization_header()
    response = client.post(path, headers=[auth_header])
    assert response.status_code == 404
    assert mock_update.call_count == 0


@freeze_time("2019-06-13 13:00")
def test_cancel_letter_job_updates_notifications_and_job_to_cancelled(sample_letter_template, admin_request, mocker):
    job = create_job(template=sample_letter_template, notification_count=1, job_status="finished")
    create_notification(template=job.template, job=job, status="created")

    mock_get_job = mocker.patch("app.job.rest.dao_get_job_by_service_id_and_job_id", return_value=job)
    mock_can_letter_job_be_cancelled = mocker.patch(
        "app.job.rest.can_letter_job_be_cancelled", return_value=(True, None)
    )
    mock_dao_cancel_letter_job = mocker.patch("app.job.rest.dao_cancel_letter_job", return_value=1)
    mock_update_redis = mocker.patch("app.job.rest.adjust_daily_service_limits_for_cancelled_letters")

    response = admin_request.post(
        "job.cancel_letter_job",
        service_id=job.service_id,
        job_id=job.id,
    )

    mock_get_job.assert_called_once_with(job.service_id, str(job.id))
    mock_can_letter_job_be_cancelled.assert_called_once_with(job)
    mock_dao_cancel_letter_job.assert_called_once_with(job)
    mock_update_redis.assert_called_once_with(job.service_id, 1, datetime(2019, 6, 13, 13, 0))

    assert response == 1


@freeze_time("2019-06-13 13:00")
def test_cancel_letter_job_does_not_call_cancel_if_can_letter_job_be_cancelled_returns_False(
    sample_letter_template, admin_request, mocker
):
    job = create_job(template=sample_letter_template, notification_count=2, job_status="finished")
    create_notification(template=job.template, job=job, status="sending")
    create_notification(template=job.template, job=job, status="created")

    mock_get_job = mocker.patch("app.job.rest.dao_get_job_by_service_id_and_job_id", return_value=job)
    error_message = "Sorry, it's too late, letters have already been sent."
    mock_can_letter_job_be_cancelled = mocker.patch(
        "app.job.rest.can_letter_job_be_cancelled", return_value=(False, error_message)
    )
    mock_dao_cancel_letter_job = mocker.patch("app.job.rest.dao_cancel_letter_job")
    mock_update_redis = mocker.patch("app.job.rest.adjust_daily_service_limits_for_cancelled_letters")

    response = admin_request.post(
        "job.cancel_letter_job", service_id=job.service_id, job_id=job.id, _expected_status=400
    )

    mock_get_job.assert_called_once_with(job.service_id, str(job.id))
    mock_can_letter_job_be_cancelled.assert_called_once_with(job)
    assert not mock_update_redis.called
    assert mock_dao_cancel_letter_job.call_count == 0

    assert response["message"] == "Sorry, it's too late, letters have already been sent."


def test_create_unscheduled_job(client, sample_template, mocker, mock_celery_task, fake_uuid):
    mock_celery_task(process_job)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_template.id),
            "original_file_name": "thisisatest.csv",
            "notification_count": "1",
            "valid": "True",
        },
    )
    data = {
        "id": fake_uuid,
        "created_by": str(sample_template.created_by.id),
    }
    path = f"/service/{sample_template.service.id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]

    response = client.post(path, data=json.dumps(data), headers=headers)
    assert response.status_code == 201

    app.celery.tasks.process_job.apply_async.assert_called_once_with(
        ([str(fake_uuid)]), {"sender_id": None}, queue="job-tasks"
    )

    resp_json = json.loads(response.get_data(as_text=True))

    assert resp_json["data"]["id"] == fake_uuid
    assert resp_json["data"]["statistics"] == []
    assert resp_json["data"]["job_status"] == "pending"
    assert not resp_json["data"]["scheduled_for"]
    assert resp_json["data"]["job_status"] == "pending"
    assert resp_json["data"]["template"] == str(sample_template.id)
    assert resp_json["data"]["original_file_name"] == "thisisatest.csv"
    assert resp_json["data"]["notification_count"] == 1


def test_create_unscheduled_job_with_sender_id_in_metadata(
    client, sample_template, mocker, mock_celery_task, fake_uuid
):
    mock_celery_task(process_job)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_template.id),
            "original_file_name": "thisisatest.csv",
            "notification_count": "1",
            "valid": "True",
            "sender_id": fake_uuid,
        },
    )
    data = {
        "id": fake_uuid,
        "created_by": str(sample_template.created_by.id),
    }
    path = f"/service/{sample_template.service.id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]

    response = client.post(path, data=json.dumps(data), headers=headers)
    assert response.status_code == 201

    app.celery.tasks.process_job.apply_async.assert_called_once_with(
        ([str(fake_uuid)]), {"sender_id": fake_uuid}, queue="job-tasks"
    )


@freeze_time("2016-01-01 12:00:00.000000")
def test_create_scheduled_job(client, sample_template, mocker, mock_celery_task, fake_uuid):
    scheduled_date = (datetime.utcnow() + timedelta(hours=95, minutes=59)).isoformat()
    mock_celery_task(process_job)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_template.id),
            "original_file_name": "thisisatest.csv",
            "notification_count": "1",
            "valid": "True",
        },
    )
    data = {
        "id": fake_uuid,
        "created_by": str(sample_template.created_by.id),
        "scheduled_for": scheduled_date,
    }
    path = f"/service/{sample_template.service.id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]

    response = client.post(path, data=json.dumps(data), headers=headers)
    assert response.status_code == 201

    app.celery.tasks.process_job.apply_async.assert_not_called()

    resp_json = json.loads(response.get_data(as_text=True))

    assert resp_json["data"]["id"] == fake_uuid
    assert resp_json["data"]["scheduled_for"] == datetime(2016, 1, 5, 11, 59, 0, tzinfo=pytz.UTC).isoformat()
    assert resp_json["data"]["job_status"] == "scheduled"
    assert resp_json["data"]["template"] == str(sample_template.id)
    assert resp_json["data"]["original_file_name"] == "thisisatest.csv"
    assert resp_json["data"]["notification_count"] == 1


@pytest.mark.parametrize(
    "contact_list_archived",
    (
        True,
        False,
    ),
)
def test_create_job_with_contact_list_id(
    client,
    mocker,
    mock_celery_task,
    sample_template,
    fake_uuid,
    contact_list_archived,
):
    mock_celery_task(process_job)
    mocker.patch("app.job.rest.get_job_metadata_from_s3", return_value={"template_id": str(sample_template.id)})
    contact_list = create_service_contact_list(archived=contact_list_archived)
    data = {
        "id": fake_uuid,
        "valid": "True",
        "original_file_name": contact_list.original_file_name,
        "created_by": str(sample_template.service.users[0].id),
        "notification_count": 100,
        "contact_list_id": str(contact_list.id),
    }
    response = client.post(
        f"/service/{sample_template.service_id}/job",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    resp_json = response.get_json()
    assert response.status_code == 201
    assert resp_json["data"]["contact_list_id"] == str(contact_list.id)
    assert resp_json["data"]["original_file_name"] == "EmergencyContactList.xls"


def test_create_job_returns_403_if_service_is_not_active(client, fake_uuid, sample_service, mocker):
    sample_service.active = False
    mock_job_dao = mocker.patch("app.dao.jobs_dao.dao_create_job")
    auth_header = create_admin_authorization_header()
    response = client.post(
        f"/service/{sample_service.id}/job",
        data="",
        headers=[("Content-Type", "application/json"), auth_header],
    )

    assert response.status_code == 403
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["result"] == "error"
    assert resp_json["message"] == "Create job is not allowed: service is inactive "
    mock_job_dao.assert_not_called()


@pytest.mark.parametrize(
    "extra_metadata",
    (
        {},
        {"valid": "anything not the string True"},
    ),
)
def test_create_job_returns_400_if_file_is_invalid(
    client,
    fake_uuid,
    sample_template,
    mocker,
    extra_metadata,
):
    mock_job_dao = mocker.patch("app.dao.jobs_dao.dao_create_job")
    auth_header = create_admin_authorization_header()
    metadata = dict(
        template_id=str(sample_template.id),
        original_file_name="thisisatest.csv",
        notification_count=1,
        **extra_metadata,
    )
    mocker.patch("app.job.rest.get_job_metadata_from_s3", return_value=metadata)
    data = {"id": fake_uuid}
    response = client.post(
        f"/service/{sample_template.service.id}/job",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    assert response.status_code == 400
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["result"] == "error"
    assert resp_json["message"] == "File is not valid, can't create job"
    mock_job_dao.assert_not_called()


def test_create_job_returns_403_if_letter_template_type_and_service_in_trial(
    client, fake_uuid, sample_trial_letter_template, mocker
):
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_trial_letter_template.id),
            "original_file_name": "thisisatest.csv",
            "notification_count": "1",
        },
    )
    data = {
        "id": fake_uuid,
        "created_by": str(sample_trial_letter_template.created_by.id),
    }
    mock_job_dao = mocker.patch("app.dao.jobs_dao.dao_create_job")
    auth_header = create_admin_authorization_header()
    response = client.post(
        f"/service/{sample_trial_letter_template.service.id}/job",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    assert response.status_code == 403
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["result"] == "error"
    assert resp_json["message"] == "Create letter job is not allowed for service in trial mode "
    mock_job_dao.assert_not_called()


@freeze_time("2016-01-01 11:09:00.061258")
def test_should_not_create_scheduled_job_more_then_7_days_in_the_future(
    client, sample_template, mocker, mock_celery_task, fake_uuid
):
    scheduled_date = (datetime.utcnow() + timedelta(days=7, minutes=1)).isoformat()
    mock_celery_task(process_job)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_template.id),
            "original_file_name": "thisisatest.csv",
            "notification_count": "1",
            "valid": "True",
        },
    )
    data = {
        "id": fake_uuid,
        "created_by": str(sample_template.created_by.id),
        "scheduled_for": scheduled_date,
    }
    path = f"/service/{sample_template.service.id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]

    response = client.post(path, data=json.dumps(data), headers=headers)
    assert response.status_code == 400

    app.celery.tasks.process_job.apply_async.assert_not_called()

    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["result"] == "error"
    assert "scheduled_for" in resp_json["message"]
    assert resp_json["message"]["scheduled_for"] == ["Date cannot be more than 7 days in the future"]


@freeze_time("2016-01-01 11:09:00.061258")
def test_should_not_create_scheduled_job_in_the_past(client, sample_template, mocker, mock_celery_task, fake_uuid):
    scheduled_date = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    mock_celery_task(process_job)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_template.id),
            "original_file_name": "thisisatest.csv",
            "notification_count": "1",
            "valid": "True",
        },
    )
    data = {"id": fake_uuid, "created_by": str(sample_template.created_by.id), "scheduled_for": scheduled_date}
    path = f"/service/{sample_template.service.id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]

    response = client.post(path, data=json.dumps(data), headers=headers)
    assert response.status_code == 400

    app.celery.tasks.process_job.apply_async.assert_not_called()

    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["result"] == "error"
    assert "scheduled_for" in resp_json["message"]
    assert resp_json["message"]["scheduled_for"] == ["Date cannot be in the past"]


def test_create_job_returns_400_if_missing_id(client, sample_template, mocker, mock_celery_task):
    mock_celery_task(process_job)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_template.id),
        },
    )
    data = {}
    path = f"/service/{sample_template.service.id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]
    response = client.post(path, data=json.dumps(data), headers=headers)

    resp_json = json.loads(response.get_data(as_text=True))
    assert response.status_code == 400

    app.celery.tasks.process_job.apply_async.assert_not_called()
    assert resp_json["result"] == "error"
    assert "Missing data for required field." in resp_json["message"]["id"]


def test_create_job_returns_400_if_missing_data(client, sample_template, mocker, mock_celery_task, fake_uuid):
    mock_celery_task(process_job)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_template.id),
        },
    )
    data = {
        "id": fake_uuid,
        "valid": "True",
    }
    path = f"/service/{sample_template.service.id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]
    response = client.post(path, data=json.dumps(data), headers=headers)

    resp_json = json.loads(response.get_data(as_text=True))
    assert response.status_code == 400

    app.celery.tasks.process_job.apply_async.assert_not_called()
    assert resp_json["result"] == "error"
    assert "Missing data for required field." in resp_json["message"]["original_file_name"]
    assert "Missing data for required field." in resp_json["message"]["notification_count"]


def test_create_job_returns_404_if_template_does_not_exist(client, sample_service, mocker, mock_celery_task, fake_uuid):
    mock_celery_task(process_job)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_service.id),
        },
    )
    data = {
        "id": fake_uuid,
    }
    path = f"/service/{sample_service.id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]
    response = client.post(path, data=json.dumps(data), headers=headers)

    resp_json = json.loads(response.get_data(as_text=True))
    assert response.status_code == 404

    app.celery.tasks.process_job.apply_async.assert_not_called()
    assert resp_json["result"] == "error"
    assert resp_json["message"] == "No result found"


def test_create_job_returns_404_if_missing_service(client, sample_template, mocker, mock_celery_task):
    mock_celery_task(process_job)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_template.id),
        },
    )
    random_id = str(uuid.uuid4())
    data = {}
    path = f"/service/{random_id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]
    response = client.post(path, data=json.dumps(data), headers=headers)

    resp_json = json.loads(response.get_data(as_text=True))
    assert response.status_code == 404

    app.celery.tasks.process_job.apply_async.assert_not_called()
    assert resp_json["result"] == "error"
    assert resp_json["message"] == "No result found"


def test_create_job_returns_400_if_archived_template(client, sample_template, mocker, mock_celery_task, fake_uuid):
    mock_celery_task(process_job)
    sample_template.archived = True
    dao_update_template(sample_template)
    mocker.patch(
        "app.job.rest.get_job_metadata_from_s3",
        return_value={
            "template_id": str(sample_template.id),
        },
    )
    data = {
        "id": fake_uuid,
        "valid": "True",
    }
    path = f"/service/{sample_template.service.id}/job"
    auth_header = create_admin_authorization_header()
    headers = [("Content-Type", "application/json"), auth_header]
    response = client.post(path, data=json.dumps(data), headers=headers)

    resp_json = json.loads(response.get_data(as_text=True))
    assert response.status_code == 400

    app.celery.tasks.process_job.apply_async.assert_not_called()
    assert resp_json["result"] == "error"
    assert "Template has been deleted" in resp_json["message"]["template"]


def _setup_jobs(template, number_of_jobs=5):
    for _ in range(number_of_jobs):
        create_job(template=template)


def test_get_all_notifications_for_job_in_order_of_job_number(admin_request, sample_template):
    main_job = create_job(sample_template)
    another_job = create_job(sample_template)

    notification_1 = create_notification(job=main_job, to_field="1", job_row_number=1)
    notification_2 = create_notification(job=main_job, to_field="2", job_row_number=2)
    notification_3 = create_notification(job=main_job, to_field="3", job_row_number=3)
    create_notification(job=another_job)

    resp = admin_request.get(
        "job.get_all_notifications_for_service_job", service_id=main_job.service_id, job_id=main_job.id
    )

    assert len(resp["notifications"]) == 3
    assert resp["notifications"][0]["to"] == notification_1.to
    assert resp["notifications"][0]["job_row_number"] == notification_1.job_row_number
    assert resp["notifications"][1]["to"] == notification_2.to
    assert resp["notifications"][1]["job_row_number"] == notification_2.job_row_number
    assert resp["notifications"][2]["to"] == notification_3.to
    assert resp["notifications"][2]["job_row_number"] == notification_3.job_row_number


@pytest.mark.parametrize(
    "expected_notification_count, status_args",
    [
        (1, ["created"]),
        (0, ["sending"]),
        (1, ["created", "sending"]),
        (0, ["sending", "delivered"]),
    ],
)
def test_get_all_notifications_for_job_filtered_by_status(
    admin_request, sample_job, expected_notification_count, status_args
):
    create_notification(job=sample_job, to_field="1", status="created")

    resp = admin_request.get(
        "job.get_all_notifications_for_service_job",
        service_id=sample_job.service_id,
        job_id=sample_job.id,
        status=status_args,
    )
    assert len(resp["notifications"]) == expected_notification_count


def test_get_all_notifications_for_job_returns_correct_format(admin_request, sample_notification_with_job):
    service_id = sample_notification_with_job.service_id
    job_id = sample_notification_with_job.job_id

    resp = admin_request.get("job.get_all_notifications_for_service_job", service_id=service_id, job_id=job_id)

    assert len(resp["notifications"]) == 1
    assert resp["notifications"][0]["id"] == str(sample_notification_with_job.id)
    assert resp["notifications"][0]["status"] == sample_notification_with_job.status


def test_get_notification_count_for_job_id(admin_request, mocker, sample_job):
    mock_dao = mocker.patch("app.job.rest.dao_get_notification_count_for_job_id", return_value=3)
    response = admin_request.get(
        "job.get_notification_count_for_job_id", service_id=sample_job.service_id, job_id=sample_job.id
    )
    mock_dao.assert_called_once_with(job_id=str(sample_job.id))
    assert response["count"] == 3


def test_get_notification_count_for_job_id_for_wrong_service_id(admin_request, sample_job):
    service_id = uuid.uuid4()
    response = admin_request.get(
        "job.get_notification_count_for_job_id", service_id=service_id, job_id=sample_job.id, _expected_status=404
    )
    assert response["message"] == "No result found"


def test_get_notification_count_for_job_id_for_wrong_job_id(admin_request, sample_service):
    job_id = uuid.uuid4()
    response = admin_request.get(
        "job.get_notification_count_for_job_id", service_id=sample_service.id, job_id=job_id, _expected_status=404
    )
    assert response["message"] == "No result found"


def test_get_job_by_id(admin_request, sample_job):
    job_id = str(sample_job.id)
    service_id = sample_job.service.id

    resp_json = admin_request.get("job.get_job_by_service_and_job_id", service_id=service_id, job_id=job_id)

    assert resp_json["data"]["id"] == job_id
    assert resp_json["data"]["statistics"] == []
    assert resp_json["data"]["created_by"]["name"] == "Test User"


def test_get_job_by_id_should_return_summed_statistics(admin_request, sample_job):
    job_id = str(sample_job.id)
    service_id = sample_job.service.id

    create_notification(job=sample_job, status="created")
    create_notification(job=sample_job, status="created")
    create_notification(job=sample_job, status="created")
    create_notification(job=sample_job, status="sending")
    create_notification(job=sample_job, status="failed")
    create_notification(job=sample_job, status="failed")
    create_notification(job=sample_job, status="failed")
    create_notification(job=sample_job, status="technical-failure")
    create_notification(job=sample_job, status="temporary-failure")
    create_notification(job=sample_job, status="temporary-failure")

    resp_json = admin_request.get("job.get_job_by_service_and_job_id", service_id=service_id, job_id=job_id)

    assert resp_json["data"]["id"] == job_id
    assert {"status": "created", "count": 3} in resp_json["data"]["statistics"]
    assert {"status": "sending", "count": 1} in resp_json["data"]["statistics"]
    assert {"status": "failed", "count": 3} in resp_json["data"]["statistics"]
    assert {"status": "technical-failure", "count": 1} in resp_json["data"]["statistics"]
    assert {"status": "temporary-failure", "count": 2} in resp_json["data"]["statistics"]
    assert resp_json["data"]["created_by"]["name"] == "Test User"


def test_get_job_by_id_with_stats_for_old_job_where_notifications_have_been_purged(admin_request, sample_template):
    old_job = create_job(
        sample_template, notification_count=10, created_at=datetime.utcnow() - timedelta(days=9), job_status="finished"
    )

    def __create_ft_status(job, status, count):
        create_ft_notification_status(
            bst_date=job.created_at.date(),
            notification_type="sms",
            service=job.service,
            job=job,
            template=job.template,
            key_type="normal",
            notification_status=status,
            count=count,
        )

    __create_ft_status(old_job, "created", 3)
    __create_ft_status(old_job, "sending", 1)
    __create_ft_status(old_job, "failed", 3)
    __create_ft_status(old_job, "technical-failure", 1)
    __create_ft_status(old_job, "temporary-failure", 2)

    resp_json = admin_request.get("job.get_job_by_service_and_job_id", service_id=old_job.service_id, job_id=old_job.id)

    assert resp_json["data"]["id"] == str(old_job.id)
    assert {"status": "created", "count": 3} in resp_json["data"]["statistics"]
    assert {"status": "sending", "count": 1} in resp_json["data"]["statistics"]
    assert {"status": "failed", "count": 3} in resp_json["data"]["statistics"]
    assert {"status": "technical-failure", "count": 1} in resp_json["data"]["statistics"]
    assert {"status": "temporary-failure", "count": 2} in resp_json["data"]["statistics"]
    assert resp_json["data"]["created_by"]["name"] == "Test User"


@freeze_time("2017-07-17 07:17")
def test_get_jobs(admin_request, sample_template, mocker):
    _setup_jobs(sample_template)

    mock_redis_get = mocker.patch(
        "app.redis_store.get",
        return_value=None,
    )

    service_id = sample_template.service.id

    resp_json = admin_request.get("job.get_jobs_by_service", service_id=service_id)
    assert len(resp_json["data"]) == 5
    assert resp_json["data"][0] == {
        "archived": False,
        "created_at": "2017-07-17T07:17:00+00:00",
        "created_by": {
            "id": ANY,
            "name": "Test User",
        },
        "id": ANY,
        "job_status": "pending",
        "notification_count": 1,
        "original_file_name": "some.csv",
        "processing_finished": None,
        "processing_started": None,
        "scheduled_for": None,
        "service": str(sample_template.service.id),
        "service_name": {"name": sample_template.service.name},
        "statistics": [],
        "template": str(sample_template.id),
        "template_name": sample_template.name,
        "template_type": "sms",
        "template_version": 1,
        "updated_at": None,
        "contact_list_id": None,
    }
    assert len(mock_redis_get.mock_calls) == 5


def test_get_jobs_with_limit_days(admin_request, sample_template, mocker):
    jobs = []
    for time in [
        "Sunday 1st July 2018 22:59",
        "Sunday 2nd July 2018 23:00",  # beginning of monday morning
        "Monday 3rd July 2018 12:00",
    ]:
        with freeze_time(time):
            jobs.append(create_job(template=sample_template))

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    with freeze_time("Monday 9th July 2018 12:00"):
        resp_json = admin_request.get("job.get_jobs_by_service", service_id=sample_template.service_id, limit_days=7)

    assert len(resp_json["data"]) == 2
    assert mock_redis_get.mock_calls == [
        mocker.call(f"job-{jobs[2].id}-notification-outcomes"),
        mocker.call(f"job-{jobs[1].id}-notification-outcomes"),
    ]


def test_get_jobs_by_contact_list(admin_request, sample_template, mocker):
    contact_list = create_service_contact_list()
    job_1 = create_job(template=sample_template)  # noqa
    job_2 = create_job(template=sample_template, contact_list_id=contact_list.id)

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    resp_json = admin_request.get(
        "job.get_jobs_by_service",
        service_id=sample_template.service_id,
        contact_list_id=contact_list.id,
    )

    assert len(resp_json["data"]) == 1
    assert mock_redis_get.mock_calls == [
        mocker.call(f"job-{job_2.id}-notification-outcomes"),
    ]


def test_get_jobs_should_return_statistics(admin_request, sample_template, mocker):
    now = datetime.utcnow()
    earlier = datetime.utcnow() - timedelta(days=1)
    job_1 = create_job(sample_template, processing_started=earlier)
    job_2 = create_job(sample_template, processing_started=now)
    create_notification(job=job_1, status="created")
    create_notification(job=job_1, status="created")
    create_notification(job=job_1, status="created")
    create_notification(job=job_2, status="sending")
    create_notification(job=job_2, status="sending")
    create_notification(job=job_2, status="sending")

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    resp_json = admin_request.get("job.get_jobs_by_service", service_id=sample_template.service_id)

    assert len(resp_json["data"]) == 2
    assert resp_json["data"][0]["id"] == str(job_2.id)
    assert {"status": "sending", "count": 3} in resp_json["data"][0]["statistics"]
    assert resp_json["data"][1]["id"] == str(job_1.id)
    assert {"status": "created", "count": 3} in resp_json["data"][1]["statistics"]
    assert mock_redis_get.mock_calls == [
        mocker.call(f"job-{job_2.id}-notification-outcomes"),
        mocker.call(f"job-{job_1.id}-notification-outcomes"),
    ]


def test_get_jobs_should_return_no_stats_if_no_rows_in_notifications(admin_request, sample_template, mocker):
    now = datetime.utcnow()
    earlier = datetime.utcnow() - timedelta(days=1)
    job_1 = create_job(sample_template, created_at=earlier)
    job_2 = create_job(sample_template, created_at=now)

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    resp_json = admin_request.get("job.get_jobs_by_service", service_id=sample_template.service_id)

    assert len(resp_json["data"]) == 2
    assert resp_json["data"][0]["id"] == str(job_2.id)
    assert resp_json["data"][0]["statistics"] == []
    assert resp_json["data"][1]["id"] == str(job_1.id)
    assert resp_json["data"][1]["statistics"] == []
    assert mock_redis_get.mock_calls == [
        mocker.call(f"job-{job_2.id}-notification-outcomes"),
        mocker.call(f"job-{job_1.id}-notification-outcomes"),
    ]


def test_get_jobs_should_paginate(admin_request, sample_template, mocker):
    create_10_jobs(sample_template)

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    with set_config(admin_request.app, "PAGE_SIZE", 2):
        resp_json = admin_request.get("job.get_jobs_by_service", service_id=sample_template.service_id)

    assert resp_json["data"][0]["created_at"] == "2015-01-01T10:00:00+00:00"
    assert resp_json["data"][1]["created_at"] == "2015-01-01T09:00:00+00:00"
    assert resp_json["page_size"] == 2
    assert resp_json["total"] == 10
    assert "links" in resp_json
    assert set(resp_json["links"].keys()) == {"next", "last"}
    assert len(mock_redis_get.mock_calls) == 2


def test_get_jobs_accepts_page_parameter(admin_request, sample_template, mocker):
    create_10_jobs(sample_template)

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    with set_config(admin_request.app, "PAGE_SIZE", 2):
        resp_json = admin_request.get("job.get_jobs_by_service", service_id=sample_template.service_id, page=2)

    assert resp_json["data"][0]["created_at"] == "2015-01-01T08:00:00+00:00"
    assert resp_json["data"][1]["created_at"] == "2015-01-01T07:00:00+00:00"
    assert resp_json["page_size"] == 2
    assert resp_json["total"] == 10
    assert "links" in resp_json
    assert set(resp_json["links"].keys()) == {"prev", "next", "last"}
    assert mock_redis_get.mock_calls == [
        mocker.call(f'job-{resp_json["data"][0]["id"]}-notification-outcomes'),
        mocker.call(f'job-{resp_json["data"][1]["id"]}-notification-outcomes'),
    ]


@pytest.mark.parametrize(
    "statuses_filter, expected_statuses",
    [
        ("", JOB_STATUS_TYPES),
        ("pending", [JOB_STATUS_PENDING]),
        (
            "pending, in progress, finished, sending limits exceeded, scheduled, cancelled, ready to send, sent to dvla, error",  # noqa
            JOB_STATUS_TYPES,
        ),
        # bad statuses are accepted, just return no data
        ("foo", []),
    ],
)
def test_get_jobs_can_filter_on_statuses(admin_request, sample_template, statuses_filter, expected_statuses, mocker):
    create_job(sample_template, job_status="pending")
    create_job(sample_template, job_status="in progress")
    create_job(sample_template, job_status="finished")
    create_job(sample_template, job_status="sending limits exceeded")
    create_job(sample_template, job_status="scheduled")
    create_job(sample_template, job_status="cancelled")
    create_job(sample_template, job_status="ready to send")
    create_job(sample_template, job_status="sent to dvla")
    create_job(sample_template, job_status="error")

    mock_redis_get = mocker.patch("app.redis_store.get", return_value=None)
    mocker.patch("app.redis_store.set", side_effect=AssertionError("Set call not expected"))

    resp_json = admin_request.get(
        "job.get_jobs_by_service", service_id=sample_template.service_id, statuses=statuses_filter
    )

    assert {x["job_status"] for x in resp_json["data"]} == set(expected_statuses)
    assert len(mock_redis_get.mock_calls) == len(expected_statuses)


def create_10_jobs(template):
    with freeze_time("2015-01-01T00:00:00") as the_time:
        for _ in range(10):
            the_time.tick(timedelta(hours=1))
            create_job(template)


def test_get_all_notifications_for_job_returns_csv_format(admin_request, sample_notification_with_job):
    resp = admin_request.get(
        "job.get_all_notifications_for_service_job",
        service_id=sample_notification_with_job.service_id,
        job_id=sample_notification_with_job.job_id,
        format_for_csv=True,
    )

    assert len(resp["notifications"]) == 1
    assert set(resp["notifications"][0].keys()) == {
        "id",
        "created_at",
        "created_by_name",
        "created_by_email_address",
        "template_type",
        "template_name",
        "job_name",
        "status",
        "row_number",
        "recipient",
        "client_reference",
        "api_key_name",
    }


@freeze_time("2017-06-10 12:00")
def test_get_jobs_should_retrieve_from_ft_notification_status_for_old_jobs(admin_request, sample_template, mocker):
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

    mock_redis_get = mocker.patch(
        "app.redis_store.get",
        return_value=None,
    )

    resp_json = admin_request.get("job.get_jobs_by_service", service_id=sample_template.service_id)

    assert resp_json["data"][0]["id"] == str(job_3.id)
    assert resp_json["data"][0]["statistics"] == []
    assert resp_json["data"][1]["id"] == str(job_2.id)
    assert resp_json["data"][1]["statistics"] == [{"status": "created", "count": 1}]
    assert resp_json["data"][2]["id"] == str(job_1.id)
    assert resp_json["data"][2]["statistics"] == [{"status": "delivered", "count": 6}]
    assert mock_redis_get.mock_calls == [
        mocker.call(f'job-{resp_json["data"][0]["id"]}-notification-outcomes'),
        mocker.call(f'job-{resp_json["data"][1]["id"]}-notification-outcomes'),
        mocker.call(f'job-{resp_json["data"][2]["id"]}-notification-outcomes'),
    ]


@freeze_time("2017-07-17 07:17")
def test_get_scheduled_job_stats_when_no_scheduled_jobs(admin_request, sample_template):
    # This sets up a bunch of regular, non-scheduled jobs
    _setup_jobs(sample_template)

    service_id = sample_template.service.id

    resp_json = admin_request.get("job.get_scheduled_job_stats", service_id=service_id)
    assert resp_json == {
        "count": 0,
        "soonest_scheduled_for": None,
    }


@freeze_time("2017-07-17 07:17")
def test_get_scheduled_job_stats(admin_request):
    service_1 = create_service(service_name="service 1")
    service_1_template = create_template(service=service_1)
    service_2 = create_service(service_name="service 2")
    service_2_template = create_template(service=service_2)

    # Shouldn’t be counted – wrong status
    create_job(service_1_template, job_status="finished", scheduled_for="2017-07-17 07:00")
    create_job(service_1_template, job_status="in progress", scheduled_for="2017-07-17 08:00")

    # Should be counted – service 1
    create_job(service_1_template, job_status="scheduled", scheduled_for="2017-07-17 09:00")
    create_job(service_1_template, job_status="scheduled", scheduled_for="2017-07-17 10:00")
    create_job(service_1_template, job_status="scheduled", scheduled_for="2017-07-17 11:00")

    # Should be counted – service 2
    create_job(service_2_template, job_status="scheduled", scheduled_for="2017-07-17 11:00")

    assert admin_request.get(
        "job.get_scheduled_job_stats",
        service_id=service_1.id,
    ) == {
        "count": 3,
        "soonest_scheduled_for": "2017-07-17T09:00:00+00:00",
    }

    assert admin_request.get(
        "job.get_scheduled_job_stats",
        service_id=service_2.id,
    ) == {
        "count": 1,
        "soonest_scheduled_for": "2017-07-17T11:00:00+00:00",
    }
