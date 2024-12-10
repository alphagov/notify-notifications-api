import uuid
from unittest.mock import ANY

import pytest
from flask import json

from app.celery.letters_pdf_tasks import get_pdf_for_templated_letter
from app.celery.research_mode_tasks import create_fake_letter_callback
from app.config import QueueNames
from app.constants import (
    EMAIL_TYPE,
    INTERNATIONAL_LETTERS,
    KEY_TYPE_TEAM,
    KEY_TYPE_TEST,
    LETTER_TYPE,
    NOTIFICATION_CREATED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_PENDING_VIRUS_CHECK,
    NOTIFICATION_SENDING,
    SMS_TYPE,
)
from app.models import Job, Notification
from app.notifications.process_letter_notifications import (
    create_letter_notification,
)
from app.schema_validation import validate
from app.v2.errors import RateLimitError
from app.v2.notifications.notification_schemas import post_letter_response
from tests import create_service_authorization_header
from tests.app.db import create_letter_contact, create_service, create_template
from tests.conftest import set_config_values

test_address = {"address_line_1": "test 1", "address_line_2": "test 2", "postcode": "test pc"}


@pytest.mark.parametrize("reference", [None, "reference_from_client"])
def test_post_letter_notification_returns_201(api_client_request, sample_letter_template, mocker, reference):
    mock = mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")
    data = {
        "template_id": str(sample_letter_template.id),
        "personalisation": {
            "address_line_1": "Her Royal Highness Queen Elizabeth II",
            "address_line_2": "Buckingham Palace",
            "address_line_3": "London",
            "postcode": "SW1 1AA",
            "name": "Lizzie",
        },
    }

    if reference:
        data.update({"reference": reference})

    resp_json = api_client_request.post(
        sample_letter_template.service_id, "v2_notifications.post_notification", notification_type="letter", _data=data
    )

    assert validate(resp_json, post_letter_response) == resp_json
    assert Job.query.count() == 0
    notification = Notification.query.one()
    assert notification.status == NOTIFICATION_CREATED
    assert resp_json["id"] == str(notification.id)
    assert resp_json["reference"] == reference
    assert resp_json["content"]["subject"] == sample_letter_template.subject
    assert resp_json["content"]["body"] == sample_letter_template.content
    assert f"v2/notifications/{notification.id}" in resp_json["uri"]
    assert resp_json["template"]["id"] == str(sample_letter_template.id)
    assert resp_json["template"]["version"] == sample_letter_template.version
    assert (
        f"services/{sample_letter_template.service_id}/templates/{sample_letter_template.id}"
        in resp_json["template"]["uri"]
    )
    assert not resp_json["scheduled_for"]
    assert not notification.reply_to_text
    mock.assert_called_once_with([str(notification.id)], queue=QueueNames.CREATE_LETTERS_PDF)


def test_post_letter_notification_sets_postage(api_client_request, notify_db_session, mocker):
    service = create_service(service_permissions=[LETTER_TYPE])
    template = create_template(service, template_type="letter", postage="first")
    mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")
    data = {
        "template_id": str(template.id),
        "personalisation": {
            "address_line_1": "Her Royal Highness Queen Elizabeth II",
            "address_line_2": "Buckingham Palace",
            "address_line_3": "London",
            "postcode": "SW1 1AA",
            "name": "Lizzie",
        },
    }

    resp_json = api_client_request.post(
        service.id, "v2_notifications.post_notification", notification_type="letter", _data=data
    )

    assert validate(resp_json, post_letter_response) == resp_json
    notification = Notification.query.one()
    assert notification.postage == "first"


def test_post_letter_notification_formats_postcode(api_client_request, notify_db_session, mocker):
    service = create_service(service_permissions=[LETTER_TYPE])
    template = create_template(service, template_type="letter")
    mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")
    data = {
        "template_id": str(template.id),
        "personalisation": {
            "address_line_1": "Her Royal Highness Queen Elizabeth II",
            "address_line_2": "Buckingham Palace",
            "address_line_3": "London",
            "postcode": "  Sw1  1aa   ",
            "name": "Lizzie",
        },
    }

    resp_json = api_client_request.post(
        service.id, "v2_notifications.post_notification", notification_type="letter", _data=data
    )

    assert validate(resp_json, post_letter_response) == resp_json
    notification = Notification.query.one()
    # We store what the client gives us, and only reformat it when
    # generating the PDF
    assert notification.personalisation["postcode"] == "  Sw1  1aa   "


def test_post_letter_notification_stores_country(api_client_request, notify_db_session, mocker):
    service = create_service(service_permissions=[LETTER_TYPE, INTERNATIONAL_LETTERS])
    template = create_template(service, template_type="letter")
    mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")
    data = {
        "template_id": str(template.id),
        "personalisation": {
            "address_line_1": "Kaiser Wilhelm II",
            "address_line_2": "Kronprinzenpalais",
            "address_line_5": "   deutschland   ",
        },
    }

    resp_json = api_client_request.post(
        service.id, "v2_notifications.post_notification", notification_type="letter", _data=data
    )

    assert validate(resp_json, post_letter_response) == resp_json
    notification = Notification.query.one()
    # In the personalisation we store what the client gives us
    assert notification.personalisation["address_line_1"] == "Kaiser Wilhelm II"
    assert notification.personalisation["address_line_2"] == "Kronprinzenpalais"
    assert notification.personalisation["address_line_5"] == "   deutschland   "
    # In the to field we store the whole address with the canonical country
    assert notification.to == ("Kaiser Wilhelm II\nKronprinzenpalais\nGermany")
    assert notification.postage == "europe"
    assert notification.international


def test_post_letter_notification_international_sets_rest_of_world(api_client_request, notify_db_session, mocker):
    service = create_service(service_permissions=[LETTER_TYPE, INTERNATIONAL_LETTERS])
    template = create_template(service, template_type="letter")
    mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")
    data = {
        "template_id": str(template.id),
        "personalisation": {
            "address_line_1": "Prince Harry",
            "address_line_2": "Toronto",
            "address_line_5": "Canada",
        },
    }

    resp_json = api_client_request.post(
        service.id, "v2_notifications.post_notification", notification_type="letter", _data=data
    )

    assert validate(resp_json, post_letter_response) == resp_json
    notification = Notification.query.one()

    assert notification.postage == "rest-of-world"


@pytest.mark.parametrize(
    "permissions, personalisation, expected_error",
    (
        (
            [LETTER_TYPE],
            {
                "address_line_1": "Her Royal Highness Queen Elizabeth II",
                "address_line_2": "Buckingham Palace",
                "address_line_3": "London",
                "postcode": "not a real postcode",
                "name": "Lizzie",
            },
            "Must be a real UK postcode",
        ),
        (
            [LETTER_TYPE],
            {
                "address_line_1": "Her Royal Highness Queen Elizabeth II",
                "address_line_2": "]Buckingham Palace",
                "postcode": "SW1A 1AA",
                "name": "Lizzie",
            },
            "Address lines must not start with any of the following characters: @ ( ) = [ ] ” \\ / , < >",
        ),
        (
            [LETTER_TYPE, INTERNATIONAL_LETTERS],
            {
                "address_line_1": "Her Royal Highness Queen Elizabeth II",
                "address_line_2": "Buckingham Palace",
                "address_line_3": "London",
                "postcode": "not a real postcode",
                "name": "Lizzie",
            },
            "Last line of address must be a real UK postcode or another country",
        ),
        (
            [LETTER_TYPE],
            {
                "address_line_1": "No fixed abode",
                "address_line_2": "Buckingham Palace",
                "postcode": "SW1A 1AA",
                "name": "Unknown",
            },
            "Must be a real address",
        ),
    ),
)
def test_post_letter_notification_throws_error_for_bad_address(
    api_client_request, notify_db_session, mocker, permissions, personalisation, expected_error
):
    service = create_service(service_permissions=permissions)
    template = create_template(service, template_type="letter", postage="first")
    mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")
    data = {"template_id": str(template.id), "personalisation": personalisation}

    error_json = api_client_request.post(
        service.id, "v2_notifications.post_notification", notification_type="letter", _data=data, _expected_status=400
    )

    assert error_json["status_code"] == 400
    assert error_json["errors"] == [{"error": "ValidationError", "message": expected_error}]


def test_post_letter_notification_with_test_key_creates_pdf_and_sets_status_to_delivered(
    notify_api, api_client_request, sample_letter_template, mock_celery_task
):
    data = {
        "template_id": str(sample_letter_template.id),
        "personalisation": {
            "address_line_1": "Her Royal Highness Queen Elizabeth II",
            "address_line_2": "Buckingham Palace",
            "address_line_3": "London",
            "postcode": "SW1 1AA",
            "name": "Lizzie",
        },
        "reference": "foo",
    }

    fake_create_letter_task = mock_celery_task(get_pdf_for_templated_letter)
    fake_create_dvla_response_task = mock_celery_task(create_fake_letter_callback)

    with set_config_values(notify_api, {"SEND_LETTERS_ENABLED": True}):
        api_client_request.post(
            sample_letter_template.service_id,
            "v2_notifications.post_notification",
            notification_type="letter",
            _data=data,
            _api_key_type=KEY_TYPE_TEST,
        )

    notification = Notification.query.one()

    fake_create_letter_task.assert_called_once_with([str(notification.id)], queue="research-mode-tasks")
    assert not fake_create_dvla_response_task.called
    assert notification.status == NOTIFICATION_DELIVERED
    assert notification.updated_at is not None


def test_post_letter_notification_with_test_key_creates_pdf_and_sets_status_to_sending_and_sends_fake_response_file(
    notify_api, api_client_request, sample_letter_template, mock_celery_task
):
    data = {
        "template_id": str(sample_letter_template.id),
        "personalisation": {
            "address_line_1": "Her Royal Highness Queen Elizabeth II",
            "address_line_2": "Buckingham Palace",
            "address_line_3": "London",
            "postcode": "SW1 1AA",
            "name": "Lizzie",
        },
        "reference": "foo",
    }

    fake_create_letter_task = mock_celery_task(get_pdf_for_templated_letter)
    fake_create_dvla_response_task = mock_celery_task(create_fake_letter_callback)
    with set_config_values(notify_api, {"SEND_LETTERS_ENABLED": False}):
        api_client_request.post(
            sample_letter_template.service_id,
            "v2_notifications.post_notification",
            notification_type="letter",
            _data=data,
            _api_key_type=KEY_TYPE_TEST,
        )

    notification = Notification.query.one()

    fake_create_letter_task.assert_called_once_with([str(notification.id)], queue="research-mode-tasks")
    assert fake_create_dvla_response_task.called
    assert notification.status == NOTIFICATION_SENDING


def test_post_letter_notification_returns_400_and_missing_template(api_client_request, sample_service_full_permissions):
    data = {"template_id": str(uuid.uuid4()), "personalisation": test_address}

    error_json = api_client_request.post(
        sample_service_full_permissions.id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _expected_status=400,
    )

    assert error_json["status_code"] == 400
    assert error_json["errors"] == [{"error": "BadRequestError", "message": "Template not found"}]


def test_post_letter_notification_returns_400_for_empty_personalisation(
    api_client_request, sample_service_full_permissions, sample_letter_template
):
    data = {
        "template_id": str(sample_letter_template.id),
        "personalisation": {"address_line_1": "", "address_line_2": "", "postcode": ""},
    }

    error_json = api_client_request.post(
        sample_service_full_permissions.id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _expected_status=400,
    )

    assert error_json["status_code"] == 400
    assert all(e["error"] == "ValidationError" for e in error_json["errors"])
    assert {e["message"] for e in error_json["errors"]} == {
        "Address must be at least 3 lines",
    }


def test_post_notification_returns_400_for_missing_letter_contact_block_personalisation(
    api_client_request,
    sample_service,
):
    letter_contact_block = create_letter_contact(
        service=sample_service, contact_block="((contact block))", is_default=True
    )
    template = create_template(
        service=sample_service,
        template_type="letter",
        reply_to=letter_contact_block.id,
    )
    data = {
        "template_id": str(template.id),
        "personalisation": {
            "address_line_1": "Line 1",
            "address_line_2": "Line 2",
            "postcode": "SW1A 1AA",
        },
    }

    error_json = api_client_request.post(
        sample_service.id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _expected_status=400,
    )

    assert error_json["status_code"] == 400
    assert error_json["errors"] == [{"error": "BadRequestError", "message": "Missing personalisation: contact block"}]


def test_notification_returns_400_for_missing_template_field(api_client_request, sample_service_full_permissions):
    data = {"personalisation": test_address}

    error_json = api_client_request.post(
        sample_service_full_permissions.id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _expected_status=400,
    )

    assert error_json["status_code"] == 400
    assert error_json["errors"] == [{"error": "ValidationError", "message": "template_id is a required property"}]


def test_notification_returns_400_if_address_doesnt_have_underscores(api_client_request, sample_letter_template):
    data = {
        "template_id": str(sample_letter_template.id),
        "personalisation": {
            "address line 1": "Her Royal Highness Queen Elizabeth II",
            "address-line-2": "Buckingham Palace",
            "postcode": "SW1 1AA",
        },
    }

    error_json = api_client_request.post(
        sample_letter_template.service_id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _expected_status=400,
    )

    assert error_json["status_code"] == 400
    assert error_json["errors"] == [{"error": "ValidationError", "message": "Address must be at least 3 lines"}]


def test_returns_a_429_limit_exceeded_if_rate_limit_exceeded(api_client_request, sample_letter_template, mocker):
    persist_mock = mocker.patch("app.v2.notifications.post_notifications.persist_notification")
    mocker.patch(
        "app.v2.notifications.post_notifications.check_rate_limiting",
        side_effect=RateLimitError("LIMIT", "INTERVAL", "TYPE"),
    )

    data = {"template_id": str(sample_letter_template.id), "personalisation": test_address}

    error_json = api_client_request.post(
        sample_letter_template.service_id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _expected_status=429,
    )

    assert error_json["status_code"] == 429
    assert error_json["errors"] == [
        {
            "error": "RateLimitError",
            "message": "Exceeded rate limit for key type TYPE of LIMIT requests per INTERVAL seconds",
        }
    ]

    assert not persist_mock.called


@pytest.mark.parametrize(
    "service_args, expected_status, expected_message",
    [
        (
            {"service_permissions": [EMAIL_TYPE, SMS_TYPE]},
            400,
            "Service is not allowed to send letters",
        ),
        (
            {"restricted": True},
            403,
            "Cannot send letters when service is in trial mode",
        ),
    ],
)
def test_post_letter_notification_returns_403_if_not_allowed_to_send_notification(
    api_client_request,
    notify_db_session,
    service_args,
    expected_status,
    expected_message,
):
    service = create_service(**service_args)
    template = create_template(service, template_type=LETTER_TYPE)

    data = {"template_id": str(template.id), "personalisation": test_address}

    error_json = api_client_request.post(
        service.id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _expected_status=expected_status,
    )

    assert error_json["status_code"] == expected_status
    assert error_json["errors"] == [{"error": "BadRequestError", "message": expected_message}]


def test_post_letter_notification_doesnt_accept_team_key(api_client_request, sample_letter_template, mocker):
    mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")
    data = {
        "template_id": str(sample_letter_template.id),
        "personalisation": {"address_line_1": "Foo", "address_line_2": "Bar", "postcode": "Baz"},
    }

    error_json = api_client_request.post(
        sample_letter_template.service_id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _api_key_type=KEY_TYPE_TEAM,
        _expected_status=403,
    )

    assert error_json["status_code"] == 403
    assert error_json["errors"] == [{"error": "BadRequestError", "message": "Cannot send letters with a team api key"}]


def test_post_letter_notification_doesnt_send_in_trial(api_client_request, sample_trial_letter_template, mocker):
    mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")
    data = {
        "template_id": str(sample_trial_letter_template.id),
        "personalisation": {"address_line_1": "Foo", "address_line_2": "Bar", "postcode": "Baz"},
    }

    error_json = api_client_request.post(
        sample_trial_letter_template.service_id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _expected_status=403,
    )

    assert error_json["status_code"] == 403
    assert error_json["errors"] == [
        {"error": "BadRequestError", "message": "Cannot send letters when service is in trial mode"}
    ]


def test_post_letter_notification_is_delivered_but_still_creates_pdf_if_in_trial_mode_and_using_test_key(
    api_client_request, sample_trial_letter_template, mocker
):
    fake_create_letter_task = mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")

    data = {
        "template_id": sample_trial_letter_template.id,
        "personalisation": {"address_line_1": "Foo", "address_line_2": "Bar", "postcode": "BA5 5AB"},
    }

    api_client_request.post(
        sample_trial_letter_template.service_id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
        _api_key_type=KEY_TYPE_TEST,
    )

    notification = Notification.query.one()
    assert notification.status == NOTIFICATION_DELIVERED
    fake_create_letter_task.assert_called_once_with([str(notification.id)], queue="research-mode-tasks")


def test_post_letter_notification_is_delivered_and_has_pdf_uploaded_to_test_letters_bucket_using_test_key(
    api_client_request, mocker
):
    sample_letter_service = create_service(service_permissions=["letter"])
    mocker.patch("app.celery.letters_pdf_tasks.notify_celery.send_task")
    s3mock = mocker.patch("app.v2.notifications.post_notifications.upload_letter_pdf", return_value="test.pdf")
    data = {"reference": "letter-reference", "content": "bGV0dGVyLWNvbnRlbnQ="}

    api_client_request.post(
        sample_letter_service.id,
        "v2_notifications.post_precompiled_letter_notification",
        _data=data,
        _api_key_type=KEY_TYPE_TEST,
    )

    notification = Notification.query.one()
    assert notification.status == NOTIFICATION_PENDING_VIRUS_CHECK
    s3mock.assert_called_once_with(ANY, b"letter-content", precompiled=True)


def test_post_letter_notification_ignores_reply_to_text_for_service(api_client_request, notify_db_session, mocker):
    mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")

    service = create_service(service_permissions=[LETTER_TYPE])
    create_letter_contact(service=service, contact_block="ignored", is_default=True)
    template = create_template(service=service, template_type="letter")
    data = {
        "template_id": template.id,
        "personalisation": {"address_line_1": "Foo", "address_line_2": "Bar", "postcode": "BA5 5AB"},
    }

    api_client_request.post(
        service.id,
        "v2_notifications.post_precompiled_letter_notification",
        _data=data,
    )

    notifications = Notification.query.all()
    assert len(notifications) == 1
    assert notifications[0].reply_to_text is None


def test_post_letter_notification_persists_notification_reply_to_text_for_template(
    api_client_request, notify_db_session, mocker
):
    mocker.patch("app.celery.letters_pdf_tasks.get_pdf_for_templated_letter.apply_async")

    service = create_service(service_permissions=[LETTER_TYPE])
    create_letter_contact(service=service, contact_block="the default", is_default=True)
    template_letter_contact = create_letter_contact(service=service, contact_block="not the default", is_default=False)
    template = create_template(service=service, template_type="letter", reply_to=template_letter_contact.id)
    data = {
        "template_id": template.id,
        "personalisation": {"address_line_1": "Foo", "address_line_2": "Bar", "postcode": "BA5 5AB"},
    }

    api_client_request.post(
        service.id,
        "v2_notifications.post_notification",
        notification_type="letter",
        _data=data,
    )

    notifications = Notification.query.all()
    assert len(notifications) == 1
    assert notifications[0].reply_to_text == "not the default"


def test_post_precompiled_letter_with_invalid_base64(api_client_request, mocker):
    sample_service = create_service(service_permissions=["letter"])
    mocker.patch("app.v2.notifications.post_notifications.upload_letter_pdf")

    data = {"reference": "letter-reference", "content": "hi"}

    resp_json = api_client_request.post(
        sample_service.id, "v2_notifications.post_precompiled_letter_notification", _data=data, _expected_status=400
    )

    assert resp_json["errors"][0]["message"] == "Cannot decode letter content (invalid base64 encoding)"

    assert not Notification.query.first()


@pytest.mark.parametrize(
    "notification_postage, expected_postage", [("second", "second"), ("first", "first"), (None, "second")]
)
def test_post_precompiled_letter_notification_returns_201(
    api_client_request, mocker, notification_postage, expected_postage
):
    sample_service = create_service(service_permissions=["letter"])
    s3mock = mocker.patch("app.v2.notifications.post_notifications.upload_letter_pdf")
    mocker.patch("app.celery.letters_pdf_tasks.notify_celery.send_task")
    data = {"reference": "letter-reference", "content": "bGV0dGVyLWNvbnRlbnQ="}
    if notification_postage:
        data["postage"] = notification_postage

    resp_json = api_client_request.post(
        sample_service.id, "v2_notifications.post_precompiled_letter_notification", _data=data
    )

    s3mock.assert_called_once_with(ANY, b"letter-content", precompiled=True)

    notification = Notification.query.one()

    assert notification.billable_units == 0
    assert notification.status == NOTIFICATION_PENDING_VIRUS_CHECK
    assert notification.postage == expected_postage

    assert resp_json == {"id": str(notification.id), "reference": "letter-reference", "postage": expected_postage}


def test_post_precompiled_letter_notification_if_s3_upload_fails_notification_is_not_persisted(
    api_client_request, mocker
):
    sample_service = create_service(service_permissions=["letter"])
    persist_letter_mock = mocker.patch(
        "app.v2.notifications.post_notifications.create_letter_notification", side_effect=create_letter_notification
    )
    s3mock = mocker.patch("app.v2.notifications.post_notifications.upload_letter_pdf", side_effect=Exception())
    mocker.patch("app.celery.letters_pdf_tasks.notify_celery.send_task")
    data = {"reference": "letter-reference", "content": "bGV0dGVyLWNvbnRlbnQ="}

    with pytest.raises(expected_exception=Exception):
        api_client_request.post(sample_service.id, "v2_notifications.post_precompiled_letter_notification", _data=data)

    assert s3mock.called
    assert persist_letter_mock.called
    assert Notification.query.count() == 0


def test_post_letter_notification_throws_error_for_invalid_postage(api_client_request):
    sample_service = create_service(service_permissions=["letter"])
    data = {"reference": "letter-reference", "content": "bGV0dGVyLWNvbnRlbnQ=", "postage": "europe"}
    resp_json = api_client_request.post(
        sample_service.id, "v2_notifications.post_precompiled_letter_notification", _data=data, _expected_status=400
    )
    assert resp_json["errors"][0]["message"] == "postage invalid. It must be either first or second."

    assert not Notification.query.first()


@pytest.mark.parametrize("content_type", ["application/json", "application/text"])
def test_post_letter_notification_when_payload_is_invalid_json_returns_400(client, sample_service, content_type):
    auth_header = create_service_authorization_header(service_id=sample_service.id)
    payload_not_json = {
        "template_id": "dont-convert-to-json",
    }
    response = client.post(
        path="/v2/notifications/letter",
        data=payload_not_json,
        headers=[("Content-Type", content_type), auth_header],
    )

    assert response.status_code == 400
    error_msg = json.loads(response.get_data(as_text=True))["errors"][0]["message"]

    assert error_msg == "Invalid JSON supplied in POST data"
