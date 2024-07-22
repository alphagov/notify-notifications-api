import uuid
from unittest.mock import call

import pytest
from flask import current_app, json

from app.constants import (
    EMAIL_TYPE,
    INTERNATIONAL_SMS_TYPE,
    LETTER_TYPE,
    NOTIFICATION_CREATED,
    SMS_TYPE,
)
from app.dao import templates_dao
from app.dao.service_sms_sender_dao import dao_update_service_sms_sender
from app.models import Notification
from app.schema_validation import validate
from app.v2.errors import RateLimitError
from app.v2.notifications.notification_schemas import (
    post_email_response,
    post_sms_response,
)
from tests import create_service_authorization_header
from tests.app.db import (
    create_reply_to_email,
    create_service,
    create_service_sms_sender,
    create_service_with_inbound_number,
    create_template,
)


@pytest.mark.parametrize("reference", [None, "reference_from_client"])
def test_post_sms_notification_returns_201(api_client_request, sample_template_with_placeholders, mocker, reference):
    mocked = mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")
    data = {
        "phone_number": "+447700900855",
        "template_id": str(sample_template_with_placeholders.id),
        "personalisation": {" Name": "Jo"},
    }
    if reference:
        data.update({"reference": reference})

    resp_json = api_client_request.post(
        sample_template_with_placeholders.service_id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )

    assert validate(resp_json, post_sms_response) == resp_json
    notifications = Notification.query.all()
    assert len(notifications) == 1
    assert notifications[0].status == NOTIFICATION_CREATED
    notification_id = notifications[0].id
    assert notifications[0].postage is None
    assert notifications[0].document_download_count is None
    assert resp_json["id"] == str(notification_id)
    assert resp_json["reference"] == reference
    assert resp_json["content"]["body"] == sample_template_with_placeholders.content.replace("(( Name))", "Jo")
    assert resp_json["content"]["from_number"] == current_app.config["FROM_NUMBER"]
    assert f"v2/notifications/{notification_id}" in resp_json["uri"]
    assert resp_json["template"]["id"] == str(sample_template_with_placeholders.id)
    assert resp_json["template"]["version"] == sample_template_with_placeholders.version
    assert (
        f"services/{sample_template_with_placeholders.service_id}/templates/{sample_template_with_placeholders.id}"
        in resp_json["template"]["uri"]
    )
    assert not resp_json["scheduled_for"]
    assert mocked.called


def test_post_sms_notification_uses_inbound_number_as_sender(api_client_request, notify_db_session, mocker):
    service = create_service_with_inbound_number(inbound_number="1")

    template = create_template(service=service, content="Hello (( Name))\nYour thing is due soon")
    mocked = mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")
    data = {"phone_number": "+447700900855", "template_id": str(template.id), "personalisation": {" Name": "Jo"}}

    resp_json = api_client_request.post(
        service.id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )

    assert validate(resp_json, post_sms_response) == resp_json
    notifications = Notification.query.all()
    assert len(notifications) == 1
    notification_id = notifications[0].id
    assert resp_json["id"] == str(notification_id)
    assert resp_json["content"]["from_number"] == "1"
    assert notifications[0].reply_to_text == "1"
    mocked.assert_called_once_with([str(notification_id)], queue="send-sms-tasks")


def test_post_sms_notification_uses_inbound_number_reply_to_as_sender(api_client_request, notify_db_session, mocker):
    service = create_service_with_inbound_number(inbound_number="07123123123")

    template = create_template(service=service, content="Hello (( Name))\nYour thing is due soon")
    mocked = mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")
    data = {"phone_number": "+447700900855", "template_id": str(template.id), "personalisation": {" Name": "Jo"}}

    resp_json = api_client_request.post(
        service.id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )

    assert validate(resp_json, post_sms_response) == resp_json
    notifications = Notification.query.all()
    assert len(notifications) == 1
    notification_id = notifications[0].id
    assert resp_json["id"] == str(notification_id)
    assert resp_json["content"]["from_number"] == "447123123123"
    assert notifications[0].reply_to_text == "447123123123"
    mocked.assert_called_once_with([str(notification_id)], queue="send-sms-tasks")


def test_post_sms_notification_returns_201_with_sms_sender_id(
    api_client_request, sample_template_with_placeholders, mocker
):
    sms_sender = create_service_sms_sender(service=sample_template_with_placeholders.service, sms_sender="123456")
    mocked = mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")
    data = {
        "phone_number": "+447700900855",
        "template_id": str(sample_template_with_placeholders.id),
        "personalisation": {" Name": "Jo"},
        "sms_sender_id": str(sms_sender.id),
    }

    resp_json = api_client_request.post(
        sample_template_with_placeholders.service_id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )

    assert validate(resp_json, post_sms_response) == resp_json
    assert resp_json["content"]["from_number"] == sms_sender.sms_sender
    notifications = Notification.query.all()
    assert len(notifications) == 1
    assert notifications[0].reply_to_text == sms_sender.sms_sender
    mocked.assert_called_once_with([resp_json["id"]], queue="send-sms-tasks")


def test_post_sms_notification_uses_sms_sender_id_reply_to(
    api_client_request, sample_template_with_placeholders, mocker
):
    sms_sender = create_service_sms_sender(service=sample_template_with_placeholders.service, sms_sender="07123123123")
    mocked = mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")
    data = {
        "phone_number": "+447700900855",
        "template_id": str(sample_template_with_placeholders.id),
        "personalisation": {" Name": "Jo"},
        "sms_sender_id": str(sms_sender.id),
    }

    resp_json = api_client_request.post(
        sample_template_with_placeholders.service_id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )

    assert validate(resp_json, post_sms_response) == resp_json
    assert validate(resp_json, post_sms_response) == resp_json
    assert resp_json["content"]["from_number"] == "447123123123"
    notifications = Notification.query.all()
    assert len(notifications) == 1
    assert notifications[0].reply_to_text == "447123123123"
    mocked.assert_called_once_with([resp_json["id"]], queue="send-sms-tasks")


def test_notification_reply_to_text_is_original_value_if_sender_is_changed_after_post_notification(
    api_client_request, sample_template, mocker
):
    sms_sender = create_service_sms_sender(service=sample_template.service, sms_sender="123456", is_default=False)
    mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")
    data = {
        "phone_number": "+447700900855",
        "template_id": str(sample_template.id),
        "sms_sender_id": str(sms_sender.id),
    }

    api_client_request.post(
        sample_template.service_id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )

    dao_update_service_sms_sender(
        service_id=sample_template.service_id,
        service_sms_sender_id=sms_sender.id,
        is_default=sms_sender.is_default,
        sms_sender="updated",
    )

    notifications = Notification.query.all()
    assert len(notifications) == 1
    assert notifications[0].reply_to_text == "123456"


# This test checks the memory_caches created in `app.serialised_models`. They have a TTL of 2 seconds (as of writing
# this comment), which has sometimes caused this test to flake when running slowly (eg on CI). Normally we'd use
# freezegun to stop time elapsing, but the way we create the cache configures the timers to use `time.monotonic` before
# freezegun is able to monkeypatch them. We could manually monkeypatch them but it reaches deep into the internal
# implementation of TimedCaches, so feels a bit more gross than just accepting that this test interacts with time and
# in rare slow runs, may fail.
@pytest.mark.flaky(max_runs=3, min_passes=1)
def test_should_cache_template_lookups_in_memory(mocker, api_client_request, sample_template):
    mock_get_template = mocker.patch(
        "app.dao.templates_dao.dao_get_template_by_id_and_service_id",
        wraps=templates_dao.dao_get_template_by_id_and_service_id,
    )
    mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")

    data = {
        "phone_number": "+447700900855",
        "template_id": str(sample_template.id),
    }

    for _ in range(5):
        api_client_request.post(
            sample_template.service_id,
            "v2_notifications.post_notification",
            notification_type="sms",
            _data=data,
        )

    assert mock_get_template.call_count == 1
    assert mock_get_template.call_args_list == [
        call(service_id=str(sample_template.service_id), template_id=str(sample_template.id), version=None)
    ]
    assert Notification.query.count() == 5


def test_should_cache_template_and_service_in_redis(mocker, api_client_request, sample_template):
    from app.schemas import service_schema, template_schema

    mock_redis_get = mocker.patch(
        "app.redis_store.get",
        return_value=None,
    )
    mock_redis_set = mocker.patch(
        "app.redis_store.set",
    )

    mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")

    data = {
        "phone_number": "+447700900855",
        "template_id": str(sample_template.id),
    }

    api_client_request.post(
        sample_template.service_id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )

    expected_service_key = f"service-{sample_template.service_id}"
    expected_templates_key = f"service-{sample_template.service_id}-template-{sample_template.id}-version-None"

    assert mock_redis_get.call_args_list == [
        call(expected_service_key),
        call(expected_templates_key),
    ]

    service_dict = service_schema.dump(sample_template.service)
    template_dict = template_schema.dump(sample_template)

    assert len(mock_redis_set.call_args_list) == 2

    service_call, templates_call = mock_redis_set.call_args_list

    assert service_call[0][0] == expected_service_key
    assert json.loads(service_call[0][1]) == {"data": service_dict}
    assert service_call[1]["ex"] == 2_419_200

    assert templates_call[0][0] == expected_templates_key
    assert json.loads(templates_call[0][1]) == {"data": template_dict}
    assert templates_call[1]["ex"] == 2_419_200


def test_should_return_template_if_found_in_redis(mocker, api_client_request, sample_template):
    from app.schemas import service_schema, template_schema

    service_dict = service_schema.dump(sample_template.service)
    template_dict = template_schema.dump(sample_template)

    mocker.patch(
        "app.redis_store.get",
        side_effect=[
            json.dumps({"data": service_dict}).encode("utf-8"),
            json.dumps({"data": template_dict}).encode("utf-8"),
        ],
    )
    mock_get_template = mocker.patch("app.dao.templates_dao.dao_get_template_by_id_and_service_id")
    mock_get_service = mocker.patch("app.dao.services_dao.dao_fetch_service_by_id")

    mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")

    data = {
        "phone_number": "+447700900855",
        "template_id": str(sample_template.id),
    }

    api_client_request.post(
        sample_template.service_id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )

    assert mock_get_template.called is False
    assert mock_get_service.called is False


@pytest.mark.parametrize(
    "notification_type, key_send_to, send_to",
    [("sms", "phone_number", "+447700900855"), ("email", "email_address", "sample@email.com")],
)
def test_post_notification_returns_400_and_missing_template(
    api_client_request, sample_service, notification_type, key_send_to, send_to
):
    data = {key_send_to: send_to, "template_id": str(uuid.uuid4())}

    error_json = api_client_request.post(
        sample_service.id,
        "v2_notifications.post_notification",
        notification_type=notification_type,
        _data=data,
        _expected_status=400,
    )

    assert error_json["status_code"] == 400
    assert error_json["errors"] == [{"error": "BadRequestError", "message": "Template not found"}]


@pytest.mark.parametrize(
    "notification_type, key_send_to, send_to",
    [
        ("sms", "phone_number", "+447700900855"),
        ("email", "email_address", "sample@email.com"),
        ("letter", "personalisation", {"address_line_1": "The queen", "postcode": "SW1 1AA"}),
    ],
)
def test_post_notification_returns_401_and_well_formed_auth_error(
    client, sample_template, notification_type, key_send_to, send_to
):
    data = {key_send_to: send_to, "template_id": str(sample_template.id)}

    response = client.post(
        path=f"/v2/notifications/{notification_type}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json")],
    )

    assert response.status_code == 401
    assert response.headers["Content-type"] == "application/json"
    error_resp = json.loads(response.get_data(as_text=True))
    assert error_resp["status_code"] == 401
    assert error_resp["errors"] == [
        {"error": "AuthError", "message": "Unauthorized: authentication token must be provided"}
    ]


def test_post_notification_with_too_long_reference_returns_400(
    api_client_request,
    sample_letter_template,
    sample_sms_template,
    sample_email_template,
):
    types_and_templates_and_data = [
        ("letter", sample_letter_template, {"personalisation": {"address_line_1": "The king", "postcode": "SW1 1AA"}}),
        ("email", sample_email_template, {"email_address": "sample@email.com"}),
        ("sms", sample_sms_template, {"phone_number": "+447700900855"}),
    ]

    for notification_type, template, data in types_and_templates_and_data:
        data["template_id"] = template.id
        data["reference"] = "a" * 1001

        error_resp = api_client_request.post(
            template.service_id,
            "v2_notifications.post_notification",
            notification_type=notification_type,
            _data=data,
            _expected_status=400,
            headers=[("Content-Type", "application/json")],
        )

        assert error_resp["status_code"] == 400
        assert error_resp["errors"] == [
            {"error": "ValidationError", "message": "reference " + ("a" * 1001) + " is too long"}
        ]


def test_post_notification_errors_with_too_much_qr_code_data(
    api_client_request,
    sample_service_full_permissions,
):
    letter_template = create_template(
        sample_service_full_permissions, template_type=LETTER_TYPE, postage="second", content="qr: ((qrcode))"
    )

    data = {
        "personalisation": {
            "address_line_1": "The king",
            "address_line_2": "Buckingham Palace",
            "postcode": "SW1 1AA",
            "qrcode": "too much data" * 50,
        },
        "template_id": letter_template.id,
        "reference": "qr code",
    }

    error_resp = api_client_request.post(
        letter_template.service_id,
        "v2_notifications.post_notification",
        notification_type=LETTER_TYPE,
        _data=data,
        _expected_status=400,
        headers=[("Content-Type", "application/json")],
    )

    assert error_resp["status_code"] == 400
    assert error_resp["errors"] == [
        {
            "error": "ValidationError",
            "message": "Cannot create a usable QR code - the link is too long",
            "data": "too much data" * 50,
            "max_bytes": 504,
            "num_bytes": 650,
        }
    ]


@pytest.mark.parametrize(
    "notification_type, key_send_to, send_to",
    [("sms", "phone_number", "+447700900855"), ("email", "email_address", "sample@email.com")],
)
def test_notification_returns_400_and_for_schema_problems(
    api_client_request, sample_template, notification_type, key_send_to, send_to
):
    data = {key_send_to: send_to, "template": str(sample_template.id)}

    error_resp = api_client_request.post(
        sample_template.service_id,
        "v2_notifications.post_notification",
        notification_type=notification_type,
        _data=data,
        _expected_status=400,
    )

    assert error_resp["status_code"] == 400
    assert {"error": "ValidationError", "message": "template_id is a required property"} in error_resp["errors"]
    assert {
        "error": "ValidationError",
        "message": "Additional properties are not allowed (template was unexpected)",
    } in error_resp["errors"]


@pytest.mark.parametrize("reference", [None, "reference_from_client"])
def test_post_email_notification_returns_201(
    api_client_request, sample_email_template_with_placeholders, mocker, reference
):
    mocked = mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    data = {
        "email_address": sample_email_template_with_placeholders.service.users[0].email_address,
        "template_id": sample_email_template_with_placeholders.id,
        "personalisation": {"name": "Bob"},
    }
    if reference:
        data.update({"reference": reference})

    resp_json = api_client_request.post(
        sample_email_template_with_placeholders.service_id,
        "v2_notifications.post_notification",
        notification_type="email",
        _data=data,
    )

    assert validate(resp_json, post_email_response) == resp_json
    notification = Notification.query.one()
    assert notification.status == NOTIFICATION_CREATED
    assert notification.postage is None
    assert resp_json["id"] == str(notification.id)
    assert resp_json["reference"] == reference
    assert notification.reference is None
    assert notification.reply_to_text is None
    assert notification.document_download_count is None
    assert resp_json["content"]["body"] == sample_email_template_with_placeholders.content.replace("((name))", "Bob")
    assert resp_json["content"]["subject"] == sample_email_template_with_placeholders.subject.replace("((name))", "Bob")
    assert resp_json["content"]["from_email"] == "{}@{}".format(
        sample_email_template_with_placeholders.service.email_sender_local_part,
        current_app.config["NOTIFY_EMAIL_DOMAIN"],
    )
    assert f"v2/notifications/{notification.id}" in resp_json["uri"]
    assert resp_json["template"]["id"] == str(sample_email_template_with_placeholders.id)
    assert resp_json["template"]["version"] == sample_email_template_with_placeholders.version
    assert (
        f"services/{str(sample_email_template_with_placeholders.service_id)}/templates/{str(sample_email_template_with_placeholders.id)}"
        in resp_json["template"]["uri"]
    )
    assert not resp_json["scheduled_for"]
    assert mocked.called


@pytest.mark.parametrize(
    (
        "personalisation, expected_status, expect_error_message, expect_upload, "
        "expected_confirmation, expected_retention, expected_filename"
    ),
    (
        ({"doc": "just some text"}, 201, None, False, None, None, None),
        ({"doc": {"file": False}}, 400, None, False, None, None, None),
        ({"doc": {"file": "YSxiLGMKMSwyLDMK"}}, 201, None, True, True, "26 weeks", None),
        ({"doc": {"file": "YSxiLGMKMSwyLDMK", "is_csv": None}}, 201, None, True, True, "26 weeks", None),
        ({"doc": {"file": "YSxiLGMKMSwyLDMK", "is_csv": True}}, 201, None, True, True, "26 weeks", None),
        ({"doc": {"file": "YSxiLGMKMSwyLDMK", "is_csv": False}}, 201, None, True, True, "26 weeks", None),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "is_csv": "bad"}},
            400,
            "Unsupported value for is_csv: bad. Use a boolean true or false value.",
            False,
            None,
            None,
            None,
        ),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "is_csv": True, "confirm_email_before_download": None}},
            201,
            None,
            True,
            True,
            "26 weeks",
            None,
        ),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "is_csv": True, "confirm_email_before_download": True}},
            201,
            None,
            True,
            True,
            "26 weeks",
            None,
        ),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "is_csv": True, "confirm_email_before_download": False}},
            201,
            None,
            True,
            False,
            "26 weeks",
            None,
        ),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "is_csv": True, "confirm_email_before_download": "potato"}},
            400,
            "Unsupported value for confirm_email_before_download: potato. Use a boolean true or false value.",
            False,
            None,
            None,
            None,
        ),
        ({"doc": {"file": "YSxiLGMKMSwyLDMK", "retention_period": None}}, 201, None, True, True, "26 weeks", None),
        ({"doc": {"file": "YSxiLGMKMSwyLDMK", "retention_period": "1 week"}}, 201, None, True, True, "1 week", None),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "retention_period": "70 weeks"}},
            201,
            None,
            True,
            True,
            "70 weeks",
            None,
        ),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "retention_period": "9999 weeks"}},
            400,
            "Unsupported value for retention_period: 9999 weeks",
            False,
            None,
            None,
            None,
        ),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "retention_period": "1 month"}},
            400,
            "Unsupported value for retention_period: 1 month",
            False,
            None,
            None,
            None,
        ),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "retention_period": False}},
            400,
            "Unsupported value for retention_period: False",
            False,
            None,
            None,
            None,
        ),
        ({"doc": {"file": "YSxiLGMKMSwyLDMK", "other": "attribute"}}, 400, None, False, None, None, None),
        ({"doc": {"potato": "YSxiLGMKMSwyLDMK"}}, 201, None, False, None, None, None),
        ({"doc": {"potato": "YSxiLGMKMSwyLDMK", "is_csv": "cucumber"}}, 201, None, False, None, None, None),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "filename": "file.csv"}},
            201,
            None,
            False,
            None,
            None,
            "file.csv",
        ),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "filename": "file"}},
            400,
            "`filename` must end with a file extension. For example, filename.csv",
            False,
            None,
            None,
            None,
        ),
        (
            {"doc": {"file": "YSxiLGMKMSwyLDMK", "is_csv": True, "filename": "file.csv"}},
            400,
            "Do not set a value for `is_csv` if `filename` is set.",
            False,
            None,
            None,
            None,
        ),
    ),
)
def test_post_email_notification_validates_personalisation_send_a_file_values(
    api_client_request,
    mocker,
    personalisation,
    expected_status,
    expect_error_message,
    expect_upload,
    expected_confirmation,
    expected_retention,
    expected_filename,
):
    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    document_download_mock = mocker.patch("app.v2.notifications.post_notifications.document_download_client")
    document_download_mock.upload_document.side_effect = (
        lambda service_id, content, is_csv, confirmation_email, **kwargs: f"{content}-link"
    )

    service = create_service(
        contact_link="test@notify.example",
        service_permissions=[EMAIL_TYPE],
    )

    template = create_template(
        service,
        template_type=EMAIL_TYPE,
        subject="Hello",
        content="Hello.\nHere's a file for you: ((doc))",
    )

    data = {
        "email_address": template.service.users[0].email_address,
        "template_id": template.id,
        "personalisation": personalisation,
    }

    response = api_client_request.post(
        template.service_id,
        "v2_notifications.post_notification",
        notification_type="email",
        _data=data,
        _expected_status=expected_status,
    )

    if expect_error_message:
        assert expect_error_message in response["errors"][0]["message"]

    if expect_upload:
        assert document_download_mock.upload_document.call_args_list == [
            mocker.call(
                str(template.service_id),
                "YSxiLGMKMSwyLDMK",
                mocker.ANY,
                confirmation_email=template.service.users[0].email_address if expected_confirmation else None,
                retention_period=expected_retention,
                filename=expected_filename,
            )
        ]


@pytest.mark.parametrize(
    "recipient, notification_type",
    [
        ("simulate-delivered@notifications.service.gov.uk", EMAIL_TYPE),
        ("simulate-delivered-2@notifications.service.gov.uk", EMAIL_TYPE),
        ("simulate-delivered-3@notifications.service.gov.uk", EMAIL_TYPE),
        ("07700 900000", "sms"),
        ("07700 900111", "sms"),
        ("07700 900222", "sms"),
    ],
)
def test_should_not_persist_or_send_notification_if_simulated_recipient(
    api_client_request, recipient, notification_type, sample_email_template, sample_template, mocker
):
    apply_async = mocker.patch(f"app.celery.provider_tasks.deliver_{notification_type}.apply_async")

    if notification_type == "sms":
        data = {"phone_number": recipient, "template_id": str(sample_template.id)}
    else:
        data = {"email_address": recipient, "template_id": str(sample_email_template.id)}

    resp_json = api_client_request.post(
        sample_email_template.service_id,
        "v2_notifications.post_notification",
        notification_type=notification_type,
        _data=data,
    )

    apply_async.assert_not_called()
    assert resp_json["id"]
    assert Notification.query.count() == 0


@pytest.mark.parametrize(
    "notification_type, key_send_to, send_to",
    [("sms", "phone_number", "07700 900 855"), ("email", "email_address", "sample@email.com")],
)
def test_returns_a_429_limit_exceeded_if_rate_limit_exceeded(
    api_client_request, sample_service, mocker, notification_type, key_send_to, send_to
):
    sample = create_template(service=sample_service, template_type=notification_type)
    persist_mock = mocker.patch("app.v2.notifications.post_notifications.persist_notification")
    deliver_mock = mocker.patch("app.v2.notifications.post_notifications.send_notification_to_queue_detached")
    mocker.patch(
        "app.v2.notifications.post_notifications.check_rate_limiting",
        side_effect=RateLimitError("LIMIT", "INTERVAL", "TYPE"),
    )

    data = {key_send_to: send_to, "template_id": str(sample.id)}

    resp_json = api_client_request.post(
        sample_service.id,
        "v2_notifications.post_notification",
        notification_type=notification_type,
        _data=data,
        _expected_status=429,
    )

    assert resp_json["errors"][0]["error"] == "RateLimitError"
    assert resp_json["errors"][0]["message"] == (
        "Exceeded rate limit for key type TYPE of LIMIT requests per INTERVAL seconds"
    )
    assert resp_json["status_code"] == 429

    assert not persist_mock.called
    assert not deliver_mock.called


def test_post_sms_notification_returns_400_if_not_allowed_to_send_int_sms(
    api_client_request,
    notify_db_session,
):
    service = create_service(service_permissions=[SMS_TYPE])
    template = create_template(service=service)

    data = {"phone_number": "20-12-1234-1234", "template_id": template.id}

    error_json = api_client_request.post(
        service.id, "v2_notifications.post_notification", notification_type="sms", _data=data, _expected_status=400
    )

    assert error_json["status_code"] == 400
    assert error_json["errors"] == [
        {"error": "BadRequestError", "message": "Cannot send to international mobile numbers"}
    ]


def test_post_sms_notification_with_archived_reply_to_id_returns_400(api_client_request, sample_template, mocker):
    archived_sender = create_service_sms_sender(sample_template.service, "12345", is_default=False, archived=True)
    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    data = {"phone_number": "+447700900855", "template_id": sample_template.id, "sms_sender_id": archived_sender.id}

    resp_json = api_client_request.post(
        sample_template.service_id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
        _expected_status=400,
    )

    assert (
        f"sms_sender_id {archived_sender.id} does not exist in database for service id {sample_template.service_id}"
        in resp_json["errors"][0]["message"]
    )
    assert "BadRequestError" in resp_json["errors"][0]["error"]


@pytest.mark.parametrize(
    "recipient,label,permission_type, notification_type,expected_error",
    [
        ("07700 900000", "phone_number", "email", "sms", "text messages"),
        ("someone@test.com", "email_address", "sms", "email", "emails"),
    ],
)
def test_post_sms_notification_returns_400_if_not_allowed_to_send_notification(
    notify_db_session, api_client_request, recipient, label, permission_type, notification_type, expected_error
):
    service = create_service(service_permissions=[permission_type])
    sample_template_without_permission = create_template(service=service, template_type=notification_type)
    data = {label: recipient, "template_id": sample_template_without_permission.id}

    error_json = api_client_request.post(
        sample_template_without_permission.service_id,
        "v2_notifications.post_notification",
        notification_type=notification_type,
        _data=data,
        _expected_status=400,
    )

    assert error_json["status_code"] == 400
    assert error_json["errors"] == [
        {"error": "BadRequestError", "message": f"Service is not allowed to send {expected_error}"}
    ]


@pytest.mark.parametrize("restricted", [True, False])
def test_post_sms_notification_returns_400_if_number_not_in_guest_list(
    notify_db_session, api_client_request, restricted
):
    service = create_service(restricted=restricted, service_permissions=[SMS_TYPE, INTERNATIONAL_SMS_TYPE])
    template = create_template(service=service)

    data = {
        "phone_number": "+3225484211",
        "template_id": template.id,
    }

    error_json = api_client_request.post(
        service.id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _api_key_type="team",
        _data=data,
        _expected_status=400,
    )

    assert error_json["status_code"] == 400
    assert error_json["errors"] == [
        {"error": "BadRequestError", "message": "Can’t send to this recipient using a team-only API key"}
    ]


def test_post_sms_notification_returns_201_if_allowed_to_send_int_sms(
    sample_service,
    sample_template,
    api_client_request,
    mocker,
):
    mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")

    data = {"phone_number": "20-12-1234-1234", "template_id": sample_template.id}

    api_client_request.post(
        sample_service.id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )


def test_post_sms_should_persist_supplied_sms_number(api_client_request, sample_template_with_placeholders, mocker):
    mocked = mocker.patch("app.celery.provider_tasks.deliver_sms.apply_async")
    data = {
        "phone_number": "+(44) 77009-00855",
        "template_id": str(sample_template_with_placeholders.id),
        "personalisation": {" Name": "Jo"},
    }

    resp_json = api_client_request.post(
        sample_template_with_placeholders.service_id,
        "v2_notifications.post_notification",
        notification_type="sms",
        _data=data,
    )

    notifications = Notification.query.all()
    assert len(notifications) == 1
    notification_id = notifications[0].id
    assert "+(44) 77009-00855" == notifications[0].to
    assert resp_json["id"] == str(notification_id)
    assert mocked.called


def test_post_notification_raises_bad_request_if_not_valid_notification_type(api_client_request, sample_service):
    error_json = api_client_request.post(
        sample_service.id, "v2_notifications.post_notification", notification_type="foo", _data={}, _expected_status=404
    )
    assert "The requested URL was not found on the server." in error_json["message"]


@pytest.mark.parametrize("notification_type", ["sms", "email"])
def test_post_notification_with_wrong_type_of_sender(
    api_client_request, sample_template, sample_email_template, notification_type, fake_uuid
):
    if notification_type == EMAIL_TYPE:
        template = sample_email_template
        form_label = "sms_sender_id"
        data = {"email_address": "test@test.com", "template_id": str(sample_email_template.id), form_label: fake_uuid}
    else:
        template = sample_template
        form_label = "email_reply_to_id"
        data = {"phone_number": "+447700900855", "template_id": str(template.id), form_label: fake_uuid}

    resp_json = api_client_request.post(
        template.service_id,
        "v2_notifications.post_notification",
        notification_type=notification_type,
        _data=data,
        _expected_status=400,
    )

    assert f"Additional properties are not allowed ({form_label} was unexpected)" in resp_json["errors"][0]["message"]
    assert "ValidationError" in resp_json["errors"][0]["error"]


def test_post_email_notification_with_valid_reply_to_id_returns_201(api_client_request, sample_email_template, mocker):
    reply_to_email = create_reply_to_email(sample_email_template.service, "test@test.com")
    mocked = mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    data = {
        "email_address": sample_email_template.service.users[0].email_address,
        "template_id": sample_email_template.id,
        "email_reply_to_id": reply_to_email.id,
    }

    resp_json = api_client_request.post(
        sample_email_template.service_id,
        "v2_notifications.post_notification",
        notification_type="email",
        _data=data,
    )

    assert validate(resp_json, post_email_response) == resp_json
    notification = Notification.query.first()
    assert notification.reply_to_text == "test@test.com"
    assert resp_json["id"] == str(notification.id)
    assert mocked.called

    assert notification.reply_to_text == reply_to_email.email_address


def test_post_email_notification_with_invalid_reply_to_id_returns_400(
    api_client_request, sample_email_template, mocker, fake_uuid
):
    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    data = {
        "email_address": sample_email_template.service.users[0].email_address,
        "template_id": sample_email_template.id,
        "email_reply_to_id": fake_uuid,
    }

    resp_json = api_client_request.post(
        sample_email_template.service_id,
        "v2_notifications.post_notification",
        notification_type="email",
        _data=data,
        _expected_status=400,
    )

    assert (
        f"email_reply_to_id {fake_uuid} does not exist in database for service id {sample_email_template.service_id}"
        in resp_json["errors"][0]["message"]
    )
    assert "BadRequestError" in resp_json["errors"][0]["error"]


def test_post_email_notification_with_archived_reply_to_id_returns_400(
    api_client_request, sample_email_template, mocker
):
    archived_reply_to = create_reply_to_email(
        sample_email_template.service, "reply_to@test.com", is_default=False, archived=True
    )
    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    data = {
        "email_address": "test@test.com",
        "template_id": sample_email_template.id,
        "email_reply_to_id": archived_reply_to.id,
    }

    resp_json = api_client_request.post(
        sample_email_template.service_id,
        "v2_notifications.post_notification",
        notification_type="email",
        _data=data,
        _expected_status=400,
    )

    assert (
        f"email_reply_to_id {archived_reply_to.id} does not exist in database for service "
        f"id {sample_email_template.service_id}"
    ) in resp_json["errors"][0]["message"]
    assert "BadRequestError" in resp_json["errors"][0]["error"]


def test_post_email_notification_with_unsubscribe_link_returns_201(api_client_request, sample_email_template, mocker):
    mocked = mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    unsubscribe_link = "https://www.someservice.com/unsubscribe?for=anne@example.com"
    data = {
        "email_address": sample_email_template.service.users[0].email_address,
        "template_id": sample_email_template.id,
        "one_click_unsubscribe_url": unsubscribe_link,
    }

    response_json = api_client_request.post(
        sample_email_template.service_id,
        "v2_notifications.post_notification",
        notification_type="email",
        _data=data,
    )

    assert validate(response_json, post_email_response) == response_json
    notification = Notification.query.first()
    assert response_json["id"] == str(notification.id)

    assert notification.unsubscribe_link == unsubscribe_link == response_json["content"]["one_click_unsubscribe_url"]
    assert mocked.called


@pytest.mark.parametrize(
    "extra, expect_email_confirmation, expect_retention_period",
    (
        ({}, True, "26 weeks"),
        ({"is_csv": None}, True, "26 weeks"),
        ({"is_csv": False}, True, "26 weeks"),
        ({"is_csv": True}, True, "26 weeks"),
        ({"confirm_email_before_download": False}, False, "26 weeks"),
        ({"confirm_email_before_download": True}, True, "26 weeks"),
        ({"retention_period": None}, True, "26 weeks"),
        ({"retention_period": "1 week"}, True, "1 week"),
        ({"retention_period": "4 weeks"}, True, "4 weeks"),
    ),
)
def test_post_notification_with_document_upload(
    api_client_request, notify_db_session, mocker, extra, expect_email_confirmation, expect_retention_period
):
    service = create_service(service_permissions=[EMAIL_TYPE])
    service.contact_link = "contact.me@gov.uk"
    template = create_template(
        service=service, template_type="email", content="Document 1: ((first_link)). Document 2: ((second_link))"
    )

    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    document_download_mock = mocker.patch("app.v2.notifications.post_notifications.document_download_client")
    document_download_mock.upload_document.side_effect = lambda service_id, content, is_csv, **kwargs: f"{content}-link"

    data = {
        "email_address": service.users[0].email_address,
        "template_id": template.id,
        "personalisation": {"first_link": {"file": "abababab", **extra}, "second_link": {"file": "cdcdcdcd", **extra}},
    }

    resp_json = api_client_request.post(
        service.id,
        "v2_notifications.post_notification",
        notification_type="email",
        _data=data,
    )

    assert validate(resp_json, post_email_response) == resp_json

    confirmation_email = data["email_address"] if expect_email_confirmation else None

    assert document_download_mock.upload_document.call_args_list == [
        call(
            str(service.id),
            "abababab",
            extra.get("is_csv"),
            confirmation_email=confirmation_email,
            retention_period=expect_retention_period,
            filename=None,
        ),
        call(
            str(service.id),
            "cdcdcdcd",
            extra.get("is_csv"),
            confirmation_email=confirmation_email,
            retention_period=expect_retention_period,
            filename=None,
        ),
    ]

    notification = Notification.query.one()
    assert notification.status == NOTIFICATION_CREATED
    assert notification.personalisation == {"first_link": "abababab-link", "second_link": "cdcdcdcd-link"}
    assert notification.document_download_count == 2

    assert resp_json["content"]["body"] == "Document 1: abababab-link. Document 2: cdcdcdcd-link"


def test_post_notification_with_document_upload_simulated(api_client_request, notify_db_session, mocker):
    service = create_service(service_permissions=[EMAIL_TYPE])
    service.contact_link = "contact.me@gov.uk"
    template = create_template(service=service, template_type="email", content="Document: ((document))")

    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    document_download_mock = mocker.patch("app.v2.notifications.post_notifications.document_download_client")
    document_download_mock.get_upload_url_for_simulated_email.return_value = "https://document-url"

    data = {
        "email_address": "simulate-delivered@notifications.service.gov.uk",
        "template_id": template.id,
        "personalisation": {"document": {"file": "abababab"}},
    }

    resp_json = api_client_request.post(
        service.id,
        "v2_notifications.post_notification",
        notification_type="email",
        _data=data,
    )

    assert validate(resp_json, post_email_response) == resp_json

    assert resp_json["content"]["body"] == "Document: https://document-url/test-document"


def test_post_notification_without_document_upload_permission(api_client_request, notify_db_session, mocker):
    service = create_service(service_permissions=[EMAIL_TYPE])
    template = create_template(service=service, template_type="email", content="Document: ((document))")

    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    document_download_mock = mocker.patch("app.v2.notifications.post_notifications.document_download_client")
    document_download_mock.upload_document.return_value = "https://document-url/"

    data = {
        "email_address": service.users[0].email_address,
        "template_id": template.id,
        "personalisation": {"document": {"file": "abababab"}},
    }

    api_client_request.post(
        service.id, "v2_notifications.post_notification", notification_type="email", _data=data, _expected_status=400
    )


@pytest.mark.parametrize(
    "notification_type, contact_data",
    [
        ("letter", {}),
        ("sms", {"phone_number": "07700900100"}),
    ],
)
def test_post_notification_with_document_rejects_sms_and_letter(
    api_client_request, sample_service, notification_type, contact_data
):
    sample_service.contact_link = "contact.me@gov.uk"
    template = create_template(
        service=sample_service, template_type=notification_type, content="Document: ((document))"
    )

    data = {"template_id": template.id, "personalisation": {"document": {"file": "abababab"}}} | contact_data

    response_json = api_client_request.post(
        sample_service.id,
        "v2_notifications.post_notification",
        notification_type=notification_type,
        _data=data,
        _expected_status=400,
    )

    assert response_json == {
        "status_code": 400,
        "errors": [
            {
                "error": "BadRequestError",
                "message": "Can only send a file by email",
            }
        ],
    }


def test_post_notification_returns_400_when_get_json_throws_exception(client, sample_email_template):
    auth_header = create_service_authorization_header(service_id=sample_email_template.service_id)
    response = client.post(
        path="v2/notifications/email", data="[", headers=[("Content-Type", "application/json"), auth_header]
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "notification_type, content_type",
    [
        ("email", "application/json"),
        ("email", "application/text"),
        ("sms", "application/json"),
        ("sms", "application/text"),
    ],
)
def test_post_notification_when_payload_is_invalid_json_returns_400(
    client, sample_service, notification_type, content_type
):
    auth_header = create_service_authorization_header(service_id=sample_service.id)
    payload_not_json = {
        "template_id": "dont-convert-to-json",
    }
    response = client.post(
        path=f"/v2/notifications/{notification_type}",
        data=payload_not_json,
        headers=[("Content-Type", content_type), auth_header],
    )

    assert response.status_code == 400
    error_msg = json.loads(response.get_data(as_text=True))["errors"][0]["message"]

    assert error_msg == "Invalid JSON supplied in POST data"


@pytest.mark.parametrize("notification_type", ["email", "sms"])
def test_post_notification_returns_201_when_content_type_is_missing_but_payload_is_valid_json(
    client, sample_service, notification_type, mocker
):
    template = create_template(service=sample_service, template_type=notification_type)
    mocker.patch(f"app.celery.provider_tasks.deliver_{notification_type}.apply_async")
    auth_header = create_service_authorization_header(service_id=sample_service.id)

    valid_json = {
        "template_id": str(template.id),
    }
    if notification_type == "email":
        valid_json.update({"email_address": sample_service.users[0].email_address})
    else:
        valid_json.update({"phone_number": "+447700900855"})
    response = client.post(
        path=f"/v2/notifications/{notification_type}",
        data=json.dumps(valid_json),
        headers=[auth_header],
    )
    assert response.status_code == 201


@pytest.mark.parametrize("notification_type", ["email", "sms"])
def test_post_email_notification_when_data_is_empty_returns_400(api_client_request, sample_service, notification_type):
    resp_json = api_client_request.post(
        sample_service.id,
        "v2_notifications.post_notification",
        notification_type=notification_type,
        _data=None,
        _expected_status=400,
    )

    error_msg = resp_json["errors"][0]["message"]
    if notification_type == "sms":
        assert error_msg == "phone_number is a required property"
    else:
        assert error_msg == "email_address is a required property"
