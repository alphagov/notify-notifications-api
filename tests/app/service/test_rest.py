import json
import uuid
from collections import namedtuple
from datetime import UTC, date, datetime, timedelta
from unittest.mock import ANY

import pytest
from flask import current_app, url_for
from freezegun import freeze_time
from sqlalchemy.exc import SQLAlchemyError

from app.constants import (
    BROADCAST_TYPE,
    EMAIL_AUTH_TYPE,
    EMAIL_TYPE,
    INBOUND_SMS_TYPE,
    INTERNATIONAL_LETTERS,
    INTERNATIONAL_SMS_TYPE,
    KEY_TYPE_NORMAL,
    KEY_TYPE_TEAM,
    KEY_TYPE_TEST,
    LETTER_TYPE,
    NOTIFICATION_RETURNED_LETTER,
    NOTIFICATION_TYPES,
    SERVICE_JOIN_REQUEST_APPROVED,
    SERVICE_JOIN_REQUEST_CANCELLED,
    SERVICE_JOIN_REQUEST_PENDING,
    SMS_TYPE,
    UPLOAD_LETTERS,
)
from app.dao.organisation_dao import dao_add_service_to_organisation
from app.dao.service_join_requests_dao import dao_create_service_join_request
from app.dao.service_user_dao import dao_get_service_user
from app.dao.services_dao import (
    dao_add_user_to_service,
    dao_fetch_service_by_id,
    dao_remove_user_from_service,
)
from app.dao.templates_dao import dao_redact_template
from app.dao.users_dao import get_user_by_id, save_model_user
from app.models import (
    AnnualBilling,
    EmailBranding,
    Notification,
    Permission,
    Service,
    ServiceEmailReplyTo,
    ServiceJoinRequest,
    ServiceLetterContact,
    ServicePermission,
    ServiceSmsSender,
    UnsubscribeRequestReport,
    User,
)
from app.utils import DATETIME_FORMAT
from tests import create_admin_authorization_header
from tests.app.dao.test_service_join_requests_dao import (
    create_service_join_request,
    setup_service_join_request_test_data,
)
from tests.app.db import (
    create_annual_billing,
    create_api_key,
    create_domain,
    create_email_branding,
    create_ft_billing,
    create_ft_notification_status,
    create_inbound_number,
    create_job,
    create_letter_branding,
    create_letter_contact,
    create_notification,
    create_notification_history,
    create_organisation,
    create_reply_to_email,
    create_returned_letter,
    create_service,
    create_service_sms_sender,
    create_service_with_defined_sms_sender,
    create_service_with_inbound_number,
    create_template,
    create_template_folder,
    create_unsubscribe_request,
    create_unsubscribe_request_report,
    create_user,
)


def test_get_service_list(client, service_factory):
    service_factory.get("one")
    service_factory.get("two")
    service_factory.get("three")
    auth_header = create_admin_authorization_header()
    response = client.get("/service", headers=[auth_header])
    assert response.status_code == 200
    json_resp = json.loads(response.get_data(as_text=True))
    assert len(json_resp["data"]) == 3
    assert json_resp["data"][0]["name"] == "one"
    assert json_resp["data"][1]["name"] == "two"
    assert json_resp["data"][2]["name"] == "three"


def test_get_service_list_with_only_active_flag(client, service_factory):
    inactive = service_factory.get("one")
    active = service_factory.get("two")

    inactive.active = False

    auth_header = create_admin_authorization_header()
    response = client.get("/service?only_active=True", headers=[auth_header])
    assert response.status_code == 200
    json_resp = json.loads(response.get_data(as_text=True))
    assert len(json_resp["data"]) == 1
    assert json_resp["data"][0]["id"] == str(active.id)


def test_get_service_list_with_user_id_and_only_active_flag(admin_request, sample_user, service_factory):
    other_user = create_user(email="foo@bar.gov.uk")

    inactive = service_factory.get("one", user=sample_user)
    active = service_factory.get("two", user=sample_user)
    # from other user
    service_factory.get("three", user=other_user)

    inactive.active = False

    json_resp = admin_request.get("service.get_services", user_id=sample_user.id, only_active=True)
    assert len(json_resp["data"]) == 1
    assert json_resp["data"][0]["id"] == str(active.id)


def test_get_service_list_by_user(admin_request, sample_user, service_factory):
    other_user = create_user(email="foo@bar.gov.uk")
    service_factory.get("one", sample_user)
    service_factory.get("two", sample_user)
    service_factory.get("three", other_user)

    json_resp = admin_request.get("service.get_services", user_id=sample_user.id)
    assert len(json_resp["data"]) == 2
    assert json_resp["data"][0]["name"] == "one"
    assert json_resp["data"][1]["name"] == "two"


def test_get_service_list_by_user_should_return_empty_list_if_no_services(admin_request, sample_service):
    # service is already created by sample user
    new_user = create_user(email="foo@bar.gov.uk")

    json_resp = admin_request.get("service.get_services", user_id=new_user.id)
    assert json_resp["data"] == []


def test_get_service_list_should_return_empty_list_if_no_services(notify_db_session, admin_request):
    json_resp = admin_request.get("service.get_services")
    assert len(json_resp["data"]) == 0


def test_find_services_by_name_finds_services(notify_db_session, admin_request, mocker):
    service_1 = create_service(service_name="ABCDEF")
    service_2 = create_service(service_name="ABCGHT")
    mock_get_services_by_partial_name = mocker.patch(
        "app.service.rest.get_services_by_partial_name", return_value=[service_1, service_2]
    )
    response = admin_request.get("service.find_services_by_name", service_name="ABC")["data"]
    mock_get_services_by_partial_name.assert_called_once_with("ABC")
    assert len(response) == 2


def test_find_services_by_name_handles_no_results(notify_db_session, admin_request, mocker):
    mock_get_services_by_partial_name = mocker.patch("app.service.rest.get_services_by_partial_name", return_value=[])
    response = admin_request.get("service.find_services_by_name", service_name="ABC")["data"]
    mock_get_services_by_partial_name.assert_called_once_with("ABC")
    assert len(response) == 0


def test_find_services_by_name_handles_special_characters(notify_db_session, admin_request):
    create_service(service_name="ABCDEF")
    service_2 = create_service(service_name="ZYX % WVU")
    create_service(service_name="123456")
    response = admin_request.get("service.find_services_by_name", service_name="%")
    assert response["data"] == [service_2.serialize_for_org_dashboard()]


def test_find_services_by_name_handles_no_service_name(notify_db_session, admin_request, mocker):
    mock_get_services_by_partial_name = mocker.patch("app.service.rest.get_services_by_partial_name")
    admin_request.get("service.find_services_by_name", _expected_status=400)
    mock_get_services_by_partial_name.assert_not_called()


@freeze_time("2019-05-02")
def test_get_live_services_data(sample_user, admin_request):
    org = create_organisation()

    service = create_service(go_live_user=sample_user, go_live_at=datetime(2018, 1, 1))
    service_2 = create_service(service_name="second", go_live_at=datetime(2019, 1, 1), go_live_user=sample_user)

    sms_template = create_template(service=service)
    email_template = create_template(service=service, template_type="email")
    dao_add_service_to_organisation(service=service, organisation_id=org.id)
    create_ft_billing(bst_date="2019-04-20", template=sms_template)
    create_ft_billing(bst_date="2019-04-20", template=email_template)

    create_annual_billing(service.id, 1, 2019)
    create_annual_billing(service_2.id, 2, 2018)

    response = admin_request.get("service.get_live_services_data")["data"]

    assert len(response) == 2
    assert response == [
        {
            "consent_to_research": None,
            "contact_email": "notify@digital.cabinet-office.gov.uk",
            "contact_mobile": "+447700900986",
            "contact_name": "Test User",
            "email_totals": 1,
            "email_volume_intent": None,
            "letter_totals": 0,
            "letter_volume_intent": None,
            "live_date": "Mon, 01 Jan 2018 00:00:00 GMT",
            "organisation_name": "test_org_1",
            "service_id": ANY,
            "service_name": "Sample service",
            "sms_totals": 1,
            "sms_volume_intent": None,
            "organisation_type": None,
            "free_sms_fragment_limit": 1,
        },
        {
            "consent_to_research": None,
            "contact_email": "notify@digital.cabinet-office.gov.uk",
            "contact_mobile": "+447700900986",
            "contact_name": "Test User",
            "email_totals": 0,
            "email_volume_intent": None,
            "letter_totals": 0,
            "letter_volume_intent": None,
            "live_date": "Tue, 01 Jan 2019 00:00:00 GMT",
            "organisation_name": None,
            "service_id": ANY,
            "service_name": "second",
            "sms_totals": 0,
            "sms_volume_intent": None,
            "organisation_type": None,
            "free_sms_fragment_limit": 2,
        },
    ]


def test_get_service_by_id(admin_request, sample_service):
    json_resp = admin_request.get("service.get_service_by_id", service_id=sample_service.id)
    assert json_resp["data"]["name"] == sample_service.name
    assert json_resp["data"]["id"] == str(sample_service.id)
    assert json_resp["data"]["email_branding"] is None
    assert json_resp["data"]["prefix_sms"] is False
    assert json_resp["data"]["allowed_broadcast_provider"] is None
    assert json_resp["data"]["broadcast_channel"] is None

    assert set(json_resp["data"].keys()) == {
        "active",
        "allowed_broadcast_provider",
        "billing_contact_email_addresses",
        "billing_contact_names",
        "billing_reference",
        "broadcast_channel",
        "consent_to_research",
        "contact_link",
        "count_as_live",
        "created_by",
        "custom_email_sender_name",
        "email_branding",
        "email_message_limit",
        "email_sender_local_part",
        "go_live_at",
        "go_live_user",
        "has_active_go_live_request",
        "id",
        "inbound_api",
        "letter_branding",
        "letter_message_limit",
        "name",
        "notes",
        "organisation",
        "organisation_type",
        "permissions",
        "prefix_sms",
        "purchase_order_number",
        "rate_limit",
        "restricted",
        "service_callback_api",
        "sms_message_limit",
        "volume_email",
        "volume_letter",
        "volume_sms",
    }


@pytest.mark.parametrize("detailed", [True, False])
def test_get_service_by_id_returns_organisation_type(admin_request, sample_service, detailed):
    json_resp = admin_request.get("service.get_service_by_id", service_id=sample_service.id, detailed=detailed)
    assert json_resp["data"]["organisation_type"] is None


def test_get_service_list_has_default_permissions(admin_request, service_factory):
    service_factory.get("one")
    service_factory.get("one")
    service_factory.get("two")
    service_factory.get("three")

    json_resp = admin_request.get("service.get_services")
    assert len(json_resp["data"]) == 3
    assert all(
        set(json["permissions"])
        == {EMAIL_TYPE, SMS_TYPE, INTERNATIONAL_SMS_TYPE, LETTER_TYPE, UPLOAD_LETTERS, INTERNATIONAL_LETTERS}
        for json in json_resp["data"]
    )


def test_get_service_by_id_has_default_service_permissions(admin_request, sample_service):
    json_resp = admin_request.get("service.get_service_by_id", service_id=sample_service.id)

    assert set(json_resp["data"]["permissions"]) == {
        EMAIL_TYPE,
        SMS_TYPE,
        INTERNATIONAL_SMS_TYPE,
        LETTER_TYPE,
        UPLOAD_LETTERS,
        INTERNATIONAL_LETTERS,
    }


def test_get_service_by_id_should_404_if_no_service(admin_request, notify_db_session):
    json_resp = admin_request.get("service.get_service_by_id", service_id=uuid.uuid4(), _expected_status=404)

    assert json_resp["result"] == "error"
    assert json_resp["message"] == "No result found"


def test_get_service_by_id_and_user(client, sample_service, sample_user):
    sample_service.reply_to_email = "something@service.com"
    create_reply_to_email(service=sample_service, email_address="new@service.com")
    auth_header = create_admin_authorization_header()
    resp = client.get(f"/service/{sample_service.id}?user_id={sample_user.id}", headers=[auth_header])
    assert resp.status_code == 200
    json_resp = resp.json
    assert json_resp["data"]["name"] == sample_service.name
    assert json_resp["data"]["id"] == str(sample_service.id)


def test_get_service_by_id_should_404_if_no_service_for_user(notify_api, sample_user):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            service_id = str(uuid.uuid4())
            auth_header = create_admin_authorization_header()
            resp = client.get(f"/service/{service_id}?user_id={sample_user.id}", headers=[auth_header])
            assert resp.status_code == 404
            json_resp = resp.json
            assert json_resp["result"] == "error"
            assert json_resp["message"] == "No result found"


def test_get_service_by_id_returns_go_live_user_and_go_live_at(admin_request, sample_user):
    now = datetime.utcnow()
    service = create_service(user=sample_user, go_live_user=sample_user, go_live_at=now)
    json_resp = admin_request.get("service.get_service_by_id", service_id=service.id)
    assert json_resp["data"]["go_live_user"] == str(sample_user.id)
    assert json_resp["data"]["go_live_at"] == str(now)


@pytest.mark.parametrize(
    "platform_admin, expected_count_as_live",
    (
        (True, False),
        (False, True),
    ),
)
def test_create_service(
    admin_request,
    sample_user,
    platform_admin,
    expected_count_as_live,
):
    sample_user.platform_admin = platform_admin
    data = {
        "name": "created service",
        "user_id": str(sample_user.id),
        "email_message_limit": 1000,
        "sms_message_limit": 1000,
        "letter_message_limit": 1000,
        "restricted": False,
        "active": False,
        "created_by": str(sample_user.id),
    }

    json_resp = admin_request.post("service.create_service", _data=data, _expected_status=201)

    assert json_resp["data"]["id"]
    assert json_resp["data"]["name"] == "created service"
    assert json_resp["data"]["email_sender_local_part"] == "created.service"
    assert json_resp["data"]["letter_branding"] is None
    assert json_resp["data"]["count_as_live"] is expected_count_as_live

    service_db = Service.query.get(json_resp["data"]["id"])
    assert service_db.name == "created service"

    json_resp = admin_request.get(
        "service.get_service_by_id", service_id=json_resp["data"]["id"], user_id=sample_user.id
    )

    assert json_resp["data"]["name"] == "created service"

    service_sms_senders = ServiceSmsSender.query.filter_by(service_id=service_db.id).all()
    assert len(service_sms_senders) == 1
    assert service_sms_senders[0].sms_sender == current_app.config["FROM_NUMBER"]


@pytest.mark.parametrize(
    "domain, expected_org",
    (
        (None, False),
        ("", False),
        ("unknown.gov.uk", False),
        ("unknown-example.gov.uk", False),
        ("example.gov.uk", True),
        ("test.gov.uk", True),
        ("test.example.gov.uk", True),
    ),
)
def test_create_service_with_domain_sets_organisation(
    admin_request,
    sample_user,
    domain,
    expected_org,
):
    red_herring_org = create_organisation(name="Sub example")
    create_domain("specific.example.gov.uk", red_herring_org.id)
    create_domain("aaaaaaaa.example.gov.uk", red_herring_org.id)

    org = create_organisation()
    create_domain("example.gov.uk", org.id)
    create_domain("test.gov.uk", org.id)

    another_org = create_organisation(name="Another")
    create_domain("cabinet-office.gov.uk", another_org.id)
    create_domain("cabinetoffice.gov.uk", another_org.id)

    sample_user.email_address = f"test@{domain}"

    data = {
        "name": "created service",
        "user_id": str(sample_user.id),
        "email_message_limit": 1000,
        "sms_message_limit": 1000,
        "letter_message_limit": 1000,
        "restricted": False,
        "active": False,
        "created_by": str(sample_user.id),
        "service_domain": domain,
    }

    json_resp = admin_request.post("service.create_service", _data=data, _expected_status=201)

    if expected_org:
        assert json_resp["data"]["organisation"] == str(org.id)
    else:
        assert json_resp["data"]["organisation"] is None


def test_create_service_should_create_annual_billing_for_service(admin_request, sample_user):
    data = {
        "name": "created service",
        "user_id": str(sample_user.id),
        "email_message_limit": 1000,
        "sms_message_limit": 1000,
        "letter_message_limit": 1000,
        "restricted": False,
        "active": False,
        "created_by": str(sample_user.id),
    }
    assert len(AnnualBilling.query.all()) == 0
    admin_request.post("service.create_service", _data=data, _expected_status=201)

    annual_billing = AnnualBilling.query.all()
    assert len(annual_billing) == 1


def test_create_service_should_raise_exception_and_not_create_service_if_annual_billing_query_fails(
    admin_request, sample_user, mocker
):
    mocker.patch("app.service.rest.set_default_free_allowance_for_service", side_effect=SQLAlchemyError)
    data = {
        "name": "created service",
        "user_id": str(sample_user.id),
        "email_message_limit": 1000,
        "sms_message_limit": 1000,
        "letter_message_limit": 1000,
        "restricted": False,
        "active": False,
        "created_by": str(sample_user.id),
    }
    assert len(AnnualBilling.query.all()) == 0
    with pytest.raises(expected_exception=SQLAlchemyError):
        admin_request.post("service.create_service", _data=data)

    annual_billing = AnnualBilling.query.all()
    assert len(annual_billing) == 0
    assert len(Service.query.filter(Service.name == "created service").all()) == 0


def test_create_service_inherits_branding_from_organisation(
    admin_request,
    sample_user,
):
    org = create_organisation()
    email_branding = create_email_branding()
    org.email_branding = email_branding
    letter_branding = create_letter_branding()
    org.letter_branding = letter_branding
    create_domain("example.gov.uk", org.id)
    sample_user.email_address = "test@example.gov.uk"

    json_resp = admin_request.post(
        "service.create_service",
        _data={
            "name": "created service",
            "user_id": str(sample_user.id),
            "email_message_limit": 1000,
            "sms_message_limit": 1000,
            "letter_message_limit": 1000,
            "restricted": False,
            "active": False,
            "created_by": str(sample_user.id),
        },
        _expected_status=201,
    )

    assert json_resp["data"]["email_branding"] == str(email_branding.id)
    assert json_resp["data"]["letter_branding"] == str(letter_branding.id)


def test_should_not_create_service_with_missing_user_id_field(notify_api, fake_uuid):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            data = {
                "name": "created service",
                "email_message_limit": 1000,
                "sms_message_limit": 1000,
                "letter_message_limit": 1000,
                "restricted": False,
                "active": False,
                "created_by": str(fake_uuid),
            }
            auth_header = create_admin_authorization_header()
            headers = [("Content-Type", "application/json"), auth_header]
            resp = client.post("/service", data=json.dumps(data), headers=headers)
            json_resp = resp.json
            assert resp.status_code == 400
            assert json_resp["result"] == "error"
            assert "Missing data for required field." in json_resp["message"]["user_id"]


def test_should_error_if_created_by_missing(notify_api, sample_user):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            data = {
                "name": "created service",
                "email_message_limit": 1000,
                "sms_message_limit": 1000,
                "letter_message_limit": 1000,
                "restricted": False,
                "active": False,
                "user_id": str(sample_user.id),
            }
            auth_header = create_admin_authorization_header()
            headers = [("Content-Type", "application/json"), auth_header]
            resp = client.post("/service", data=json.dumps(data), headers=headers)
            json_resp = resp.json
            assert resp.status_code == 400
            assert json_resp["result"] == "error"
            assert "Missing data for required field." in json_resp["message"]["created_by"]


def test_should_not_create_service_with_missing_if_user_id_is_not_in_database(notify_api, notify_db_session, fake_uuid):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            data = {
                "user_id": fake_uuid,
                "name": "created service",
                "email_message_limit": 1000,
                "sms_message_limit": 1000,
                "letter_message_limit": 1000,
                "restricted": False,
                "active": False,
                "created_by": str(fake_uuid),
            }
            auth_header = create_admin_authorization_header()
            headers = [("Content-Type", "application/json"), auth_header]
            resp = client.post("/service", data=json.dumps(data), headers=headers)
            json_resp = resp.json
            assert resp.status_code == 404
            assert json_resp["result"] == "error"
            assert json_resp["message"] == "No result found"


def test_should_not_create_service_if_missing_data(notify_api, sample_user):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            data = {"user_id": str(sample_user.id)}
            auth_header = create_admin_authorization_header()
            headers = [("Content-Type", "application/json"), auth_header]
            resp = client.post("/service", data=json.dumps(data), headers=headers)
            json_resp = resp.json
            assert resp.status_code == 400
            assert json_resp["result"] == "error"
            assert "Missing data for required field." in json_resp["message"]["name"]
            assert "Missing data for required field." in json_resp["message"]["email_message_limit"]
            assert "Missing data for required field." in json_resp["message"]["sms_message_limit"]
            assert "Missing data for required field." in json_resp["message"]["letter_message_limit"]
            assert "Missing data for required field." in json_resp["message"]["restricted"]


def test_should_not_create_service_with_duplicate_name(notify_api, sample_user, sample_service):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            data = {
                "name": sample_service.name,
                "user_id": str(sample_service.users[0].id),
                "email_message_limit": 1000,
                "sms_message_limit": 1000,
                "letter_message_limit": 1000,
                "restricted": False,
                "active": False,
                "created_by": str(sample_user.id),
            }
            auth_header = create_admin_authorization_header()
            headers = [("Content-Type", "application/json"), auth_header]
            resp = client.post("/service", data=json.dumps(data), headers=headers)
            json_resp = resp.json
            assert json_resp["result"] == "error"
            assert f"Duplicate service name '{sample_service.name}'" in json_resp["message"]["name"]


def test_create_service_should_throw_duplicate_key_constraint_for_existing_normalised_service_name(
    notify_api, service_factory, sample_user
):
    first_service = service_factory.get("First service")
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            service_name = "First SERVICE"
            data = {
                "name": service_name,
                "user_id": str(first_service.users[0].id),
                "email_message_limit": 1000,
                "sms_message_limit": 1000,
                "letter_message_limit": 1000,
                "restricted": False,
                "active": False,
                "created_by": str(sample_user.id),
            }
            auth_header = create_admin_authorization_header()
            headers = [("Content-Type", "application/json"), auth_header]
            resp = client.post("/service", data=json.dumps(data), headers=headers)
            json_resp = resp.json
            assert json_resp["result"] == "error"
            assert f"Duplicate service name '{service_name}'" in json_resp["message"]["name"]


@pytest.mark.parametrize("has_active_go_live_request", (True, False))
def test_update_service(client, notify_db_session, sample_service, has_active_go_live_request):
    brand = EmailBranding(colour="#000000", logo="justice-league.png", name="Justice League", alt_text="Justice League")
    notify_db_session.add(brand)
    notify_db_session.commit()

    assert sample_service.email_branding is None

    data = {
        "name": "updated service name",
        "created_by": str(sample_service.created_by.id),
        "email_branding": str(brand.id),
        "organisation_type": "school_or_college",
        "has_active_go_live_request": has_active_go_live_request,
    }

    auth_header = create_admin_authorization_header()

    resp = client.post(
        f"/service/{sample_service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    result = resp.json
    assert resp.status_code == 200
    assert result["data"]["name"] == "updated service name"
    assert result["data"]["email_sender_local_part"] == "updated.service.name"
    assert result["data"]["email_branding"] == str(brand.id)
    assert result["data"]["organisation_type"] == "school_or_college"
    assert result["data"]["has_active_go_live_request"] == has_active_go_live_request


def test_cant_update_service_org_type_to_random_value(client, sample_service):
    data = {
        "name": "updated service name",
        "created_by": str(sample_service.created_by.id),
        "organisation_type": "foo",
    }

    auth_header = create_admin_authorization_header()

    resp = client.post(
        f"/service/{sample_service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    assert resp.status_code == 500


def test_update_service_letter_branding(client, notify_db_session, sample_service):
    letter_branding = create_letter_branding(name="test brand", filename="test-brand")
    data = {"letter_branding": str(letter_branding.id)}

    auth_header = create_admin_authorization_header()

    resp = client.post(
        f"/service/{sample_service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    result = resp.json
    assert resp.status_code == 200
    assert result["data"]["letter_branding"] == str(letter_branding.id)


def test_update_service_remove_letter_branding(client, notify_db_session, sample_service):
    letter_branding = create_letter_branding(name="test brand", filename="test-brand")
    data = {"letter_branding": str(letter_branding.id)}

    auth_header = create_admin_authorization_header()

    client.post(
        f"/service/{sample_service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    data = {"letter_branding": None}
    resp = client.post(
        f"/service/{sample_service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    result = resp.json
    assert resp.status_code == 200
    assert result["data"]["letter_branding"] is None


def test_update_service_remove_email_branding(admin_request, notify_db_session, sample_service):
    brand = EmailBranding(colour="#000000", logo="justice-league.png", name="Justice League", alt_text="Justice League")
    sample_service.email_branding = brand
    notify_db_session.commit()

    resp = admin_request.post("service.update_service", service_id=sample_service.id, _data={"email_branding": None})
    assert resp["data"]["email_branding"] is None


def test_update_service_change_email_branding(admin_request, notify_db_session, sample_service):
    brand1 = EmailBranding(colour="#000000", logo="justice-league.png", name="Justice League", text="Foo")
    brand2 = EmailBranding(colour="#111111", logo="avengers.png", name="Avengers", text="Foo")
    notify_db_session.add_all([brand1, brand2])
    sample_service.email_branding = brand1
    notify_db_session.commit()

    resp = admin_request.post(
        "service.update_service", service_id=sample_service.id, _data={"email_branding": str(brand2.id)}
    )
    assert resp["data"]["email_branding"] == str(brand2.id)


def test_update_service_flags(client, sample_service):
    auth_header = create_admin_authorization_header()
    resp = client.get(f"/service/{sample_service.id}", headers=[auth_header])
    json_resp = resp.json
    assert resp.status_code == 200
    assert json_resp["data"]["name"] == sample_service.name

    data = {"permissions": [LETTER_TYPE, INTERNATIONAL_SMS_TYPE]}

    auth_header = create_admin_authorization_header()

    resp = client.post(
        f"/service/{sample_service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    result = resp.json
    assert resp.status_code == 200
    assert set(result["data"]["permissions"]) == {LETTER_TYPE, INTERNATIONAL_SMS_TYPE}


@pytest.mark.parametrize(
    "field",
    (
        "volume_email",
        "volume_sms",
        "volume_letter",
    ),
)
@pytest.mark.parametrize(
    "value, expected_status, expected_persisted",
    (
        (1234, 200, 1234),
        (None, 200, None),
        ("Aa", 400, None),
    ),
)
def test_update_service_sets_volumes(
    admin_request,
    sample_service,
    field,
    value,
    expected_status,
    expected_persisted,
):
    admin_request.post(
        "service.update_service",
        service_id=sample_service.id,
        _data={
            field: value,
        },
        _expected_status=expected_status,
    )
    assert getattr(sample_service, field) == expected_persisted


@pytest.mark.parametrize(
    "value, expected_status, expected_persisted",
    (
        (True, 200, True),
        (False, 200, False),
        ("unknown", 400, None),
    ),
)
def test_update_service_sets_research_consent(
    admin_request,
    sample_service,
    value,
    expected_status,
    expected_persisted,
):
    assert sample_service.consent_to_research is None
    admin_request.post(
        "service.update_service",
        service_id=sample_service.id,
        _data={
            "consent_to_research": value,
        },
        _expected_status=expected_status,
    )
    assert sample_service.consent_to_research is expected_persisted


@pytest.fixture(scope="function")
def service_with_no_permissions(notify_db_session):
    return create_service(service_permissions=[])


def test_update_service_flags_with_service_without_default_service_permissions(client, service_with_no_permissions):
    auth_header = create_admin_authorization_header()
    data = {
        "permissions": [LETTER_TYPE, INTERNATIONAL_SMS_TYPE],
    }

    resp = client.post(
        f"/service/{service_with_no_permissions.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    result = resp.json

    assert resp.status_code == 200
    assert set(result["data"]["permissions"]) == {LETTER_TYPE, INTERNATIONAL_SMS_TYPE}


def test_update_service_flags_will_remove_service_permissions(client, notify_db_session):
    auth_header = create_admin_authorization_header()

    service = create_service(service_permissions=[SMS_TYPE, EMAIL_TYPE, INTERNATIONAL_SMS_TYPE])

    assert INTERNATIONAL_SMS_TYPE in [p.permission for p in service.permissions]

    data = {"permissions": [SMS_TYPE, EMAIL_TYPE]}

    resp = client.post(
        f"/service/{service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    result = resp.json

    assert resp.status_code == 200
    assert INTERNATIONAL_SMS_TYPE not in result["data"]["permissions"]

    permissions = ServicePermission.query.filter_by(service_id=service.id).all()
    assert {p.permission for p in permissions} == {SMS_TYPE, EMAIL_TYPE}


def test_update_permissions_will_override_permission_flags(client, service_with_no_permissions):
    auth_header = create_admin_authorization_header()

    data = {"permissions": [LETTER_TYPE, INTERNATIONAL_SMS_TYPE]}

    resp = client.post(
        f"/service/{service_with_no_permissions.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    result = resp.json

    assert resp.status_code == 200
    assert set(result["data"]["permissions"]) == {LETTER_TYPE, INTERNATIONAL_SMS_TYPE}


def test_update_service_permissions_will_add_service_permissions(client, sample_service):
    auth_header = create_admin_authorization_header()

    data = {"permissions": [EMAIL_TYPE, SMS_TYPE, LETTER_TYPE]}

    resp = client.post(
        f"/service/{sample_service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    result = resp.json

    assert resp.status_code == 200
    assert set(result["data"]["permissions"]) == {SMS_TYPE, EMAIL_TYPE, LETTER_TYPE}


@pytest.mark.parametrize(
    "permission_to_add",
    [
        (EMAIL_TYPE),
        (SMS_TYPE),
        (INTERNATIONAL_SMS_TYPE),
        (LETTER_TYPE),
        (INBOUND_SMS_TYPE),
        (EMAIL_AUTH_TYPE),
        (BROADCAST_TYPE),  # TODO: remove this ability to set broadcast permission this way
    ],
)
def test_add_service_permission_will_add_permission(client, service_with_no_permissions, permission_to_add):
    auth_header = create_admin_authorization_header()

    data = {"permissions": [permission_to_add]}

    resp = client.post(
        f"/service/{service_with_no_permissions.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    permissions = ServicePermission.query.filter_by(service_id=service_with_no_permissions.id).all()

    assert resp.status_code == 200
    assert [p.permission for p in permissions] == [permission_to_add]


def test_update_permissions_with_an_invalid_permission_will_raise_error(client, sample_service):
    auth_header = create_admin_authorization_header()
    invalid_permission = "invalid_permission"

    data = {"permissions": [EMAIL_TYPE, SMS_TYPE, invalid_permission]}

    resp = client.post(
        f"/service/{sample_service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    result = resp.json

    assert resp.status_code == 400
    assert result["result"] == "error"
    assert f"Invalid Service Permission: '{invalid_permission}'" in result["message"]["permissions"]


def test_update_permissions_with_duplicate_permissions_will_raise_error(client, sample_service):
    auth_header = create_admin_authorization_header()

    data = {"permissions": [EMAIL_TYPE, SMS_TYPE, LETTER_TYPE, LETTER_TYPE]}

    resp = client.post(
        f"/service/{sample_service.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    result = resp.json

    assert resp.status_code == 400
    assert result["result"] == "error"
    assert f"Duplicate Service Permission: ['{LETTER_TYPE}']" in result["message"]["permissions"]


def test_should_not_update_service_with_duplicate_name(notify_api, notify_db_session, sample_user, sample_service):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            service_name = "another name"
            service = create_service(service_name=service_name, user=sample_user)
            data = {"name": service_name, "created_by": str(service.created_by.id)}

            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{sample_service.id}",
                data=json.dumps(data),
                headers=[("Content-Type", "application/json"), auth_header],
            )
            assert resp.status_code == 400
            json_resp = resp.json
            assert json_resp["result"] == "error"
            assert f"Duplicate service name '{service_name}'" in json_resp["message"]["name"]


def test_should_not_update_service_with_duplicate_normalised_service_name(
    notify_api, notify_db_session, sample_user, sample_service
):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            # create a second service
            create_service(service_name="service name", user=sample_user)

            data = {"name": "SERVICE (name)", "created_by": str(sample_user.id)}

            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{sample_service.id}",
                data=json.dumps(data),
                headers=[("Content-Type", "application/json"), auth_header],
            )
            assert resp.status_code == 400
            json_resp = resp.json
            assert json_resp["result"] == "error"
            assert "Duplicate service name 'SERVICE (name)'" in json_resp["message"]["name"]


def test_update_service_should_404_if_id_is_invalid(notify_api):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            data = {"name": "updated service name"}

            missing_service_id = uuid.uuid4()

            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{missing_service_id}",
                data=json.dumps(data),
                headers=[("Content-Type", "application/json"), auth_header],
            )
            assert resp.status_code == 404


def test_get_users_by_service(notify_api, sample_service):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            user_on_service = sample_service.users[0]
            auth_header = create_admin_authorization_header()

            resp = client.get(
                f"/service/{sample_service.id}/users",
                headers=[("Content-Type", "application/json"), auth_header],
            )

            assert resp.status_code == 200
            result = resp.json
            assert len(result["data"]) == 1
            assert result["data"][0]["name"] == user_on_service.name
            assert result["data"][0]["email_address"] == user_on_service.email_address
            assert result["data"][0]["mobile_number"] == user_on_service.mobile_number


def test_get_users_for_service_returns_empty_list_if_no_users_associated_with_service(notify_api, sample_service):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            dao_remove_user_from_service(sample_service, sample_service.users[0])
            auth_header = create_admin_authorization_header()

            response = client.get(
                f"/service/{sample_service.id}/users",
                headers=[("Content-Type", "application/json"), auth_header],
            )
            result = json.loads(response.get_data(as_text=True))
            assert response.status_code == 200
            assert result["data"] == []


def test_get_users_for_service_returns_404_when_service_does_not_exist(notify_api, notify_db_session):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            service_id = uuid.uuid4()
            auth_header = create_admin_authorization_header()

            response = client.get(
                f"/service/{service_id}/users", headers=[("Content-Type", "application/json"), auth_header]
            )
            assert response.status_code == 404
            result = json.loads(response.get_data(as_text=True))
            assert result["result"] == "error"
            assert result["message"] == "No result found"


def test_default_permissions_are_added_for_user_service(notify_api, notify_db_session, sample_service, sample_user):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            data = {
                "name": "created service",
                "user_id": str(sample_user.id),
                "email_message_limit": 1000,
                "sms_message_limit": 1000,
                "letter_message_limit": 1000,
                "restricted": False,
                "active": False,
                "created_by": str(sample_user.id),
            }
            auth_header = create_admin_authorization_header()
            headers = [("Content-Type", "application/json"), auth_header]
            resp = client.post("/service", data=json.dumps(data), headers=headers)
            json_resp = resp.json
            assert resp.status_code == 201
            assert json_resp["data"]["id"]
            assert json_resp["data"]["name"] == "created service"

            auth_header_fetch = create_admin_authorization_header()

            resp = client.get(
                "/service/{}?user_id={}".format(json_resp["data"]["id"], sample_user.id), headers=[auth_header_fetch]
            )
            assert resp.status_code == 200
            header = create_admin_authorization_header()
            response = client.get(url_for("user.get_user", user_id=sample_user.id), headers=[header])
            assert response.status_code == 200
            json_resp = json.loads(response.get_data(as_text=True))
            service_permissions = json_resp["data"]["permissions"][str(sample_service.id)]
            from app.dao.permissions_dao import default_service_permissions

            assert sorted(default_service_permissions) == sorted(service_permissions)


def test_add_existing_user_to_another_service_with_all_permissions(
    notify_api, notify_db_session, sample_service, sample_user
):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            # check which users part of service
            user_already_in_service = sample_service.users[0]
            auth_header = create_admin_authorization_header()

            resp = client.get(
                f"/service/{sample_service.id}/users",
                headers=[("Content-Type", "application/json"), auth_header],
            )

            assert resp.status_code == 200
            result = resp.json
            assert len(result["data"]) == 1
            assert result["data"][0]["email_address"] == user_already_in_service.email_address

            # add new user to service
            user_to_add = User(
                name="Invited User",
                email_address="invited@digital.cabinet-office.gov.uk",
                password="password",
                mobile_number="+4477123456",
            )
            # they must exist in db first
            save_model_user(user_to_add, validated_email_access=True)

            data = {
                "permissions": [
                    {"permission": "send_emails"},
                    {"permission": "send_letters"},
                    {"permission": "send_texts"},
                    {"permission": "manage_users"},
                    {"permission": "manage_settings"},
                    {"permission": "manage_api_keys"},
                    {"permission": "manage_templates"},
                    {"permission": "view_activity"},
                ],
                "folder_permissions": [],
            }

            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{sample_service.id}/users/{user_to_add.id}",
                headers=[("Content-Type", "application/json"), auth_header],
                data=json.dumps(data),
            )

            assert resp.status_code == 201

            # check new user added to service
            auth_header = create_admin_authorization_header()

            resp = client.get(
                f"/service/{sample_service.id}",
                headers=[("Content-Type", "application/json"), auth_header],
            )
            assert resp.status_code == 200
            json_resp = resp.json

            # check user has all permissions
            auth_header = create_admin_authorization_header()
            resp = client.get(
                url_for("user.get_user", user_id=user_to_add.id),
                headers=[("Content-Type", "application/json"), auth_header],
            )

            assert resp.status_code == 200
            json_resp = resp.json
            permissions = json_resp["data"]["permissions"][str(sample_service.id)]
            expected_permissions = [
                "send_texts",
                "send_emails",
                "send_letters",
                "manage_users",
                "manage_settings",
                "manage_templates",
                "manage_api_keys",
                "view_activity",
            ]
            assert sorted(expected_permissions) == sorted(permissions)


def test_add_existing_user_to_another_service_with_send_permissions(
    notify_api, notify_db_session, sample_service, sample_user
):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            # they must exist in db first
            user_to_add = User(
                name="Invited User",
                email_address="invited@digital.cabinet-office.gov.uk",
                password="password",
                mobile_number="+4477123456",
            )
            save_model_user(user_to_add, validated_email_access=True)

            data = {
                "permissions": [
                    {"permission": "send_emails"},
                    {"permission": "send_letters"},
                    {"permission": "send_texts"},
                ],
                "folder_permissions": [],
            }

            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{sample_service.id}/users/{user_to_add.id}",
                headers=[("Content-Type", "application/json"), auth_header],
                data=json.dumps(data),
            )

            assert resp.status_code == 201

            # check user has send permissions
            auth_header = create_admin_authorization_header()
            resp = client.get(
                url_for("user.get_user", user_id=user_to_add.id),
                headers=[("Content-Type", "application/json"), auth_header],
            )

            assert resp.status_code == 200
            json_resp = resp.json

            permissions = json_resp["data"]["permissions"][str(sample_service.id)]
            expected_permissions = ["send_texts", "send_emails", "send_letters"]
            assert sorted(expected_permissions) == sorted(permissions)


def test_add_existing_user_to_another_service_with_manage_permissions(
    notify_api, notify_db_session, sample_service, sample_user
):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            # they must exist in db first
            user_to_add = User(
                name="Invited User",
                email_address="invited@digital.cabinet-office.gov.uk",
                password="password",
                mobile_number="+4477123456",
            )
            save_model_user(user_to_add, validated_email_access=True)

            data = {
                "permissions": [
                    {"permission": "manage_users"},
                    {"permission": "manage_settings"},
                    {"permission": "manage_templates"},
                ]
            }

            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{sample_service.id}/users/{user_to_add.id}",
                headers=[("Content-Type", "application/json"), auth_header],
                data=json.dumps(data),
            )

            assert resp.status_code == 201

            # check user has send permissions
            auth_header = create_admin_authorization_header()
            resp = client.get(
                url_for("user.get_user", user_id=user_to_add.id),
                headers=[("Content-Type", "application/json"), auth_header],
            )

            assert resp.status_code == 200
            json_resp = resp.json

            permissions = json_resp["data"]["permissions"][str(sample_service.id)]
            expected_permissions = ["manage_users", "manage_settings", "manage_templates"]
            assert sorted(expected_permissions) == sorted(permissions)


def test_add_existing_user_to_another_service_with_folder_permissions(
    notify_api, notify_db_session, sample_service, sample_user
):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            # they must exist in db first
            user_to_add = User(
                name="Invited User",
                email_address="invited@digital.cabinet-office.gov.uk",
                password="password",
                mobile_number="+4477123456",
            )
            save_model_user(user_to_add, validated_email_access=True)

            folder_1 = create_template_folder(sample_service)
            folder_2 = create_template_folder(sample_service)

            data = {
                "permissions": [{"permission": "manage_api_keys"}],
                "folder_permissions": [str(folder_1.id), str(folder_2.id)],
            }

            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{sample_service.id}/users/{user_to_add.id}",
                headers=[("Content-Type", "application/json"), auth_header],
                data=json.dumps(data),
            )

            assert resp.status_code == 201

            new_user = dao_get_service_user(user_id=user_to_add.id, service_id=sample_service.id)

            assert len(new_user.folders) == 2
            assert folder_1 in new_user.folders
            assert folder_2 in new_user.folders


def test_add_existing_user_to_another_service_with_manage_api_keys(
    notify_api, notify_db_session, sample_service, sample_user
):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            # they must exist in db first
            user_to_add = User(
                name="Invited User",
                email_address="invited@digital.cabinet-office.gov.uk",
                password="password",
                mobile_number="+4477123456",
            )
            save_model_user(user_to_add, validated_email_access=True)

            data = {"permissions": [{"permission": "manage_api_keys"}]}

            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{sample_service.id}/users/{user_to_add.id}",
                headers=[("Content-Type", "application/json"), auth_header],
                data=json.dumps(data),
            )

            assert resp.status_code == 201

            # check user has send permissions
            auth_header = create_admin_authorization_header()
            resp = client.get(
                url_for("user.get_user", user_id=user_to_add.id),
                headers=[("Content-Type", "application/json"), auth_header],
            )

            assert resp.status_code == 200
            json_resp = resp.json

            permissions = json_resp["data"]["permissions"][str(sample_service.id)]
            expected_permissions = ["manage_api_keys"]
            assert sorted(expected_permissions) == sorted(permissions)


def test_add_existing_user_to_non_existing_service_returns404(notify_api, notify_db_session, sample_user):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            user_to_add = User(
                name="Invited User",
                email_address="invited@digital.cabinet-office.gov.uk",
                password="password",
                mobile_number="+4477123456",
            )
            save_model_user(user_to_add, validated_email_access=True)

            incorrect_id = uuid.uuid4()

            data = {"permissions": ["send_messages", "manage_service", "manage_api_keys"]}
            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{incorrect_id}/users/{user_to_add.id}",
                headers=[("Content-Type", "application/json"), auth_header],
                data=json.dumps(data),
            )

            result = resp.json
            expected_message = "No result found"

            assert resp.status_code == 404
            assert result["result"] == "error"
            assert result["message"] == expected_message


def test_add_existing_user_of_service_to_service_returns400(notify_api, notify_db_session, sample_service):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            existing_user_id = sample_service.users[0].id

            data = {"permissions": ["send_messages", "manage_service", "manage_api_keys"]}
            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{sample_service.id}/users/{existing_user_id}",
                headers=[("Content-Type", "application/json"), auth_header],
                data=json.dumps(data),
            )

            result = resp.json
            expected_message = f"User id: {existing_user_id} already part of service id: {sample_service.id}"

            assert resp.status_code == 400
            assert result["result"] == "error"
            assert result["message"] == expected_message


def test_add_unknown_user_to_service_returns404(notify_api, notify_db_session, sample_service):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            incorrect_id = 9876

            data = {"permissions": ["send_messages", "manage_service", "manage_api_keys"]}
            auth_header = create_admin_authorization_header()

            resp = client.post(
                f"/service/{sample_service.id}/users/{incorrect_id}",
                headers=[("Content-Type", "application/json"), auth_header],
                data=json.dumps(data),
            )

            result = resp.json
            expected_message = "No result found"

            assert resp.status_code == 404
            assert result["result"] == "error"
            assert result["message"] == expected_message


def test_remove_user_from_service(client, sample_user_service_permission):
    second_user = create_user(email="new@digital.cabinet-office.gov.uk")
    service = sample_user_service_permission.service

    # Simulates successfully adding a user to the service
    dao_add_user_to_service(
        service,
        second_user,
        permissions=[Permission(service_id=service.id, user_id=second_user.id, permission="manage_settings")],
    )

    endpoint = url_for("service.remove_user_from_service", service_id=str(service.id), user_id=str(second_user.id))
    auth_header = create_admin_authorization_header()
    resp = client.delete(endpoint, headers=[("Content-Type", "application/json"), auth_header])
    assert resp.status_code == 204


def test_remove_non_existant_user_from_service(client, sample_user_service_permission):
    second_user = create_user(email="new@digital.cabinet-office.gov.uk")
    endpoint = url_for(
        "service.remove_user_from_service",
        service_id=str(sample_user_service_permission.service.id),
        user_id=str(second_user.id),
    )
    auth_header = create_admin_authorization_header()
    resp = client.delete(endpoint, headers=[("Content-Type", "application/json"), auth_header])
    assert resp.status_code == 404


def test_cannot_remove_only_user_from_service(notify_api, notify_db_session, sample_user_service_permission):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            endpoint = url_for(
                "service.remove_user_from_service",
                service_id=str(sample_user_service_permission.service.id),
                user_id=str(sample_user_service_permission.user.id),
            )
            auth_header = create_admin_authorization_header()
            resp = client.delete(endpoint, headers=[("Content-Type", "application/json"), auth_header])
            assert resp.status_code == 400
            result = resp.json
            assert result["message"] == "You cannot remove the only user for a service"


# This test is just here verify get_service_and_api_key_history that is a temp solution
# until proper ui is sorted out on admin app
def test_get_service_and_api_key_history(notify_api, sample_service, sample_api_key):
    with notify_api.test_request_context():
        with notify_api.test_client() as client:
            auth_header = create_admin_authorization_header()
            response = client.get(path=f"/service/{sample_service.id}/history", headers=[auth_header])
            assert response.status_code == 200

            json_resp = json.loads(response.get_data(as_text=True))
            assert json_resp["data"]["service_history"][0]["id"] == str(sample_service.id)
            assert json_resp["data"]["api_key_history"][0]["id"] == str(sample_api_key.id)


def test_get_all_notifications_for_service_in_order(client, notify_db_session):
    service_1 = create_service(service_name="1")
    service_2 = create_service(service_name="2")

    service_1_template = create_template(service_1)
    service_2_template = create_template(service_2)

    # create notification for service_2
    create_notification(service_2_template)

    notification_1 = create_notification(service_1_template)
    notification_2 = create_notification(service_1_template)
    notification_3 = create_notification(service_1_template)

    auth_header = create_admin_authorization_header()

    response = client.get(path=f"/service/{service_1.id}/notifications", headers=[auth_header])

    resp = json.loads(response.get_data(as_text=True))
    assert len(resp["notifications"]) == 3
    assert resp["notifications"][0]["to"] == notification_3.to
    assert resp["notifications"][1]["to"] == notification_2.to
    assert resp["notifications"][2]["to"] == notification_1.to
    assert response.status_code == 200


def test_get_all_notifications_for_service_in_order_with_post_request(client, notify_db_session):
    service_1 = create_service(service_name="1")
    service_2 = create_service(service_name="2")

    service_1_template = create_template(service_1)
    service_2_template = create_template(service_2)

    # create notification for service_2
    create_notification(service_2_template)

    notification_1 = create_notification(service_1_template)
    notification_2 = create_notification(service_1_template)
    notification_3 = create_notification(service_1_template)

    response = client.post(
        path=f"/service/{service_1.id}/notifications",
        data=json.dumps({}),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )

    resp = json.loads(response.get_data(as_text=True))
    assert len(resp["notifications"]) == 3
    assert resp["notifications"][0]["to"] == notification_3.to
    assert resp["notifications"][1]["to"] == notification_2.to
    assert resp["notifications"][2]["to"] == notification_1.to
    assert response.status_code == 200


def test_get_all_notifications_for_service_filters_notifications_when_using_post_request(client, notify_db_session):
    service_1 = create_service(service_name="1")
    service_2 = create_service(service_name="2")

    service_1_sms_template = create_template(service_1)
    service_1_email_template = create_template(service_1, template_type=EMAIL_TYPE)
    service_2_sms_template = create_template(service_2)

    returned_notification = create_notification(service_1_sms_template, normalised_to="447700900855")

    create_notification(service_1_sms_template, to_field="+447700900000", normalised_to="447700900000")
    create_notification(service_1_sms_template, status="delivered", normalised_to="447700900855")
    create_notification(service_1_email_template, normalised_to="447700900855")
    # create notification for service_2
    create_notification(service_2_sms_template)

    auth_header = create_admin_authorization_header()
    data = {"page": 1, "template_type": ["sms"], "status": ["created", "sending"], "to": "0855"}

    response = client.post(
        path=f"/service/{service_1.id}/notifications",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    resp = json.loads(response.get_data(as_text=True))
    assert len(resp["notifications"]) == 1
    assert resp["notifications"][0]["to"] == returned_notification.to
    assert resp["notifications"][0]["status"] == returned_notification.status
    assert response.status_code == 200


def test_get_all_notifications_for_service_for_csv(client, sample_template):
    notification = create_notification(template=sample_template)
    auth_header = create_admin_authorization_header()

    response = client.get(path=f"/service/{sample_template.service_id}/notifications/csv", headers=[auth_header])

    resp = json.loads(response.get_data(as_text=True))
    assert response.status_code == 200
    assert len(resp["notifications"]) == 1
    assert resp["notifications"][0]["recipient"] == notification.to
    assert not resp["notifications"][0]["row_number"]
    assert resp["notifications"][0]["template_name"] == sample_template.name
    assert resp["notifications"][0]["template_type"] == notification.notification_type
    assert resp["notifications"][0]["status"] == "Sending"


def test_get_notification_for_service_without_uuid(client, notify_db_session):
    service_1 = create_service(service_name="1")
    response = client.get(
        path="/service/{}/notifications/{}".format(service_1.id, "foo"), headers=[create_admin_authorization_header()]
    )
    assert response.status_code == 404


def test_get_notification_for_service(client, notify_db_session):
    service_1 = create_service(service_name="1")
    service_2 = create_service(service_name="2")

    service_1_template = create_template(service_1)
    service_2_template = create_template(service_2)

    service_1_notifications = [
        create_notification(service_1_template),
        create_notification(service_1_template),
        create_notification(service_1_template),
    ]

    create_notification(service_2_template)

    for notification in service_1_notifications:
        response = client.get(
            path=f"/service/{service_1.id}/notifications/{notification.id}",
            headers=[create_admin_authorization_header()],
        )
        resp = json.loads(response.get_data(as_text=True))
        assert str(resp["id"]) == str(notification.id)
        assert response.status_code == 200

        service_2_response = client.get(
            path=f"/service/{service_2.id}/notifications/{notification.id}",
            headers=[create_admin_authorization_header()],
        )
        assert service_2_response.status_code == 404
        service_2_response = json.loads(service_2_response.get_data(as_text=True))
        assert service_2_response == {"message": "No result found", "result": "error"}


def test_get_notification_for_service_includes_created_by(admin_request, sample_notification):
    user = sample_notification.created_by = sample_notification.service.created_by

    resp = admin_request.get(
        "service.get_notification_for_service",
        service_id=sample_notification.service_id,
        notification_id=sample_notification.id,
    )

    assert resp["id"] == str(sample_notification.id)
    assert resp["created_by"] == {"id": str(user.id), "name": user.name, "email_address": user.email_address}


def test_get_notification_for_service_returns_old_template_version(admin_request, sample_template):
    sample_notification = create_notification(sample_template)
    sample_notification.reference = "modified-inplace"
    sample_template.version = 2
    sample_template.content = "New template content"

    resp = admin_request.get(
        "service.get_notification_for_service",
        service_id=sample_notification.service_id,
        notification_id=sample_notification.id,
    )

    assert resp["reference"] == "modified-inplace"
    assert resp["template"]["version"] == 1
    assert resp["template"]["content"] == sample_notification.template.content
    assert resp["template"]["content"] != sample_template.content


@pytest.mark.parametrize("has_unsubscribe_link", (True, False))
def test_get_notification_for_service_returns_unsubscribe_link_of_template(
    admin_request,
    sample_service,
    has_unsubscribe_link,
):
    template = create_template(
        service=sample_service,
        template_type="email",
        has_unsubscribe_link=has_unsubscribe_link,
    )
    sample_notification = create_notification(template)
    resp = admin_request.get(
        "service.get_notification_for_service",
        service_id=sample_notification.service_id,
        notification_id=sample_notification.id,
    )
    assert resp["template"]["has_unsubscribe_link"] == has_unsubscribe_link


@pytest.mark.parametrize("include_from_test_key, expected_count_of_notifications", [(False, 2), (True, 3)])
def test_get_all_notifications_for_service_including_ones_made_by_jobs(
    client,
    sample_service,
    include_from_test_key,
    expected_count_of_notifications,
    sample_notification,
    sample_notification_with_job,
    sample_template,
):
    # notification from_test_api_key
    create_notification(sample_template, key_type=KEY_TYPE_TEST)

    auth_header = create_admin_authorization_header()

    response = client.get(
        path=f"/service/{sample_service.id}/notifications?include_from_test_key={include_from_test_key}",
        headers=[auth_header],
    )

    resp = json.loads(response.get_data(as_text=True))
    assert len(resp["notifications"]) == expected_count_of_notifications
    assert resp["notifications"][0]["to"] == sample_notification_with_job.to
    assert resp["notifications"][1]["to"] == sample_notification.to
    assert response.status_code == 200


def test_get_only_api_created_notifications_for_service(
    admin_request,
    sample_job,
    sample_template,
    sample_user,
):
    # notification sent as a job
    create_notification(sample_template, job=sample_job)
    # notification sent as a one-off
    create_notification(sample_template, one_off=True, created_by_id=sample_user.id)
    # notification sent via API
    without_job = create_notification(sample_template)

    resp = admin_request.get(
        "service.get_all_notifications_for_service",
        service_id=sample_template.service_id,
        include_jobs=False,
        include_one_off=False,
    )
    assert len(resp["notifications"]) == 1
    assert resp["notifications"][0]["id"] == str(without_job.id)


def test_get_notifications_for_service_without_page_count(
    admin_request,
    sample_job,
    sample_template,
    sample_user,
):
    create_notification(sample_template)
    without_job = create_notification(sample_template)

    resp = admin_request.get(
        "service.get_all_notifications_for_service",
        service_id=sample_template.service_id,
        page_size=1,
        include_jobs=False,
        include_one_off=False,
        count_pages=False,
    )
    assert len(resp["notifications"]) == 1
    assert resp["notifications"][0]["id"] == str(without_job.id)
    assert "prev" not in resp["links"]
    assert "next" not in resp["links"]


def test_get_notifications_for_service_pagination_links(
    admin_request,
    sample_job,
    sample_template,
    sample_user,
):
    for _ in range(11):
        create_notification(sample_template, to_field="+447700900855", normalised_to="447700900855")

    page_size = 5

    page_1_response = admin_request.get(
        "service.get_all_notifications_for_service", service_id=sample_template.service_id, page_size=page_size
    )

    assert "prev" not in page_1_response["links"]
    assert page_1_response["links"]["next"]

    page_2_response = admin_request.get(
        "service.get_all_notifications_for_service", service_id=sample_template.service_id, page=2, page_size=page_size
    )

    assert page_2_response["links"]["prev"]
    assert page_2_response["links"]["next"]

    page_3_response = admin_request.get(
        "service.get_all_notifications_for_service", service_id=sample_template.service_id, page=3, page_size=page_size
    )

    assert page_3_response["links"]["prev"]
    assert "next" not in page_3_response["links"]


def test_get_notifications_for_service_for_csv_multipage(
    admin_request,
    sample_template,
):
    oldest_notification = create_notification(sample_template, created_at=datetime.now(UTC) - timedelta(2))
    end_of_page_1_notification = create_notification(sample_template, created_at=datetime.now(UTC) - timedelta(1))
    create_notification(sample_template)

    page_size = 2

    first_response = admin_request.get(
        "service.get_all_notifications_for_service_for_csv",
        service_id=sample_template.service_id,
        page_size=page_size,
    )

    assert len(first_response["notifications"]) == 2
    assert first_response["notifications"][1]["id"] == str(end_of_page_1_notification.id)

    second_response = admin_request.get(
        "service.get_all_notifications_for_service_for_csv",
        service_id=sample_template.service_id,
        page_size=page_size,
        older_than=end_of_page_1_notification.id,
    )
    assert len(second_response["notifications"]) == 1
    assert second_response["notifications"][0]["id"] == str(oldest_notification.id)


@pytest.mark.parametrize(
    "should_prefix",
    [
        True,
        False,
    ],
)
def test_prefixing_messages_based_on_prefix_sms(
    client,
    notify_db_session,
    should_prefix,
):
    service = create_service(prefix_sms=should_prefix)

    result = client.get(
        url_for("service.get_service_by_id", service_id=service.id),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    service = json.loads(result.get_data(as_text=True))["data"]
    assert service["prefix_sms"] == should_prefix


@pytest.mark.parametrize(
    "posted_value, stored_value, returned_value",
    [
        (True, True, True),
        (False, False, False),
    ],
)
def test_set_sms_prefixing_for_service(
    admin_request,
    client,
    sample_service,
    posted_value,
    stored_value,
    returned_value,
):
    result = admin_request.post(
        "service.update_service",
        service_id=sample_service.id,
        _data={"prefix_sms": posted_value},
    )
    assert result["data"]["prefix_sms"] == stored_value


def test_set_sms_prefixing_for_service_cant_be_none(
    admin_request,
    sample_service,
):
    resp = admin_request.post(
        "service.update_service",
        service_id=sample_service.id,
        _data={"prefix_sms": None},
        _expected_status=400,
    )
    assert resp["message"] == {"prefix_sms": ["Field may not be null."]}


@pytest.mark.parametrize(
    "today_only,stats",
    [("False", {"requested": 2, "delivered": 1, "failed": 0}), ("True", {"requested": 1, "delivered": 0, "failed": 0})],
    ids=["seven_days", "today"],
)
def test_get_detailed_service(sample_template, client, sample_service, today_only, stats):
    create_ft_notification_status(date(2000, 1, 1), "sms", sample_service, count=1)
    with freeze_time("2000-01-02T12:00:00"):
        create_notification(template=sample_template, status="created")
        resp = client.get(
            f"/service/{sample_service.id}?detailed=True&today_only={today_only}",
            headers=[create_admin_authorization_header()],
        )

    assert resp.status_code == 200
    service = resp.json["data"]
    assert service["id"] == str(sample_service.id)
    assert "statistics" in service.keys()
    assert set(service["statistics"].keys()) == {SMS_TYPE, EMAIL_TYPE, LETTER_TYPE}
    assert service["statistics"][SMS_TYPE] == stats


def test_get_services_with_detailed_flag(client, sample_template):
    notifications = [
        create_notification(sample_template),
        create_notification(sample_template),
        create_notification(sample_template, key_type=KEY_TYPE_TEST),
    ]
    resp = client.get("/service?detailed=True", headers=[create_admin_authorization_header()])

    assert resp.status_code == 200
    data = resp.json["data"]
    assert len(data) == 1
    assert data[0]["name"] == "Sample service"
    assert data[0]["id"] == str(notifications[0].service_id)
    assert data[0]["statistics"] == {
        EMAIL_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
        SMS_TYPE: {"delivered": 0, "failed": 0, "requested": 3},
        LETTER_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
    }


def test_get_services_with_detailed_flag_excluding_from_test_key(client, sample_template):
    create_notification(sample_template, key_type=KEY_TYPE_NORMAL)
    create_notification(sample_template, key_type=KEY_TYPE_TEAM)
    create_notification(sample_template, key_type=KEY_TYPE_TEST)
    create_notification(sample_template, key_type=KEY_TYPE_TEST)
    create_notification(sample_template, key_type=KEY_TYPE_TEST)

    resp = client.get(
        "/service?detailed=True&include_from_test_key=False", headers=[create_admin_authorization_header()]
    )

    assert resp.status_code == 200
    data = resp.json["data"]
    assert len(data) == 1
    assert data[0]["statistics"] == {
        EMAIL_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
        SMS_TYPE: {"delivered": 0, "failed": 0, "requested": 2},
        LETTER_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
    }


def test_get_services_with_detailed_flag_accepts_date_range(client, mocker):
    mock_get_detailed_services = mocker.patch("app.service.rest.get_detailed_services", return_value={})
    resp = client.get(
        url_for("service.get_services", detailed=True, start_date="2001-01-01", end_date="2002-02-02"),
        headers=[create_admin_authorization_header()],
    )

    mock_get_detailed_services.assert_called_once_with(
        start_date=date(2001, 1, 1), end_date=date(2002, 2, 2), only_active=ANY, include_from_test_key=ANY
    )
    assert resp.status_code == 200


@freeze_time("2002-02-02")
def test_get_services_with_detailed_flag_defaults_to_today(client, mocker):
    mock_get_detailed_services = mocker.patch("app.service.rest.get_detailed_services", return_value={})
    resp = client.get(url_for("service.get_services", detailed=True), headers=[create_admin_authorization_header()])

    mock_get_detailed_services.assert_called_once_with(
        end_date=date(2002, 2, 2), include_from_test_key=ANY, only_active=ANY, start_date=date(2002, 2, 2)
    )

    assert resp.status_code == 200


def test_get_detailed_services_groups_by_service(notify_db_session):
    from app.service.rest import get_detailed_services

    service_1 = create_service(service_name="1")
    service_2 = create_service(service_name="2")

    service_1_template = create_template(service_1)
    service_2_template = create_template(service_2)

    create_notification(service_1_template, status="created")
    create_notification(service_2_template, status="created")
    create_notification(service_1_template, status="delivered")
    create_notification(service_1_template, status="created")

    data = get_detailed_services(start_date=datetime.utcnow().date(), end_date=datetime.utcnow().date())
    data = sorted(data, key=lambda x: x["name"])

    assert len(data) == 2
    assert data[0]["id"] == str(service_1.id)
    assert data[0]["statistics"] == {
        EMAIL_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
        SMS_TYPE: {"delivered": 1, "failed": 0, "requested": 3},
        LETTER_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
    }
    assert data[1]["id"] == str(service_2.id)
    assert data[1]["statistics"] == {
        EMAIL_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
        SMS_TYPE: {"delivered": 0, "failed": 0, "requested": 1},
        LETTER_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
    }


def test_get_detailed_services_includes_services_with_no_notifications(notify_db_session):
    from app.service.rest import get_detailed_services

    service_1 = create_service(service_name="1")
    service_2 = create_service(service_name="2")

    service_1_template = create_template(service_1)
    create_notification(service_1_template)

    data = get_detailed_services(start_date=datetime.utcnow().date(), end_date=datetime.utcnow().date())
    data = sorted(data, key=lambda x: x["name"])

    assert len(data) == 2
    assert data[0]["id"] == str(service_1.id)
    assert data[0]["statistics"] == {
        EMAIL_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
        SMS_TYPE: {"delivered": 0, "failed": 0, "requested": 1},
        LETTER_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
    }
    assert data[1]["id"] == str(service_2.id)
    assert data[1]["statistics"] == {
        EMAIL_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
        SMS_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
        LETTER_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
    }


def test_get_detailed_services_only_includes_todays_notifications(sample_template):
    from app.service.rest import get_detailed_services

    create_notification(sample_template, created_at=datetime(2015, 10, 9, 23, 59))
    create_notification(sample_template, created_at=datetime(2015, 10, 10, 0, 0))
    create_notification(sample_template, created_at=datetime(2015, 10, 10, 12, 0))
    create_notification(sample_template, created_at=datetime(2015, 10, 10, 23, 0))

    with freeze_time("2015-10-10T12:00:00"):
        data = get_detailed_services(start_date=datetime.utcnow().date(), end_date=datetime.utcnow().date())
        data = sorted(data, key=lambda x: x["id"])

    assert len(data) == 1
    assert data[0]["statistics"] == {
        EMAIL_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
        SMS_TYPE: {"delivered": 0, "failed": 0, "requested": 3},
        LETTER_TYPE: {"delivered": 0, "failed": 0, "requested": 0},
    }


@pytest.mark.parametrize("start_date_delta, end_date_delta", [(2, 1), (3, 2), (1, 0)])
@freeze_time("2017-03-28T12:00:00")
def test_get_detailed_services_for_date_range(sample_template, start_date_delta, end_date_delta):
    from app.service.rest import get_detailed_services

    create_ft_notification_status(
        bst_date=(datetime.utcnow() - timedelta(days=3)).date(),
        service=sample_template.service,
        notification_type="sms",
    )
    create_ft_notification_status(
        bst_date=(datetime.utcnow() - timedelta(days=2)).date(),
        service=sample_template.service,
        notification_type="sms",
    )
    create_ft_notification_status(
        bst_date=(datetime.utcnow() - timedelta(days=1)).date(),
        service=sample_template.service,
        notification_type="sms",
    )

    create_notification(template=sample_template, created_at=datetime.utcnow(), status="delivered")

    start_date = (datetime.utcnow() - timedelta(days=start_date_delta)).date()
    end_date = (datetime.utcnow() - timedelta(days=end_date_delta)).date()

    data = get_detailed_services(
        only_active=False, include_from_test_key=True, start_date=start_date, end_date=end_date
    )

    assert len(data) == 1
    assert data[0]["statistics"][EMAIL_TYPE] == {"delivered": 0, "failed": 0, "requested": 0}
    assert data[0]["statistics"][SMS_TYPE] == {"delivered": 2, "failed": 0, "requested": 2}
    assert data[0]["statistics"][LETTER_TYPE] == {"delivered": 0, "failed": 0, "requested": 0}


def test_search_for_notification_by_to_field(client, sample_template, sample_email_template):
    notification1 = create_notification(
        template=sample_template, to_field="+447700900855", normalised_to="447700900855"
    )
    notification2 = create_notification(
        template=sample_email_template, to_field="jack@gmail.com", normalised_to="jack@gmail.com"
    )

    response = client.get(
        "/service/{}/notifications?to={}&template_type={}".format(notification1.service_id, "jack@gmail.com", "email"),
        headers=[create_admin_authorization_header()],
    )
    notifications = json.loads(response.get_data(as_text=True))["notifications"]

    assert response.status_code == 200
    assert len(notifications) == 1
    assert str(notification2.id) == notifications[0]["id"]


def test_search_for_notification_by_to_field_return_empty_list_if_there_is_no_match(
    client, sample_template, sample_email_template
):
    notification1 = create_notification(sample_template, to_field="+447700900855")
    create_notification(sample_email_template, to_field="jack@gmail.com")

    response = client.get(
        "/service/{}/notifications?to={}&template_type={}".format(notification1.service_id, "+447700900800", "sms"),
        headers=[create_admin_authorization_header()],
    )
    notifications = json.loads(response.get_data(as_text=True))["notifications"]

    assert response.status_code == 200
    assert len(notifications) == 0


def test_search_for_notification_by_to_field_return_multiple_matches(client, sample_template, sample_email_template):
    notification1 = create_notification(sample_template, to_field="+447700900855", normalised_to="447700900855")
    notification2 = create_notification(sample_template, to_field=" +44 77009 00855 ", normalised_to="447700900855")
    notification3 = create_notification(sample_template, to_field="+44770 0900 855", normalised_to="447700900855")
    notification4 = create_notification(
        sample_email_template, to_field="jack@gmail.com", normalised_to="jack@gmail.com"
    )

    response = client.get(
        "/service/{}/notifications?to={}&template_type={}".format(notification1.service_id, "+447700900855", "sms"),
        headers=[create_admin_authorization_header()],
    )
    notifications = json.loads(response.get_data(as_text=True))["notifications"]
    notification_ids = [notification["id"] for notification in notifications]

    assert response.status_code == 200
    assert len(notifications) == 3

    assert str(notification1.id) in notification_ids
    assert str(notification2.id) in notification_ids
    assert str(notification3.id) in notification_ids
    assert str(notification4.id) not in notification_ids


def test_search_for_notification_by_to_field_returns_next_link_if_more_than_50(client, sample_template):
    for _ in range(51):
        create_notification(sample_template, to_field="+447700900855", normalised_to="447700900855")

    response = client.get(
        f"/service/{sample_template.service_id}/notifications?to=+447700900855&template_type=sms",
        headers=[create_admin_authorization_header()],
    )
    assert response.status_code == 200
    response_json = json.loads(response.get_data(as_text=True))

    assert len(response_json["notifications"]) == 50
    assert "prev" not in response_json["links"]
    assert response_json["links"]["next"]


def test_search_for_notification_by_to_field_returns_no_next_link_if_50_or_less(client, sample_template):
    for _ in range(50):
        create_notification(sample_template, to_field="+447700900855", normalised_to="447700900855")

    response = client.get(
        "/service/{}/notifications?to={}&template_type={}".format(sample_template.service_id, "+447700900855", "sms"),
        headers=[create_admin_authorization_header()],
    )
    assert response.status_code == 200
    response_json = json.loads(response.get_data(as_text=True))

    assert len(response_json["notifications"]) == 50
    assert response_json["links"] == {}


def test_search_for_notification_by_to_field_for_letter(
    client,
    notify_db_session,
    sample_letter_template,
    sample_email_template,
    sample_template,
):
    letter_notification = create_notification(sample_letter_template, to_field="A. Name", normalised_to="a.name")
    create_notification(sample_email_template, to_field="A.Name@example.com", normalised_to="a.name@example.com")
    create_notification(sample_template, to_field="44770900123", normalised_to="44770900123")
    response = client.get(
        "/service/{}/notifications?to={}&template_type={}".format(
            sample_letter_template.service_id,
            "A. Name",
            "letter",
        ),
        headers=[create_admin_authorization_header()],
    )
    notifications = json.loads(response.get_data(as_text=True))["notifications"]

    assert response.status_code == 200
    assert len(notifications) == 1
    assert notifications[0]["id"] == str(letter_notification.id)


def test_update_service_calls_send_notification_as_service_becomes_live(notify_db_session, client, mocker):
    send_notification_mock = mocker.patch("app.service.rest.send_notification_to_service_users")

    restricted_service = create_service(restricted=True)

    data = {"restricted": False}

    auth_header = create_admin_authorization_header()
    resp = client.post(
        f"service/{restricted_service.id}",
        data=json.dumps(data),
        headers=[auth_header],
        content_type="application/json",
    )

    assert resp.status_code == 200
    send_notification_mock.assert_called_once_with(
        service_id=restricted_service.id,
        template_id="618185c6-3636-49cd-b7d2-6f6f5eb3bdde",
        personalisation={
            "service_name": restricted_service.name,
        },
        include_user_fields=["name"],
    )


def test_update_service_does_not_call_send_notification_for_live_service(sample_service, client, mocker):
    send_notification_mock = mocker.patch("app.service.rest.send_notification_to_service_users")

    data = {"restricted": True}

    auth_header = create_admin_authorization_header()
    resp = client.post(
        f"service/{sample_service.id}",
        data=json.dumps(data),
        headers=[auth_header],
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert not send_notification_mock.called


def test_update_service_does_not_call_send_notification_when_restricted_not_changed(sample_service, client, mocker):
    send_notification_mock = mocker.patch("app.service.rest.send_notification_to_service_users")

    data = {"name": "Name of service"}

    auth_header = create_admin_authorization_header()
    resp = client.post(
        f"service/{sample_service.id}",
        data=json.dumps(data),
        headers=[auth_header],
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert not send_notification_mock.called


def test_search_for_notification_by_to_field_filters_by_status(client, sample_template):
    notification1 = create_notification(
        sample_template, to_field="+447700900855", status="delivered", normalised_to="447700900855"
    )
    create_notification(sample_template, to_field="+447700900855", status="sending", normalised_to="447700900855")

    response = client.get(
        "/service/{}/notifications?to={}&status={}&template_type={}".format(
            notification1.service_id, "+447700900855", "delivered", "sms"
        ),
        headers=[create_admin_authorization_header()],
    )
    notifications = json.loads(response.get_data(as_text=True))["notifications"]
    notification_ids = [notification["id"] for notification in notifications]

    assert response.status_code == 200
    assert len(notifications) == 1
    assert str(notification1.id) in notification_ids


def test_search_for_notification_by_to_field_filters_by_statuses(client, sample_template):
    notification1 = create_notification(
        sample_template, to_field="+447700900855", status="delivered", normalised_to="447700900855"
    )
    notification2 = create_notification(
        sample_template, to_field="+447700900855", status="sending", normalised_to="447700900855"
    )

    response = client.get(
        "/service/{}/notifications?to={}&status={}&status={}&template_type={}".format(
            notification1.service_id, "+447700900855", "delivered", "sending", "sms"
        ),
        headers=[create_admin_authorization_header()],
    )
    notifications = json.loads(response.get_data(as_text=True))["notifications"]
    notification_ids = [notification["id"] for notification in notifications]

    assert response.status_code == 200
    assert len(notifications) == 2
    assert str(notification1.id) in notification_ids
    assert str(notification2.id) in notification_ids


def test_search_for_notification_by_to_field_returns_content(client, sample_template_with_placeholders):
    notification = create_notification(
        sample_template_with_placeholders,
        to_field="+447700900855",
        personalisation={"name": "Foo"},
        normalised_to="447700900855",
    )

    response = client.get(
        "/service/{}/notifications?to={}&template_type={}".format(
            sample_template_with_placeholders.service_id, "+447700900855", "sms"
        ),
        headers=[create_admin_authorization_header()],
    )
    notifications = json.loads(response.get_data(as_text=True))["notifications"]
    assert response.status_code == 200
    assert len(notifications) == 1

    assert notifications[0]["id"] == str(notification.id)
    assert notifications[0]["to"] == "+447700900855"
    assert notifications[0]["template"]["content"] == "Hello (( Name))\nYour thing is due soon"


def test_send_one_off_notification(sample_service, admin_request, mocker):
    template = create_template(service=sample_service)
    mocker.patch("app.service.send_notification.send_notification_to_queue")

    response = admin_request.post(
        "service.create_one_off_notification",
        service_id=sample_service.id,
        _data={"template_id": str(template.id), "to": "07700900001", "created_by": str(sample_service.created_by_id)},
        _expected_status=201,
    )

    noti = Notification.query.one()
    assert response["id"] == str(noti.id)


def test_create_pdf_letter(mocker, sample_service_full_permissions, client, fake_uuid, notify_user):
    mocker.patch("app.service.send_notification.utils_s3download")
    mocker.patch("app.service.send_notification.get_page_count", return_value=1)
    mocker.patch("app.service.send_notification.move_uploaded_pdf_to_letters_bucket")

    user = sample_service_full_permissions.users[0]
    data = json.dumps(
        {
            "filename": "valid.pdf",
            "created_by": str(user.id),
            "file_id": fake_uuid,
            "postage": "second",
            "recipient_address": "Bugs%20Bunny%0A123%20Main%20Street%0ALooney%20Town",
        }
    )

    response = client.post(
        url_for("service.create_pdf_letter", service_id=sample_service_full_permissions.id),
        data=data,
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    json_resp = json.loads(response.get_data(as_text=True))

    assert response.status_code == 201
    assert json_resp == {"id": fake_uuid}


@pytest.mark.parametrize(
    "post_data, expected_errors",
    [
        (
            {},
            [
                {"error": "ValidationError", "message": "postage is a required property"},
                {"error": "ValidationError", "message": "filename is a required property"},
                {"error": "ValidationError", "message": "created_by is a required property"},
                {"error": "ValidationError", "message": "file_id is a required property"},
                {"error": "ValidationError", "message": "recipient_address is a required property"},
            ],
        ),
        (
            {
                "postage": "third",
                "filename": "string",
                "created_by": "string",
                "file_id": "string",
                "recipient_address": "Some Address",
            },
            [
                {
                    "error": "ValidationError",
                    "message": "postage invalid. It must be first, second, europe or rest-of-world.",
                }
            ],
        ),
    ],
)
def test_create_pdf_letter_validates_against_json_schema(
    sample_service_full_permissions, client, post_data, expected_errors
):
    response = client.post(
        url_for("service.create_pdf_letter", service_id=sample_service_full_permissions.id),
        data=json.dumps(post_data),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    json_resp = json.loads(response.get_data(as_text=True))

    assert response.status_code == 400
    assert json_resp["errors"] == expected_errors


def test_get_notification_for_service_includes_template_redacted(admin_request, sample_notification):
    resp = admin_request.get(
        "service.get_notification_for_service",
        service_id=sample_notification.service_id,
        notification_id=sample_notification.id,
    )

    assert resp["id"] == str(sample_notification.id)
    assert resp["template"]["redact_personalisation"] is False


def test_get_notification_for_service_includes_precompiled_letter(admin_request, sample_notification):
    resp = admin_request.get(
        "service.get_notification_for_service",
        service_id=sample_notification.service_id,
        notification_id=sample_notification.id,
    )

    assert resp["id"] == str(sample_notification.id)
    assert resp["template"]["is_precompiled_letter"] is False


def test_get_all_notifications_for_service_includes_template_redacted(admin_request, sample_service):
    normal_template = create_template(sample_service)

    redacted_template = create_template(sample_service)
    dao_redact_template(redacted_template, sample_service.created_by_id)

    with freeze_time("2000-01-01"):
        redacted_noti = create_notification(redacted_template)
    with freeze_time("2000-01-02"):
        normal_noti = create_notification(normal_template)

    resp = admin_request.get("service.get_all_notifications_for_service", service_id=sample_service.id)

    assert resp["notifications"][0]["id"] == str(normal_noti.id)
    assert resp["notifications"][0]["template"]["redact_personalisation"] is False

    assert resp["notifications"][1]["id"] == str(redacted_noti.id)
    assert resp["notifications"][1]["template"]["redact_personalisation"] is True


def test_get_all_notifications_for_service_includes_template_hidden(admin_request, sample_service):
    letter_template = create_template(sample_service, template_type=LETTER_TYPE)
    precompiled_template = create_template(
        sample_service,
        template_type=LETTER_TYPE,
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        hidden=True,
    )

    with freeze_time("2000-01-01"):
        letter_noti = create_notification(letter_template)
    with freeze_time("2000-01-02"):
        precompiled_noti = create_notification(precompiled_template)

    resp = admin_request.get("service.get_all_notifications_for_service", service_id=sample_service.id)

    assert resp["notifications"][0]["id"] == str(precompiled_noti.id)
    assert resp["notifications"][0]["template"]["is_precompiled_letter"] is True

    assert resp["notifications"][1]["id"] == str(letter_noti.id)
    assert resp["notifications"][1]["template"]["is_precompiled_letter"] is False


def test_search_for_notification_by_to_field_returns_personlisation(client, sample_template_with_placeholders):
    create_notification(
        sample_template_with_placeholders,
        to_field="+447700900855",
        personalisation={"name": "Foo"},
        normalised_to="447700900855",
    )

    response = client.get(
        "/service/{}/notifications?to={}&template_type={}".format(
            sample_template_with_placeholders.service_id, "+447700900855", "sms"
        ),
        headers=[create_admin_authorization_header()],
    )
    notifications = json.loads(response.get_data(as_text=True))["notifications"]

    assert response.status_code == 200
    assert len(notifications) == 1
    assert "personalisation" in notifications[0].keys()
    assert notifications[0]["personalisation"]["name"] == "Foo"


def test_search_for_notification_by_to_field_returns_notifications_by_type(
    client, sample_template, sample_email_template
):
    sms_notification = create_notification(sample_template, to_field="+447700900855", normalised_to="447700900855")
    create_notification(sample_email_template, to_field="44770@gamil.com", normalised_to="44770@gamil.com")

    response = client.get(
        "/service/{}/notifications?to={}&template_type={}".format(sms_notification.service_id, "0770", "sms"),
        headers=[create_admin_authorization_header()],
    )
    notifications = json.loads(response.get_data(as_text=True))["notifications"]

    assert response.status_code == 200
    assert len(notifications) == 1
    assert notifications[0]["id"] == str(sms_notification.id)


def test_get_email_reply_to_addresses_when_there_are_no_reply_to_email_addresses(client, sample_service):
    response = client.get(f"/service/{sample_service.id}/email-reply-to", headers=[create_admin_authorization_header()])

    assert json.loads(response.get_data(as_text=True)) == []
    assert response.status_code == 200


def test_get_email_reply_to_addresses_with_one_email_address(client, notify_db_session):
    service = create_service()
    create_reply_to_email(service, "test@mail.com")

    response = client.get(f"/service/{service.id}/email-reply-to", headers=[create_admin_authorization_header()])
    json_response = json.loads(response.get_data(as_text=True))

    assert len(json_response) == 1
    assert json_response[0]["email_address"] == "test@mail.com"
    assert json_response[0]["is_default"]
    assert json_response[0]["created_at"]
    assert not json_response[0]["updated_at"]
    assert response.status_code == 200


def test_get_email_reply_to_addresses_with_multiple_email_addresses(client, notify_db_session):
    service = create_service()
    reply_to_a = create_reply_to_email(service, "test_a@mail.com")
    reply_to_b = create_reply_to_email(service, "test_b@mail.com", False)

    response = client.get(f"/service/{service.id}/email-reply-to", headers=[create_admin_authorization_header()])
    json_response = json.loads(response.get_data(as_text=True))

    assert len(json_response) == 2
    assert response.status_code == 200

    assert json_response[0]["id"] == str(reply_to_a.id)
    assert json_response[0]["service_id"] == str(reply_to_a.service_id)
    assert json_response[0]["email_address"] == "test_a@mail.com"
    assert json_response[0]["is_default"]
    assert json_response[0]["created_at"]
    assert not json_response[0]["updated_at"]

    assert json_response[1]["id"] == str(reply_to_b.id)
    assert json_response[1]["service_id"] == str(reply_to_b.service_id)
    assert json_response[1]["email_address"] == "test_b@mail.com"
    assert not json_response[1]["is_default"]
    assert json_response[1]["created_at"]
    assert not json_response[1]["updated_at"]


def test_verify_reply_to_email_address_should_send_verification_email(
    admin_request, notify_db_session, mocker, verify_reply_to_address_email_template
):
    service = create_service()
    mocked = mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    data = {"email": "reply-here@example.gov.uk"}
    notify_service = verify_reply_to_address_email_template.service
    response = admin_request.post(
        "service.verify_reply_to_email_address", service_id=service.id, _data=data, _expected_status=201
    )

    notification = Notification.query.first()
    assert notification.template_id == verify_reply_to_address_email_template.id
    assert response["data"] == {"id": str(notification.id)}
    mocked.assert_called_once_with([str(notification.id)], queue="notify-internal-tasks")
    assert notification.reply_to_text == notify_service.get_default_reply_to_email_address()


def test_verify_reply_to_email_address_doesnt_allow_duplicates(admin_request, notify_db_session, mocker):
    data = {"email": "reply-here@example.gov.uk"}
    service = create_service()
    create_reply_to_email(service, "reply-here@example.gov.uk")
    response = admin_request.post(
        "service.verify_reply_to_email_address", service_id=service.id, _data=data, _expected_status=409
    )
    assert response["message"] == "‘reply-here@example.gov.uk’ is already a reply-to email address for this service."


def test_add_service_reply_to_email_address(admin_request, sample_service):
    data = {"email_address": "new@reply.com", "is_default": True}
    response = admin_request.post(
        "service.add_service_reply_to_email_address", service_id=sample_service.id, _data=data, _expected_status=201
    )

    results = ServiceEmailReplyTo.query.all()
    assert len(results) == 1
    assert response["data"] == results[0].serialize()


def test_add_service_reply_to_email_address_doesnt_allow_duplicates(admin_request, notify_db_session, mocker):
    data = {"email_address": "reply-here@example.gov.uk", "is_default": True}
    service = create_service()
    create_reply_to_email(service, "reply-here@example.gov.uk")
    response = admin_request.post(
        "service.add_service_reply_to_email_address", service_id=service.id, _data=data, _expected_status=409
    )
    assert response["message"] == "‘reply-here@example.gov.uk’ is already a reply-to email address for this service."


def test_add_service_reply_to_email_address_can_add_multiple_addresses(admin_request, sample_service):
    data = {"email_address": "first@reply.com", "is_default": True}
    admin_request.post(
        "service.add_service_reply_to_email_address", service_id=sample_service.id, _data=data, _expected_status=201
    )
    second = {"email_address": "second@reply.com", "is_default": True}
    response = admin_request.post(
        "service.add_service_reply_to_email_address", service_id=sample_service.id, _data=second, _expected_status=201
    )
    results = ServiceEmailReplyTo.query.all()
    assert len(results) == 2
    default = [x for x in results if x.is_default]
    assert response["data"] == default[0].serialize()
    first_reply_to_not_default = [x for x in results if not x.is_default]
    assert first_reply_to_not_default[0].email_address == "first@reply.com"


def test_add_service_reply_to_email_address_raise_exception_if_no_default(admin_request, sample_service):
    data = {"email_address": "first@reply.com", "is_default": False}
    response = admin_request.post(
        "service.add_service_reply_to_email_address", service_id=sample_service.id, _data=data, _expected_status=400
    )
    assert response["message"] == "You must have at least one reply to email address as the default."


def test_add_service_reply_to_email_address_404s_when_invalid_service_id(admin_request, notify_db_session):
    response = admin_request.post(
        "service.add_service_reply_to_email_address", service_id=uuid.uuid4(), _data={}, _expected_status=404
    )

    assert response["result"] == "error"
    assert response["message"] == "No result found"


def test_update_service_reply_to_email_address(admin_request, sample_service):
    original_reply_to = create_reply_to_email(service=sample_service, email_address="some@email.com")
    data = {"email_address": "changed@reply.com", "is_default": True}
    response = admin_request.post(
        "service.update_service_reply_to_email_address",
        service_id=sample_service.id,
        reply_to_email_id=original_reply_to.id,
        _data=data,
        _expected_status=200,
    )

    results = ServiceEmailReplyTo.query.all()
    assert len(results) == 1
    assert response["data"] == results[0].serialize()


def test_update_service_reply_to_email_address_returns_400_when_no_default(admin_request, sample_service):
    original_reply_to = create_reply_to_email(service=sample_service, email_address="some@email.com")
    data = {"email_address": "changed@reply.com", "is_default": False}
    response = admin_request.post(
        "service.update_service_reply_to_email_address",
        service_id=sample_service.id,
        reply_to_email_id=original_reply_to.id,
        _data=data,
        _expected_status=400,
    )

    assert response["message"] == "You must have at least one reply to email address as the default."


def test_update_service_reply_to_email_address_404s_when_invalid_service_id(admin_request, notify_db_session):
    response = admin_request.post(
        "service.update_service_reply_to_email_address",
        service_id=uuid.uuid4(),
        reply_to_email_id=uuid.uuid4(),
        _data={},
        _expected_status=404,
    )

    assert response["result"] == "error"
    assert response["message"] == "No result found"


def test_delete_service_reply_to_email_address_archives_an_email_reply_to(
    sample_service, admin_request, notify_db_session
):
    create_reply_to_email(service=sample_service, email_address="some@email.com")
    reply_to = create_reply_to_email(service=sample_service, email_address="some@email.com", is_default=False)

    admin_request.post(
        "service.delete_service_reply_to_email_address",
        service_id=sample_service.id,
        reply_to_email_id=reply_to.id,
    )
    assert reply_to.archived is True


def test_delete_service_reply_to_email_address_returns_400_if_archiving_default_reply_to(
    admin_request, notify_db_session, sample_service
):
    reply_to = create_reply_to_email(service=sample_service, email_address="some@email.com")

    response = admin_request.post(
        "service.delete_service_reply_to_email_address",
        service_id=sample_service.id,
        reply_to_email_id=reply_to.id,
        _expected_status=400,
    )

    assert response == {"message": "You cannot delete a default email reply to address", "result": "error"}
    assert reply_to.archived is False


def test_get_email_reply_to_address(client, notify_db_session):
    service = create_service()
    reply_to = create_reply_to_email(service, "test_a@mail.com")

    response = client.get(
        f"/service/{service.id}/email-reply-to/{reply_to.id}",
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )

    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True)) == reply_to.serialize()


def test_get_letter_contacts_when_there_are_no_letter_contacts(client, sample_service):
    response = client.get(f"/service/{sample_service.id}/letter-contact", headers=[create_admin_authorization_header()])

    assert json.loads(response.get_data(as_text=True)) == []
    assert response.status_code == 200


def test_get_letter_contacts_with_one_letter_contact(client, notify_db_session):
    service = create_service()
    create_letter_contact(service, "Aberdeen, AB23 1XH")

    response = client.get(f"/service/{service.id}/letter-contact", headers=[create_admin_authorization_header()])
    json_response = json.loads(response.get_data(as_text=True))

    assert len(json_response) == 1
    assert json_response[0]["contact_block"] == "Aberdeen, AB23 1XH"
    assert json_response[0]["is_default"]
    assert json_response[0]["created_at"]
    assert not json_response[0]["updated_at"]
    assert response.status_code == 200


def test_get_letter_contacts_with_multiple_letter_contacts(client, notify_db_session):
    service = create_service()
    letter_contact_a = create_letter_contact(service, "Aberdeen, AB23 1XH")
    letter_contact_b = create_letter_contact(service, "London, E1 8QS", False)

    response = client.get(f"/service/{service.id}/letter-contact", headers=[create_admin_authorization_header()])
    json_response = json.loads(response.get_data(as_text=True))

    assert len(json_response) == 2
    assert response.status_code == 200

    assert json_response[0]["id"] == str(letter_contact_a.id)
    assert json_response[0]["service_id"] == str(letter_contact_a.service_id)
    assert json_response[0]["contact_block"] == "Aberdeen, AB23 1XH"
    assert json_response[0]["is_default"]
    assert json_response[0]["created_at"]
    assert not json_response[0]["updated_at"]

    assert json_response[1]["id"] == str(letter_contact_b.id)
    assert json_response[1]["service_id"] == str(letter_contact_b.service_id)
    assert json_response[1]["contact_block"] == "London, E1 8QS"
    assert not json_response[1]["is_default"]
    assert json_response[1]["created_at"]
    assert not json_response[1]["updated_at"]


def test_get_letter_contact_by_id(client, notify_db_session):
    service = create_service()
    letter_contact = create_letter_contact(service, "London, E1 8QS")

    response = client.get(
        f"/service/{service.id}/letter-contact/{letter_contact.id}",
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )

    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True)) == letter_contact.serialize()


def test_get_letter_contact_return_404_when_invalid_contact_id(client, notify_db_session):
    service = create_service()

    response = client.get(
        "/service/{}/letter-contact/{}".format(service.id, "93d59f88-4aa1-453c-9900-f61e2fc8a2de"),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )

    assert response.status_code == 404


def test_add_service_contact_block(client, sample_service):
    data = json.dumps({"contact_block": "London, E1 8QS", "is_default": True})
    response = client.post(
        f"/service/{sample_service.id}/letter-contact",
        data=data,
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )

    assert response.status_code == 201
    json_resp = json.loads(response.get_data(as_text=True))
    results = ServiceLetterContact.query.all()
    assert len(results) == 1
    assert json_resp["data"] == results[0].serialize()


def test_add_service_letter_contact_can_add_multiple_addresses(client, sample_service):
    first = json.dumps({"contact_block": "London, E1 8QS", "is_default": True})
    client.post(
        f"/service/{sample_service.id}/letter-contact",
        data=first,
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )

    second = json.dumps({"contact_block": "Aberdeen, AB23 1XH", "is_default": True})
    response = client.post(
        f"/service/{sample_service.id}/letter-contact",
        data=second,
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 201
    json_resp = json.loads(response.get_data(as_text=True))
    results = ServiceLetterContact.query.all()
    assert len(results) == 2
    default = [x for x in results if x.is_default]
    assert json_resp["data"] == default[0].serialize()
    first_letter_contact_not_default = [x for x in results if not x.is_default]
    assert first_letter_contact_not_default[0].contact_block == "London, E1 8QS"


def test_add_service_letter_contact_block_fine_if_no_default(client, sample_service):
    data = json.dumps({"contact_block": "London, E1 8QS", "is_default": False})
    response = client.post(
        f"/service/{sample_service.id}/letter-contact",
        data=data,
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 201


def test_add_service_letter_contact_block_404s_when_invalid_service_id(client, notify_db_session):
    response = client.post(
        f"/service/{uuid.uuid4()}/letter-contact",
        data={},
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )

    assert response.status_code == 404
    result = json.loads(response.get_data(as_text=True))
    assert result["result"] == "error"
    assert result["message"] == "No result found"


def test_update_service_letter_contact(client, sample_service):
    original_letter_contact = create_letter_contact(service=sample_service, contact_block="Aberdeen, AB23 1XH")
    data = json.dumps({"contact_block": "London, E1 8QS", "is_default": True})
    response = client.post(
        f"/service/{sample_service.id}/letter-contact/{original_letter_contact.id}",
        data=data,
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )

    assert response.status_code == 200
    json_resp = json.loads(response.get_data(as_text=True))
    results = ServiceLetterContact.query.all()
    assert len(results) == 1
    assert json_resp["data"] == results[0].serialize()


def test_update_service_letter_contact_returns_200_when_no_default(client, sample_service):
    original_reply_to = create_letter_contact(service=sample_service, contact_block="Aberdeen, AB23 1XH")
    data = json.dumps({"contact_block": "London, E1 8QS", "is_default": False})
    response = client.post(
        f"/service/{sample_service.id}/letter-contact/{original_reply_to.id}",
        data=data,
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 200


def test_update_service_letter_contact_returns_404_when_invalid_service_id(client, notify_db_session):
    response = client.post(
        f"/service/{uuid.uuid4()}/letter-contact/{uuid.uuid4()}",
        data={},
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )

    assert response.status_code == 404
    result = json.loads(response.get_data(as_text=True))
    assert result["result"] == "error"
    assert result["message"] == "No result found"


def test_delete_service_letter_contact_can_archive_letter_contact(admin_request, notify_db_session):
    service = create_service()
    create_letter_contact(service=service, contact_block="Edinburgh, ED1 1AA")
    letter_contact = create_letter_contact(service=service, contact_block="Swansea, SN1 3CC", is_default=False)

    admin_request.post(
        "service.delete_service_letter_contact",
        service_id=service.id,
        letter_contact_id=letter_contact.id,
    )

    assert letter_contact.archived is True


def test_delete_service_letter_contact_returns_200_if_archiving_template_default(admin_request, notify_db_session):
    service = create_service()
    create_letter_contact(service=service, contact_block="Edinburgh, ED1 1AA")
    letter_contact = create_letter_contact(service=service, contact_block="Swansea, SN1 3CC", is_default=False)
    create_template(service=service, template_type="letter", reply_to=letter_contact.id)

    response = admin_request.post(
        "service.delete_service_letter_contact",
        service_id=service.id,
        letter_contact_id=letter_contact.id,
        _expected_status=200,
    )
    assert response["data"]["archived"] is True


def test_add_service_sms_sender_can_add_multiple_senders(client, notify_db_session):
    service = create_service()
    data = {
        "sms_sender": "second",
        "is_default": False,
    }
    response = client.post(
        f"/service/{service.id}/sms-sender",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 201
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["sms_sender"] == "second"
    assert not resp_json["is_default"]
    senders = ServiceSmsSender.query.all()
    assert len(senders) == 2


def test_add_service_sms_sender_switches_default(client, notify_db_session):
    service = create_service_with_defined_sms_sender(sms_sender_value="first")
    data = {
        "sms_sender": "second",
        "is_default": True,
    }
    response = client.post(
        f"/service/{service.id}/sms-sender",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 201
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["sms_sender"] == "second"
    assert not resp_json["inbound_number_id"]
    assert resp_json["is_default"]
    sms_senders = ServiceSmsSender.query.filter_by(sms_sender="first").first()
    assert not sms_senders.is_default


def test_add_service_sms_sender_return_404_when_service_does_not_exist(client):
    data = {"sms_sender": "12345", "is_default": False}
    response = client.post(
        f"/service/{uuid.uuid4()}/sms-sender",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 404
    result = json.loads(response.get_data(as_text=True))
    assert result["result"] == "error"
    assert result["message"] == "No result found"


def test_update_service_sms_sender(client, notify_db_session):
    service = create_service()
    service_sms_sender = create_service_sms_sender(service=service, sms_sender="1235", is_default=False)
    data = {
        "sms_sender": "second",
        "is_default": False,
    }
    response = client.post(
        f"/service/{service.id}/sms-sender/{service_sms_sender.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 200
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["sms_sender"] == "second"
    assert not resp_json["inbound_number_id"]
    assert not resp_json["is_default"]


def test_update_service_sms_sender_switches_default(client, notify_db_session):
    service = create_service_with_defined_sms_sender(sms_sender_value="first")
    service_sms_sender = create_service_sms_sender(service=service, sms_sender="1235", is_default=False)
    data = {
        "sms_sender": "second",
        "is_default": True,
    }
    response = client.post(
        f"/service/{service.id}/sms-sender/{service_sms_sender.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 200
    resp_json = json.loads(response.get_data(as_text=True))
    assert resp_json["sms_sender"] == "second"
    assert not resp_json["inbound_number_id"]
    assert resp_json["is_default"]
    sms_senders = ServiceSmsSender.query.filter_by(sms_sender="first").first()
    assert not sms_senders.is_default


def test_update_service_sms_sender_does_not_allow_sender_update_for_inbound_number(client, notify_db_session):
    service = create_service()
    inbound_number = create_inbound_number("12345", service_id=service.id)
    service_sms_sender = create_service_sms_sender(
        service=service, sms_sender="1235", is_default=False, inbound_number_id=inbound_number.id
    )
    data = {"sms_sender": "second", "is_default": True, "inbound_number_id": str(inbound_number.id)}
    response = client.post(
        f"/service/{service.id}/sms-sender/{service_sms_sender.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 400


def test_update_service_sms_sender_return_404_when_service_does_not_exist(client):
    data = {"sms_sender": "12345", "is_default": False}
    response = client.post(
        f"/service/{uuid.uuid4()}/sms-sender/{uuid.uuid4()}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 404
    result = json.loads(response.get_data(as_text=True))
    assert result["result"] == "error"
    assert result["message"] == "No result found"


def test_delete_service_sms_sender_can_archive_sms_sender(admin_request, notify_db_session):
    service = create_service()
    service_sms_sender = create_service_sms_sender(service=service, sms_sender="5678", is_default=False)

    admin_request.post(
        "service.delete_service_sms_sender",
        service_id=service.id,
        sms_sender_id=service_sms_sender.id,
    )

    assert service_sms_sender.archived is True


def test_delete_service_sms_sender_returns_400_if_archiving_inbound_number(admin_request, notify_db_session):
    service = create_service_with_inbound_number(inbound_number="7654321")
    inbound_number = service.service_sms_senders[0]

    response = admin_request.post(
        "service.delete_service_sms_sender",
        service_id=service.id,
        sms_sender_id=service.service_sms_senders[0].id,
        _expected_status=400,
    )
    assert response == {"message": "You cannot delete an inbound number", "result": "error"}
    assert inbound_number.archived is False


def test_get_service_sms_sender_by_id(client, notify_db_session):
    service_sms_sender = create_service_sms_sender(service=create_service(), sms_sender="1235", is_default=False)
    response = client.get(
        f"/service/{service_sms_sender.service_id}/sms-sender/{service_sms_sender.id}",
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True)) == service_sms_sender.serialize()


def test_get_service_sms_sender_by_id_returns_404_when_service_does_not_exist(client, notify_db_session):
    service_sms_sender = create_service_sms_sender(service=create_service(), sms_sender="1235", is_default=False)
    response = client.get(
        f"/service/{uuid.uuid4()}/sms-sender/{service_sms_sender.id}",
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 404


def test_get_service_sms_sender_by_id_returns_404_when_sms_sender_does_not_exist(client, notify_db_session):
    service = create_service()
    response = client.get(
        f"/service/{service.id}/sms-sender/{uuid.uuid4()}",
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 404


def test_get_service_sms_senders_for_service(client, notify_db_session):
    service_sms_sender = create_service_sms_sender(service=create_service(), sms_sender="second", is_default=False)
    response = client.get(
        f"/service/{service_sms_sender.service_id}/sms-sender",
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 200
    json_resp = json.loads(response.get_data(as_text=True))
    assert len(json_resp) == 2
    assert json_resp[0]["is_default"]
    assert json_resp[0]["sms_sender"] == current_app.config["FROM_NUMBER"]
    assert not json_resp[1]["is_default"]
    assert json_resp[1]["sms_sender"] == "second"


def test_get_service_sms_senders_for_service_returns_empty_list_when_service_does_not_exist(client):
    response = client.get(
        f"/service/{uuid.uuid4()}/sms-sender",
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
    )
    assert response.status_code == 200
    assert json.loads(response.get_data(as_text=True)) == []


def test_get_organisation_for_service_id(admin_request, sample_service, sample_organisation):
    dao_add_service_to_organisation(sample_service, sample_organisation.id)
    response = admin_request.get("service.get_organisation_for_service", service_id=sample_service.id)
    assert response == sample_organisation.serialize()


def test_get_organisation_for_service_id_return_empty_dict_if_service_not_in_organisation(admin_request, fake_uuid):
    response = admin_request.get("service.get_organisation_for_service", service_id=fake_uuid)
    assert response == {}


def test_cancel_notification_for_service_raises_invalid_request_when_notification_is_not_found(
    admin_request,
    sample_service,
    fake_uuid,
):
    response = admin_request.post(
        "service.cancel_notification_for_service",
        service_id=sample_service.id,
        notification_id=fake_uuid,
        _expected_status=404,
    )
    assert response["message"] == "Notification not found"
    assert response["result"] == "error"


def test_cancel_notification_for_service_raises_invalid_request_when_notification_is_not_a_letter(
    admin_request,
    sample_notification,
    mocker,
):
    mock_adjust_redis = mocker.patch("app.service.rest.adjust_daily_service_limits_for_cancelled_letters")
    response = admin_request.post(
        "service.cancel_notification_for_service",
        service_id=sample_notification.service_id,
        notification_id=sample_notification.id,
        _expected_status=400,
    )
    assert response["message"] == "Notification cannot be cancelled - only letters can be cancelled"
    assert response["result"] == "error"
    assert not mock_adjust_redis.called


@pytest.mark.parametrize(
    "notification_status",
    [
        "cancelled",
        "sending",
        "sent",
        "delivered",
        "pending",
        "failed",
        "technical-failure",
        "temporary-failure",
        "permanent-failure",
        "validation-failed",
        "virus-scan-failed",
        "returned-letter",
    ],
)
@freeze_time("2018-07-07 12:00:00")
def test_cancel_notification_for_service_raises_invalid_request_when_letter_is_in_wrong_state_to_be_cancelled(
    admin_request,
    sample_letter_notification,
    mocker,
    notification_status,
):
    mock_adjust_redis = mocker.patch("app.service.rest.adjust_daily_service_limits_for_cancelled_letters")
    sample_letter_notification.status = notification_status
    sample_letter_notification.created_at = datetime.now()

    response = admin_request.post(
        "service.cancel_notification_for_service",
        service_id=sample_letter_notification.service_id,
        notification_id=sample_letter_notification.id,
        _expected_status=400,
    )
    if notification_status == "cancelled":
        assert response["message"] == "This letter has already been cancelled."
    else:
        assert response["message"] == (
            f"We could not cancel this letter. "
            f"Letter status: {notification_status}, created_at: 2018-07-07 12:00:00"
        )
    assert response["result"] == "error"
    assert not mock_adjust_redis.called


@pytest.mark.parametrize("notification_status", ["created", "pending-virus-check"])
@freeze_time("2018-07-07 16:00:00")
def test_cancel_notification_for_service_updates_letter_if_letter_is_in_cancellable_state(
    admin_request,
    sample_letter_notification,
    mocker,
    notification_status,
):
    mock_adjust_redis = mocker.patch("app.service.rest.adjust_daily_service_limits_for_cancelled_letters")
    sample_letter_notification.status = notification_status
    sample_letter_notification.created_at = datetime.now()

    response = admin_request.post(
        "service.cancel_notification_for_service",
        service_id=sample_letter_notification.service_id,
        notification_id=sample_letter_notification.id,
    )
    assert response["status"] == "cancelled"
    mock_adjust_redis.assert_called_once_with(
        sample_letter_notification.service_id, 1, sample_letter_notification.created_at
    )


@freeze_time("2017-12-12 17:30:00")
def test_cancel_notification_for_service_raises_error_if_its_too_late_to_cancel(
    admin_request,
    sample_letter_notification,
    mocker,
):
    mock_adjust_redis = mocker.patch("app.service.rest.adjust_daily_service_limits_for_cancelled_letters")
    sample_letter_notification.created_at = datetime(2017, 12, 11, 17, 0)

    response = admin_request.post(
        "service.cancel_notification_for_service",
        service_id=sample_letter_notification.service_id,
        notification_id=sample_letter_notification.id,
        _expected_status=400,
    )
    assert response["message"] == "It’s too late to cancel this letter. Printing started on 11 December at 5.30pm"
    assert response["result"] == "error"
    assert not mock_adjust_redis.called


@pytest.mark.parametrize(
    "created_at",
    [
        datetime(2018, 7, 6, 22, 30),  # yesterday evening
        datetime(2018, 7, 6, 23, 30),  # this morning early hours (in bst)
        datetime(2018, 7, 7, 10, 0),  # this morning normal hours
    ],
)
@freeze_time("2018-7-7 16:00:00")
def test_cancel_notification_for_service_updates_letter_if_still_time_to_cancel(
    admin_request,
    sample_letter_notification,
    mocker,
    created_at,
):
    mock_adjust_redis = mocker.patch("app.service.rest.adjust_daily_service_limits_for_cancelled_letters")
    sample_letter_notification.created_at = created_at

    response = admin_request.post(
        "service.cancel_notification_for_service",
        service_id=sample_letter_notification.service_id,
        notification_id=sample_letter_notification.id,
    )
    assert response["status"] == "cancelled"
    mock_adjust_redis.assert_called_once_with(
        sample_letter_notification.service_id, 1, sample_letter_notification.created_at
    )


def test_get_monthly_notification_data_by_service(sample_service, admin_request):
    create_ft_notification_status(
        date(2019, 4, 17), notification_type="letter", service=sample_service, notification_status="delivered"
    )
    create_ft_notification_status(
        date(2019, 3, 5), notification_type="email", service=sample_service, notification_status="sending", count=4
    )
    response = admin_request.get(
        "service.get_monthly_notification_data_by_service", start_date="2019-01-01", end_date="2019-06-17"
    )

    assert response == [
        ["2019-03-01", str(sample_service.id), "Sample service", "email", 4, 0, 0, 0, 0, 0],
        ["2019-04-01", str(sample_service.id), "Sample service", "letter", 0, 1, 0, 0, 0, 0],
    ]


@freeze_time("2019-12-11 13:30")
def test_get_returned_letter_statistics(admin_request, sample_service):
    create_returned_letter(sample_service, reported_at=datetime.utcnow() - timedelta(days=3))
    create_returned_letter(sample_service, reported_at=datetime.utcnow() - timedelta(days=2))
    create_returned_letter(sample_service, reported_at=datetime.utcnow() - timedelta(days=1))

    response = admin_request.get("service.returned_letter_statistics", service_id=sample_service.id)

    assert response == {"returned_letter_count": 3, "most_recent_report": "2019-12-10 00:00:00.000000"}


@freeze_time("2019-12-11 13:30")
def test_get_returned_letter_statistics_with_old_returned_letters(
    mocker,
    admin_request,
    sample_service,
):
    create_returned_letter(sample_service, reported_at=datetime.utcnow() - timedelta(days=8))
    create_returned_letter(sample_service, reported_at=datetime.utcnow() - timedelta(days=800))

    count_mock = mocker.patch(
        "app.service.rest.fetch_recent_returned_letter_count",
    )

    assert admin_request.get("service.returned_letter_statistics", service_id=sample_service.id) == {
        "returned_letter_count": 0,
        "most_recent_report": "2019-12-03 00:00:00.000000",
    }

    assert count_mock.called is False


def test_get_returned_letter_statistics_with_no_returned_letters(
    mocker,
    admin_request,
    sample_service,
):
    count_mock = mocker.patch(
        "app.service.rest.fetch_recent_returned_letter_count",
    )

    assert admin_request.get("service.returned_letter_statistics", service_id=sample_service.id) == {
        "returned_letter_count": 0,
        "most_recent_report": None,
    }

    assert count_mock.called is False


@freeze_time("2019-12-11 13:30")
def test_get_returned_letter_summary(admin_request, sample_service):
    create_returned_letter(sample_service, reported_at=datetime.utcnow() - timedelta(days=3))
    create_returned_letter(sample_service, reported_at=datetime.utcnow())
    create_returned_letter(sample_service, reported_at=datetime.utcnow())

    response = admin_request.get("service.returned_letter_summary", service_id=sample_service.id)

    assert len(response) == 2
    assert response[0] == {"returned_letter_count": 2, "reported_at": "2019-12-11"}
    assert response[1] == {"returned_letter_count": 1, "reported_at": "2019-12-08"}


@freeze_time("2019-12-11 13:30")
def test_get_returned_letter(admin_request, sample_letter_template):
    job = create_job(template=sample_letter_template)
    letter_from_job = create_notification(
        template=sample_letter_template,
        client_reference="letter_from_job",
        status=NOTIFICATION_RETURNED_LETTER,
        job=job,
        job_row_number=2,
        created_at=datetime.utcnow() - timedelta(days=1),
        created_by_id=sample_letter_template.service.users[0].id,
    )
    create_returned_letter(
        service=sample_letter_template.service, reported_at=datetime.utcnow(), notification_id=letter_from_job.id
    )

    one_off_letter = create_notification(
        template=sample_letter_template,
        status=NOTIFICATION_RETURNED_LETTER,
        created_at=datetime.utcnow() - timedelta(days=2),
        created_by_id=sample_letter_template.service.users[0].id,
    )
    create_returned_letter(
        service=sample_letter_template.service, reported_at=datetime.utcnow(), notification_id=one_off_letter.id
    )

    api_key = create_api_key(service=sample_letter_template.service)
    api_letter = create_notification(
        template=sample_letter_template,
        client_reference="api_letter",
        status=NOTIFICATION_RETURNED_LETTER,
        created_at=datetime.utcnow() - timedelta(days=3),
        api_key=api_key,
    )
    create_returned_letter(
        service=sample_letter_template.service, reported_at=datetime.utcnow(), notification_id=api_letter.id
    )

    precompiled_template = create_template(
        service=sample_letter_template.service, template_type="letter", hidden=True, template_name="hidden template"
    )
    precompiled_letter = create_notification_history(
        template=precompiled_template,
        api_key=api_key,
        client_reference="precompiled letter",
        created_at=datetime.utcnow() - timedelta(days=4),
    )
    create_returned_letter(
        service=sample_letter_template.service, reported_at=datetime.utcnow(), notification_id=precompiled_letter.id
    )

    uploaded_letter = create_notification_history(
        template=precompiled_template,
        client_reference="filename.pdf",
        created_at=datetime.utcnow() - timedelta(days=5),
        created_by_id=sample_letter_template.service.users[0].id,
    )
    create_returned_letter(
        service=sample_letter_template.service, reported_at=datetime.utcnow(), notification_id=uploaded_letter.id
    )

    not_included_in_results_template = create_template(
        service=create_service(service_name="not included in results"), template_type="letter"
    )
    letter_4 = create_notification_history(
        template=not_included_in_results_template, status=NOTIFICATION_RETURNED_LETTER
    )
    create_returned_letter(
        service=not_included_in_results_template.service, reported_at=datetime.utcnow(), notification_id=letter_4.id
    )
    response = admin_request.get(
        "service.get_returned_letters", service_id=sample_letter_template.service_id, reported_at="2019-12-11"
    )

    assert len(response) == 5
    assert response[0]["notification_id"] == str(letter_from_job.id)
    assert not response[0]["client_reference"]
    assert response[0]["reported_at"] == "2019-12-11"
    assert response[0]["created_at"] == "2019-12-10 13:30:00.000000"
    assert response[0]["template_name"] == sample_letter_template.name
    assert response[0]["template_id"] == str(sample_letter_template.id)
    assert response[0]["template_version"] == sample_letter_template.version
    assert response[0]["user_name"] == sample_letter_template.service.users[0].name
    assert response[0]["original_file_name"] == job.original_file_name
    assert response[0]["job_row_number"] == 3
    assert not response[0]["uploaded_letter_file_name"]

    assert response[1]["notification_id"] == str(one_off_letter.id)
    assert not response[1]["client_reference"]
    assert response[1]["reported_at"] == "2019-12-11"
    assert response[1]["created_at"] == "2019-12-09 13:30:00.000000"
    assert response[1]["template_name"] == sample_letter_template.name
    assert response[1]["template_id"] == str(sample_letter_template.id)
    assert response[1]["template_version"] == sample_letter_template.version
    assert response[1]["user_name"] == sample_letter_template.service.users[0].name
    assert not response[1]["original_file_name"]
    assert not response[1]["job_row_number"]
    assert not response[1]["uploaded_letter_file_name"]

    assert response[2]["notification_id"] == str(api_letter.id)
    assert response[2]["client_reference"] == "api_letter"
    assert response[2]["reported_at"] == "2019-12-11"
    assert response[2]["created_at"] == "2019-12-08 13:30:00.000000"
    assert response[2]["template_name"] == sample_letter_template.name
    assert response[2]["template_id"] == str(sample_letter_template.id)
    assert response[2]["template_version"] == sample_letter_template.version
    assert response[2]["user_name"] == "API"
    assert not response[2]["original_file_name"]
    assert not response[2]["job_row_number"]
    assert not response[2]["uploaded_letter_file_name"]

    assert response[3]["notification_id"] == str(precompiled_letter.id)
    assert response[3]["client_reference"] == "precompiled letter"
    assert response[3]["reported_at"] == "2019-12-11"
    assert response[3]["created_at"] == "2019-12-07 13:30:00.000000"
    assert not response[3]["template_name"]
    assert not response[3]["template_id"]
    assert not response[3]["template_version"]
    assert response[3]["user_name"] == "API"
    assert not response[3]["original_file_name"]
    assert not response[3]["job_row_number"]
    assert not response[3]["uploaded_letter_file_name"]

    assert response[4]["notification_id"] == str(uploaded_letter.id)
    assert not response[4]["client_reference"]
    assert response[4]["reported_at"] == "2019-12-11"
    assert response[4]["created_at"] == "2019-12-06 13:30:00.000000"
    assert not response[4]["template_name"]
    assert not response[4]["template_id"]
    assert not response[4]["template_version"]
    assert response[4]["user_name"] == sample_letter_template.service.users[0].name
    assert response[4]["email_address"] == sample_letter_template.service.users[0].email_address
    assert not response[4]["original_file_name"]
    assert not response[4]["job_row_number"]
    assert response[4]["uploaded_letter_file_name"] == "filename.pdf"


@freeze_time("2024-07-01 12:00")
def test_get_unsubscribe_request_report_summary_for_initial_unsubscribe_requests(admin_request, sample_service, mocker):
    """
    Test case covers when the initial unsubscribe requests have been received and have not yet been batched.
    """
    create_unsubscribe_request(sample_service, created_at=datetime.utcnow() - timedelta(days=2))
    create_unsubscribe_request(sample_service, created_at=datetime.utcnow() - timedelta(days=1))

    response = admin_request.get("service.get_unsubscribe_request_reports_summary", service_id=sample_service.id)

    assert response == [
        {
            "batch_id": None,
            "count": 2,
            "created_at": None,
            "earliest_timestamp": "2024-06-29T12:00:00.000000Z",
            "latest_timestamp": "2024-06-30T12:00:00.000000Z",
            "processed_by_service_at": None,
            "is_a_batched_report": False,
            "will_be_archived_at": "2024-09-26T23:00:00.000000Z",
            "service_id": str(sample_service.id),
        }
    ]


@freeze_time("2024-07-01 12:00")
def test_get_unsubscribe_request_reports_summary(admin_request, sample_service, mocker):
    # Create 2 unbatched unsubscribe requests
    create_unsubscribe_request(sample_service, created_at=datetime.utcnow() - timedelta(days=2))
    create_unsubscribe_request(sample_service, created_at=datetime.utcnow() - timedelta(days=1))

    # Create 2 unsubscribe_request_reports
    unsubscribe_request_report_1 = create_unsubscribe_request_report(
        sample_service,
        earliest_timestamp=datetime.utcnow() + timedelta(days=-8),
        latest_timestamp=datetime.utcnow() + timedelta(days=-7),
    )
    create_unsubscribe_request(
        sample_service,
        unsubscribe_request_report_id=unsubscribe_request_report_1.id,
    )

    unsubscribe_request_report_2 = create_unsubscribe_request_report(
        sample_service,
        earliest_timestamp=datetime.utcnow() + timedelta(days=-5),
        latest_timestamp=datetime.utcnow() + timedelta(days=-4),
        processed_by_service_at=datetime.utcnow() + timedelta(days=-3),
    )
    create_unsubscribe_request(
        sample_service,
        unsubscribe_request_report_id=unsubscribe_request_report_2.id,
    )

    expected_batched_unsubscribe_request_reports_summary = [
        {
            "batch_id": str(report.id),
            "count": report.count,
            "created_at": report.created_at.strftime(DATETIME_FORMAT),
            "earliest_timestamp": report.earliest_timestamp.strftime(DATETIME_FORMAT),
            "latest_timestamp": report.latest_timestamp.strftime(DATETIME_FORMAT),
            "processed_by_service_at": (
                report.processed_by_service_at.strftime(DATETIME_FORMAT) if report.processed_by_service_at else None
            ),
            "is_a_batched_report": True,
            "will_be_archived_at": report.will_be_archived_at.strftime(DATETIME_FORMAT),
            "service_id": str(sample_service.id),
        }
        for report in [unsubscribe_request_report_2, unsubscribe_request_report_1]
    ]
    expected_unbatched_unsubscribe_request_summary = {
        "batch_id": None,
        "count": 2,
        "created_at": None,
        "earliest_timestamp": "2024-06-29T12:00:00.000000Z",
        "latest_timestamp": "2024-06-30T12:00:00.000000Z",
        "processed_by_service_at": None,
        "is_a_batched_report": False,
        "will_be_archived_at": "2024-09-26T23:00:00.000000Z",
        "service_id": str(sample_service.id),
    }

    expected_reports_summary = [
        expected_unbatched_unsubscribe_request_summary
    ] + expected_batched_unsubscribe_request_reports_summary

    response = admin_request.get("service.get_unsubscribe_request_reports_summary", service_id=sample_service.id)

    assert response == expected_reports_summary


def test_get_unsubscribe_requests_statistics(admin_request, sample_service, mocker):
    MockUnsubscribeRequest = namedtuple(
        "MockUnsubscribeRequest",
        ["unsubscribe_requests_count", "service_id", "datetime_of_latest_unsubscribe_request"],
    )
    test_data = MockUnsubscribeRequest(0, "2fed1b45-66e1-4682-a389-85d0d50a916f", "Thu, 18 Jul 2024 15:32:28 GMT")

    mocker.patch("app.service.rest.get_unsubscribe_requests_statistics_dao", return_value=test_data)
    response = admin_request.get("service.get_unsubscribe_requests_statistics", service_id=sample_service.id)
    assert response["unsubscribe_requests_count"] == test_data.unsubscribe_requests_count
    assert response["datetime_of_latest_unsubscribe_request"] == test_data.datetime_of_latest_unsubscribe_request


def test_get_unsubscribe_requests_statistics_for_unsubscribe_requests_older_than_7_days(
    admin_request, sample_service, mocker
):
    MockUnsubscribeRequest = namedtuple(
        "MockUnsubscribeRequest",
        ["unsubscribe_requests_count", "service_id", "datetime_of_latest_unsubscribe_request"],
    )
    test_data = MockUnsubscribeRequest(0, "2fed1b45-66e1-4682-a389-85d0d50a916f", "Thu, 18 Jul 2024 15:32:28 GMT")

    mocker.patch("app.service.rest.get_unsubscribe_requests_statistics_dao", return_value=None)
    mocker.patch("app.service.rest.get_latest_unsubscribe_request_date_dao", return_value=test_data)
    response = admin_request.get("service.get_unsubscribe_requests_statistics", service_id=sample_service.id)
    assert response["unsubscribe_requests_count"] == 0
    assert response["datetime_of_latest_unsubscribe_request"] == test_data.datetime_of_latest_unsubscribe_request


def test_get_unsubscribe_requests_statistics_for_no_unsubscribe_requests(admin_request, sample_service, mocker):
    mocker.patch("app.service.rest.get_unsubscribe_requests_statistics_dao", return_value=None)
    mocker.patch("app.service.rest.get_latest_unsubscribe_request_date_dao", return_value=None)
    response = admin_request.get("service.get_unsubscribe_requests_statistics", service_id=sample_service.id)
    assert response == {}


@freeze_time("2024-07-17")
def test_process_unsubscribe_request_report(admin_request, sample_service):
    unsubscribe_request_report = create_unsubscribe_request_report(
        sample_service,
        earliest_timestamp=datetime.utcnow() + timedelta(days=-5),
        latest_timestamp=datetime.utcnow() + timedelta(days=-4),
    )
    admin_request.post(
        "service.process_unsubscribe_request_report",
        service_id=sample_service.id,
        batch_id=unsubscribe_request_report.id,
        _expected_status=204,
        _data={"report_has_been_processed": True},
    )
    updated_unsubscribe_request_report = UnsubscribeRequestReport.query.filter_by(
        id=unsubscribe_request_report.id
    ).one()
    assert updated_unsubscribe_request_report.id == unsubscribe_request_report.id
    assert updated_unsubscribe_request_report.processed_by_service_at == datetime.utcnow()


@freeze_time("2024-07-17")
def test_process_unsubscribe_request_report_set_processed_by_date_back_to_none(admin_request, sample_service):
    unsubscribe_request_report = create_unsubscribe_request_report(
        sample_service,
        earliest_timestamp=datetime.utcnow() + timedelta(days=-5),
        latest_timestamp=datetime.utcnow() + timedelta(days=-4),
        processed_by_service_at=datetime.utcnow() + timedelta(days=-2),
    )
    admin_request.post(
        "service.process_unsubscribe_request_report",
        service_id=sample_service.id,
        batch_id=unsubscribe_request_report.id,
        _expected_status=204,
        _data={"report_has_been_processed": False},
    )
    updated_unsubscribe_request_report = UnsubscribeRequestReport.query.filter_by(
        id=unsubscribe_request_report.id
    ).one()
    assert updated_unsubscribe_request_report.id == unsubscribe_request_report.id
    assert updated_unsubscribe_request_report.processed_by_service_at is None


def test_process_unsubscribe_request_report_raises_error_for_invalid_batch_id(admin_request, sample_service):
    random_batch_id = "258de158-07d3-457b-8eec-3e0e3bdab3bf"
    admin_request.post(
        "service.process_unsubscribe_request_report",
        service_id=sample_service.id,
        batch_id=random_batch_id,
        _expected_status=400,
        _data={"report_has_been_processed": False},
    )


def test_create_unsubscribe_request_report(sample_service, admin_request, mocker):
    test_id = "2802262c-b6ac-4254-93c3-a83ae7180d96"
    summary_data = {
        "batch_id": None,
        "count": 2,
        "earliest_timestamp": "2024-07-03T13:30:00+01:00",
        "latest_timestamp": "2024-07-09T21:13:11+01:00",
        "processed_by_service_at": None,
        "is_a_batched_report": False,
    }
    mocker.patch("app.service.rest.uuid.uuid4", return_value=test_id)
    mock_assign_unbatched_requests = mocker.patch(
        "app.service.rest.assign_unbatched_unsubscribe_requests_to_report_dao"
    )
    response = admin_request.post(
        "service.create_unsubscribe_request_report",
        service_id=sample_service.id,
        _data=summary_data,
        _expected_status=201,
    )
    created_unsubscribe_request_report = UnsubscribeRequestReport.query.filter_by(id=test_id).one()
    assert response == {"report_id": str(created_unsubscribe_request_report.id)}
    assert summary_data["count"] == created_unsubscribe_request_report.count
    assert created_unsubscribe_request_report.earliest_timestamp == datetime(2024, 7, 3, 13, 30)
    assert created_unsubscribe_request_report.latest_timestamp == datetime(2024, 7, 9, 21, 13, 11)
    assert created_unsubscribe_request_report.processed_by_service_at is None
    mock_assign_unbatched_requests.assert_called_once_with(
        report_id=created_unsubscribe_request_report.id,
        service_id=created_unsubscribe_request_report.service_id,
        earliest_timestamp=created_unsubscribe_request_report.earliest_timestamp,
        latest_timestamp=created_unsubscribe_request_report.latest_timestamp,
    )


def test_create_unsubscribe_request_report_raises_error_for_no_summary_data(sample_service, admin_request, mocker):
    admin_request.post(
        "service.create_unsubscribe_request_report", service_id=sample_service.id, _data=None, _expected_status=400
    )


def test_get_unsubscribe_request_report_for_download(admin_request, sample_service, mocker):
    # test_data
    UnsubscribeRequestReport = namedtuple(
        "UnsubscribeRequestReport", ["id", "earliest_timestamp", "latest_timestamp", "unsubscribe_requests"]
    )
    UnsubscribeRequest = namedtuple(
        "UnsubscribeRequest",
        ["email_address", "template_name", "original_file_name", "template_sent_at", "unsubscribe_request_received_at"],
    )

    unsubscribe_request_1 = UnsubscribeRequest(
        "foo@bar.com", "email Template Name", "contact list", "2024-07-23 13:30:00", "2024-07-25 13:30:00"
    )
    unsubscribe_request_2 = UnsubscribeRequest(
        "fizz@bar.com", "email Template Name", "contact list", "2024-07-21 11:04:00", "2024-07-23 11:04:00"
    )
    unsubscribe_request_3 = UnsubscribeRequest(
        "fizzbuzz@bar.com", "Another Service", None, "2024-07-19 23:45:00", "2024-07-21 23:45:00"
    )
    unsubscribe_request_4 = UnsubscribeRequest(
        "buzz@bar.com", "Another Service", "another contact list", "2024-07-17 09:42:00", "2024-07-19 09:42:00"
    )
    unsubscribe_request_report = UnsubscribeRequestReport(
        "e6c02a98-8e64-4ab3-b176-271274517c21",
        "2024-07-17 09:42:00",
        "2024-07-23 13:30:00",
        [unsubscribe_request_1, unsubscribe_request_2, unsubscribe_request_3, unsubscribe_request_4],
    )
    mocker.patch("app.service.rest.get_unsubscribe_request_report_by_id_dao", return_value=unsubscribe_request_report)
    mocker.patch(
        "app.service.rest.get_unsubscribe_requests_data_for_download_dao",
        return_value=unsubscribe_request_report.unsubscribe_requests,
    )
    response = admin_request.get(
        "service.get_unsubscribe_request_report_for_download",
        service_id=sample_service.id,
        batch_id=unsubscribe_request_report.id,
    )

    assert response["batch_id"] == unsubscribe_request_report.id
    assert response["earliest_timestamp"] == unsubscribe_request_report.earliest_timestamp
    assert response["latest_timestamp"] == unsubscribe_request_report.latest_timestamp
    assert response["unsubscribe_requests"] == [
        {
            "email_address": "foo@bar.com",
            "original_file_name": "contact list",
            "template_name": "email Template Name",
            "template_sent_at": "2024-07-23 14:30:00",
            "unsubscribe_request_received_at": "2024-07-25 14:30:00",
        },
        {
            "email_address": "fizz@bar.com",
            "original_file_name": "contact list",
            "template_name": "email Template Name",
            "template_sent_at": "2024-07-21 12:04:00",
            "unsubscribe_request_received_at": "2024-07-23 12:04:00",
        },
        {
            "email_address": "fizzbuzz@bar.com",
            "original_file_name": None,
            "template_name": "Another Service",
            "template_sent_at": "2024-07-20 00:45:00",
            "unsubscribe_request_received_at": "2024-07-22 00:45:00",
        },
        {
            "email_address": "buzz@bar.com",
            "original_file_name": "another contact list",
            "template_name": "Another Service",
            "template_sent_at": "2024-07-17 10:42:00",
            "unsubscribe_request_received_at": "2024-07-19 10:42:00",
        },
    ]


def test_get_unsubscribe_request_report_for_download_400_error(admin_request, sample_service):
    invalid_batch_id = "c92de771-32a0-49ec-b398-75b1308c7142"
    admin_request.get(
        "service.get_unsubscribe_request_report_for_download",
        service_id=sample_service.id,
        batch_id=invalid_batch_id,
        _expected_status=400,
    )


@pytest.mark.parametrize(
    "new_custom_email_sender_name, expected_email_sender_local_part",
    [
        ("some other name", "some.other.name"),
        # clearing custom_email_sender_name sets local part back to the normalised service name
        (None, "sample.service"),
    ],
)
def test_update_service_set_custom_email_sender_name_sets_email_sender_local_part(
    sample_user,
    admin_request,
    new_custom_email_sender_name,
    expected_email_sender_local_part,
):
    # set columns directly to avoid hitting the hybrid properties
    service = Service(
        name="Sample service",
        _custom_email_sender_name="existing name",
        _email_sender_local_part="existing.name",
        restricted=True,
        created_by=sample_user,
    )
    from app.dao.services_dao import dao_update_service

    # use the dao method as it creates a proper history version
    dao_update_service(service)

    admin_request.post(
        "service.update_service",
        service_id=service.id,
        _data={"custom_email_sender_name": new_custom_email_sender_name},
    )

    assert service.name == "Sample service"
    assert service.custom_email_sender_name == new_custom_email_sender_name
    assert service.email_sender_local_part == expected_email_sender_local_part

    ServiceHistory = Service.get_history_model()
    prev_history, new_history = ServiceHistory.query.filter_by(id=service.id).order_by(ServiceHistory.version)
    assert prev_history.version == 2
    assert prev_history.email_sender_local_part == "existing.name"

    assert new_history.version == 3
    assert new_history.email_sender_local_part == expected_email_sender_local_part


CountNotificationsTestCase = namedtuple(
    "CountNotificationsTestCase",
    ["payload", "expected_status_code", "expected_response", "create_notifications"],
)

test_cases = [
    CountNotificationsTestCase(
        payload={"template_type": "sms", "limit_days": 7},
        expected_status_code=200,
        expected_response={"notifications_sent_count": 1},
        create_notifications=True,
    ),
    CountNotificationsTestCase(
        payload={"template_type": "email", "limit_days": 7},
        expected_status_code=200,
        expected_response={"notifications_sent_count": 1},
        create_notifications=True,
    ),
    CountNotificationsTestCase(
        payload={},
        expected_status_code=200,
        expected_response={"notifications_sent_count": 3},
        create_notifications=True,
    ),
    CountNotificationsTestCase(
        payload={"template_type": "sms", "limit_days": 7},
        expected_status_code=200,
        expected_response={"notifications_sent_count": 0},
        create_notifications=False,
    ),
]


@pytest.mark.parametrize(
    "test_case",
    test_cases,
    ids=[
        "Single SMS notification within limit days",
        "Single Email notification within limit days",
        "No template_type or limit_days provided (defaults applied)",
        "No notifications",
    ],
)
@freeze_time("2023-08-10")
def test_count_notifications_for_service(admin_request, sample_template, test_case):
    service = create_service(service_name="service_1")

    if test_case.create_notifications:
        for template_type in NOTIFICATION_TYPES:
            create_ft_billing(
                bst_date=datetime.now().date(),
                template=create_template(service, template_type),
            )

    resp = admin_request.get(
        "service.count_notifications_for_service",
        service_id=service.id,
        _expected_status=test_case.expected_status_code,
        **test_case.payload,
    )

    assert resp == test_case.expected_response


ServiceJoinRequestTestCase = namedtuple(
    "TestCase",
    [
        "requester_id",
        "service_id",
        "contacted_user_ids",
        "expected_status_code",
        "expected_response",
    ],
)


@pytest.mark.parametrize(
    "test_case",
    [
        ServiceJoinRequestTestCase(
            requester_id=uuid.uuid4(),
            service_id=uuid.uuid4(),
            contacted_user_ids=[],
            expected_status_code=400,
            expected_response="contacted_user_ids [] should be non-empty",
        ),
        ServiceJoinRequestTestCase(
            requester_id=None,
            service_id=uuid.uuid4(),
            contacted_user_ids=[str(uuid.uuid4())],
            expected_status_code=400,
            expected_response="requester_id badly formed hexadecimal UUID string",
        ),
        ServiceJoinRequestTestCase(
            requester_id=uuid.uuid4(),
            service_id=uuid.uuid4(),
            contacted_user_ids=[str(uuid.uuid4())],
            expected_status_code=201,
            expected_response={"service_join_request_id": "some-uuid-here"},
        ),
    ],
    ids=[
        "Validation Error - No contacts",
        "Validation Error - Invalid Type for Requester ID (None)",
        "Created Request Successfully",
    ],
)
def test_create_service_join_request(
    admin_request,
    test_case,
):
    setup_service_join_request_test_data(test_case.service_id, test_case.requester_id, test_case.contacted_user_ids)

    resp = admin_request.post(
        "service.create_service_join_request",
        service_id=str(test_case.service_id),
        _data={"requester_id": str(test_case.requester_id), "contacted_user_ids": test_case.contacted_user_ids},
        _expected_status=test_case.expected_status_code,
    )

    if test_case.expected_status_code == 400:
        assert resp["errors"][0]["message"] == test_case.expected_response
    elif test_case.expected_status_code == 201:
        assert "service_join_request_id" in resp


def test_get_service_join_request_not_found(admin_request):
    request_id = uuid.uuid4()

    resp = admin_request.get(
        "service.get_service_join_request",
        request_id=request_id,
        _expected_status=404,
    )

    assert resp["message"] == f"Service join request with ID {request_id} not found."


def test_get_service_join_request_success(admin_request):
    requester_id = uuid.uuid4()
    service_id = uuid.uuid4()
    contacted_user = uuid.uuid4()

    setup_service_join_request_test_data(service_id, requester_id, [contacted_user])

    resp = admin_request.post(
        "service.create_service_join_request",
        service_id=str(service_id),
        _data={"requester_id": str(requester_id), "contacted_user_ids": [str(contacted_user)]},
        _expected_status=201,
    )

    request_id = resp["service_join_request_id"]

    resp = admin_request.get(
        "service.get_service_join_request",
        request_id=request_id,
        _expected_status=200,
    )

    assert resp["contacted_service_users"] is not None
    assert resp["status"] == SERVICE_JOIN_REQUEST_PENDING
    assert resp["id"] == request_id
    assert resp["created_at"] is not None


@pytest.mark.parametrize(
    "status_changed_by_id, permissions, status, reason, expected_error",
    [
        (
            uuid.uuid4(),
            ["invalid_permission"],
            "approved",
            None,
            "permissions invalid_permission is not one of "
            "[manage_users, "
            "manage_templates, "
            "manage_settings, "
            "send_texts, send_emails, "
            "send_letters, "
            "manage_api_keys, "
            "view_activity]",
        ),
        (
            uuid.uuid4(),
            ["manage_users"],
            "invalid_status",
            None,
            "status invalid_status is not one of [pending, approved, rejected, cancelled]",
        ),
        (
            None,
            ["manage_users"],
            "approved",
            None,
            "status_changed_by_id badly formed hexadecimal UUID string",
        ),
    ],
)
def test_update_service_join_request_validation_errors(
    admin_request, status_changed_by_id, permissions, status, reason, expected_error
):
    resp = admin_request.post(
        "service.update_service_join_request",
        request_id=str(uuid.uuid4()),
        _data={
            "permissions": permissions,
            "status_changed_by_id": str(status_changed_by_id),
            "status": status,
            "reason": reason,
        },
        _expected_status=400,
    )

    assert resp["errors"][0]["message"] == expected_error


@pytest.mark.parametrize(
    "status_changed_by_id, permissions, status, reason",
    [
        (uuid.uuid4(), ["manage_users"], "approved", None),
        (uuid.uuid4(), [], "approved", None),
        (uuid.uuid4(), ["manage_users"], "rejected", "user is not part of company anymore"),
    ],
)
def test_update_service_join_request_updates_service_join_request_table(
    admin_request,
    status_changed_by_id,
    permissions,
    status,
    reason,
    notify_service,
    service_join_request_approved_template,
    mocker,
):
    requester_id = uuid.uuid4()
    service_id = uuid.uuid4()
    user_id = status_changed_by_id

    setup_service_join_request_test_data(service_id, requester_id, [user_id])
    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")

    request = dao_create_service_join_request(
        requester_id=requester_id,
        service_id=service_id,
        contacted_user_ids=[user_id],
    )

    resp = admin_request.post(
        "service.update_service_join_request",
        request_id=str(request.id),
        _data={
            "permissions": permissions,
            "status_changed_by_id": str(status_changed_by_id),
            "status": status,
            "reason": reason,
        },
        _expected_status=200,
    )

    assert resp["status"] == status
    assert resp["reason"] == reason
    assert resp["status_changed_by"]["id"] == str(status_changed_by_id)


def test_update_service_join_request_request_not_found(admin_request):
    requester_id = uuid.uuid4()
    service_id = uuid.uuid4()
    user_id = uuid.uuid4()

    setup_service_join_request_test_data(service_id, requester_id, [user_id])

    resp = admin_request.post(
        "service.update_service_join_request",
        request_id=str(uuid.uuid4()),
        _data={
            "permissions": ["manage_users"],
            "status_changed_by_id": str(user_id),
            "status": "approved",
            "reason": None,
        },
        _expected_status=404,
    )

    assert resp["message"] == "Service join request not found"


@pytest.mark.parametrize(
    "status_changed_by_id, set_permissions, with_folder_permissions, status, reason",
    [
        (uuid.uuid4(), ["manage_users"], False, "approved", None),
        (uuid.uuid4(), [], True, "approved", None),
    ],
)
def test_update_service_join_request_add_user_to_service(
    admin_request,
    status_changed_by_id,
    set_permissions,
    with_folder_permissions,
    status,
    reason,
    notify_service,
    service_join_request_approved_template,
    mocker,
):
    requester_id = uuid.uuid4()
    service_id = uuid.uuid4()
    user_id = status_changed_by_id
    folder_permissions = []
    folder_1 = None
    folder_2 = None

    setup_service_join_request_test_data(service_id, requester_id, [user_id])
    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")

    request = dao_create_service_join_request(
        requester_id=requester_id,
        service_id=service_id,
        contacted_user_ids=[user_id],
    )

    if with_folder_permissions:
        service = dao_fetch_service_by_id(service_id)
        folder_1 = create_template_folder(service)
        folder_2 = create_template_folder(service)
        folder_permissions = [str(folder_1.id), str(folder_2.id)]

    admin_request.post(
        "service.update_service_join_request",
        request_id=str(request.id),
        _data={
            "permissions": set_permissions,
            "folder_permissions": folder_permissions,
            "status_changed_by_id": str(status_changed_by_id),
            "status": status,
            "reason": reason,
            "auth_type": "sms_auth",
        },
        _expected_status=200,
    )

    user = get_user_by_id(requester_id)

    assert user is not None
    assert str(service_id) in [str(service.id) for service in user.services]

    if set_permissions:
        assert user.get_permissions() == {str(service_id): set_permissions}

    if with_folder_permissions:
        service_user = dao_get_service_user(user_id=requester_id, service_id=service_id)
        assert len(service_user.folders) == 2
        assert folder_1 in service_user.folders
        assert folder_2 in service_user.folders


@pytest.mark.parametrize(
    "current_auth_type, mobile_no, selected_auth_type, expected_auth_type",
    [
        ("sms_auth", "07700900986", "sms_auth", "sms_auth"),
        ("sms_auth", "07700900986", "email_auth", "email_auth"),
        ("email_auth", "07700900986", "sms_auth", "sms_auth"),
        ("email_auth", "07700900986", "email_auth", "email_auth"),
        ("email_auth", None, "sms_auth", "email_auth"),
        ("email_auth", None, "email_auth", "email_auth"),
    ],
)
def test_update_service_join_request_updates_user_auth_type(
    admin_request,
    sample_user_service_permission,
    notify_service,
    service_join_request_approved_template,
    current_auth_type,
    mobile_no,
    selected_auth_type,
    expected_auth_type,
    mocker,
):
    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")

    requester = create_user(mobile_number=mobile_no, auth_type=current_auth_type)

    request = dao_create_service_join_request(
        requester_id=requester.id,
        service_id=sample_user_service_permission.service.id,
        contacted_user_ids=[sample_user_service_permission.id],
    )

    admin_request.post(
        "service.update_service_join_request",
        request_id=str(request.id),
        _data={
            "permissions": ["manage_users"],
            "folder_permissions": [],
            "status_changed_by_id": str(sample_user_service_permission.user.id),
            "status": "approved",
            "reason": None,
            "auth_type": selected_auth_type,
        },
    )

    assert requester.auth_type == expected_auth_type


def test_update_service_join_request_notification_sent(
    admin_request,
    notify_service,
    service_join_request_approved_template,
    mocker,
):
    requester_id = uuid.uuid4()
    service_id = uuid.uuid4()
    approver_id = uuid.uuid4()

    setup_service_join_request_test_data(service_id, requester_id, [approver_id])
    mocked = mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")

    request = dao_create_service_join_request(
        requester_id=requester_id,
        service_id=service_id,
        contacted_user_ids=[approver_id],
    )

    admin_request.post(
        "service.update_service_join_request",
        request_id=str(request.id),
        _data={
            "permissions": ["manage_users"],
            "status_changed_by_id": str(approver_id),
            "status": "approved",
            "auth_type": "email_auth",
        },
        _expected_status=200,
    )

    notification = Notification.query.first()
    mocked.assert_called_once_with(([str(notification.id)]), queue="notify-internal-tasks")

    assert notification.reply_to_text == notify_service.get_default_reply_to_email_address()
    assert notification.to == f"{requester_id}@digital.cabinet-office.gov.uk"

    assert notification.personalisation["requester_name"] == "Requester User"
    assert notification.personalisation["approver_name"] == f"User Within Existing Service {approver_id}"
    assert notification.personalisation["service_name"] == f"Service Requester Wants To Join {service_id}"
    assert f"/services/{service_id}" in notification.personalisation["dashboard_url"]


def test_update_service_join_request_get_template(
    admin_request,
    notify_service,
    service_join_request_approved_template,
    mocker,
):
    template = service_join_request_approved_template
    mocked = mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")
    mocker.patch("app.dao.templates_dao.dao_get_template_by_id", return_value=template)

    requester_id = uuid.uuid4()
    service_id = uuid.uuid4()
    approver_id = uuid.uuid4()

    setup_service_join_request_test_data(service_id, requester_id, [approver_id])

    request = dao_create_service_join_request(
        requester_id=requester_id,
        service_id=service_id,
        contacted_user_ids=[approver_id],
    )

    admin_request.post(
        "service.update_service_join_request",
        request_id=str(request.id),
        _data={
            "permissions": ["manage_users"],
            "status_changed_by_id": str(approver_id),
            "status": "approved",
        },
        _expected_status=200,
    )

    notification = Notification.query.first()
    mocked.assert_called_once_with(([str(notification.id)]), queue="notify-internal-tasks")

    assert notification.template.version == template.version
    assert notification.template_id == template.id


def test_update_service_join_request_cancels_pending_requests(
    admin_request,
    notify_service,
    service_join_request_approved_template,
    mocker,
):
    requester_id = uuid.uuid4()
    service_id = uuid.uuid4()
    approver_id = uuid.uuid4()

    setup_service_join_request_test_data(service_id, requester_id, [approver_id])
    mocker.patch("app.celery.provider_tasks.deliver_email.apply_async")

    pending_request_1 = create_service_join_request(
        requester_id=requester_id, service_id=service_id, contacted_user_ids=[approver_id]
    )

    pending_request_2 = create_service_join_request(
        requester_id=requester_id, service_id=service_id, contacted_user_ids=[approver_id]
    )

    admin_request.post(
        "service.update_service_join_request",
        request_id=str(pending_request_2.id),
        _data={
            "permissions": ["manage_users"],
            "status_changed_by_id": str(approver_id),
            "status": "approved",
            "auth_type": "sms_auth",
        },
        _expected_status=200,
    )

    all_user_requests = ServiceJoinRequest.query.filter_by(requester_id=requester_id, service_id=service_id).all()

    assert len(all_user_requests) == 2

    request_status_map = {request.id: request.status for request in all_user_requests}

    assert request_status_map[pending_request_2.id] == SERVICE_JOIN_REQUEST_APPROVED
    assert request_status_map[pending_request_1.id] == SERVICE_JOIN_REQUEST_CANCELLED
