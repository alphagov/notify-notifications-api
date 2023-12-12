import base64
import json
import random
import string
import uuid
from datetime import datetime, timedelta

import botocore
import pytest
import requests_mock
from freezegun import freeze_time
from notifications_utils import SMS_CHAR_COUNT_LIMIT
from pypdf.errors import PdfReadError

from app.constants import EMAIL_TYPE, LETTER_TYPE, SMS_TYPE
from app.dao.templates_dao import (
    dao_get_template_by_id,
    dao_get_template_versions,
    dao_redact_template,
    dao_update_template,
)
from app.models import Template, TemplateHistory
from tests import create_admin_authorization_header
from tests.app.db import (
    create_letter_attachment,
    create_letter_contact,
    create_notification,
    create_service,
    create_template,
    create_template_folder,
)
from tests.conftest import set_config_values


@pytest.mark.parametrize(
    "template_type, subject",
    [
        (SMS_TYPE, None),
        (EMAIL_TYPE, "subject"),
        (LETTER_TYPE, "subject"),
    ],
)
def test_should_create_a_new_template_for_a_service(client, sample_user, template_type, subject):
    service = create_service(service_permissions=[template_type])
    data = {
        "name": "my template",
        "template_type": template_type,
        "content": "template <b>content</b>",
        "service": str(service.id),
        "created_by": str(sample_user.id),
    }
    if subject:
        data.update({"subject": subject})
    if template_type == LETTER_TYPE:
        data.update({"postage": "first"})
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    assert response.status_code == 201
    json_resp = json.loads(response.get_data(as_text=True))
    assert json_resp["data"]["name"] == "my template"
    assert json_resp["data"]["template_type"] == template_type
    assert json_resp["data"]["content"] == "template <b>content</b>"
    assert json_resp["data"]["service"] == str(service.id)
    assert json_resp["data"]["id"]
    assert json_resp["data"]["version"] == 1
    assert json_resp["data"]["process_type"] == "normal"
    assert json_resp["data"]["created_by"] == str(sample_user.id)
    if subject:
        assert json_resp["data"]["subject"] == "subject"
    else:
        assert not json_resp["data"]["subject"]

    if template_type == LETTER_TYPE:
        assert json_resp["data"]["postage"] == "first"
    else:
        assert not json_resp["data"]["postage"]

    template = Template.query.get(json_resp["data"]["id"])
    from app.schemas import template_schema

    assert sorted(json_resp["data"]) == sorted(template_schema.dump(template))


def test_create_a_new_template_for_a_service_adds_folder_relationship(client, sample_service):
    parent_folder = create_template_folder(service=sample_service, name="parent folder")

    data = {
        "name": "my template",
        "template_type": "sms",
        "content": "template <b>content</b>",
        "service": str(sample_service.id),
        "created_by": str(sample_service.users[0].id),
        "parent_folder_id": str(parent_folder.id),
    }
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{sample_service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    assert response.status_code == 201
    template = Template.query.filter(Template.name == "my template").first()
    assert template.folder == parent_folder


@pytest.mark.parametrize(
    "template_type, expected_postage", [(SMS_TYPE, None), (EMAIL_TYPE, None), (LETTER_TYPE, "second")]
)
def test_create_a_new_template_for_a_service_adds_postage_for_letters_only(
    client, sample_service, template_type, expected_postage
):
    data = {
        "name": "my template",
        "template_type": template_type,
        "content": "template <b>content</b>",
        "service": str(sample_service.id),
        "created_by": str(sample_service.users[0].id),
    }
    if template_type in [EMAIL_TYPE, LETTER_TYPE]:
        data["subject"] = "Hi, I have good news"

    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{sample_service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    assert response.status_code == 201
    template = Template.query.filter(Template.name == "my template").first()
    assert template.postage == expected_postage


def test_create_template_should_return_400_if_folder_is_for_a_different_service(client, sample_service):
    service2 = create_service(service_name="second service")
    parent_folder = create_template_folder(service=service2)

    data = {
        "name": "my template",
        "template_type": "sms",
        "content": "template <b>content</b>",
        "service": str(sample_service.id),
        "created_by": str(sample_service.users[0].id),
        "parent_folder_id": str(parent_folder.id),
    }
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{sample_service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    assert response.status_code == 400
    assert json.loads(response.get_data(as_text=True))["message"] == "parent_folder_id not found"


def test_create_template_should_return_400_if_folder_does_not_exist(client, sample_service):
    data = {
        "name": "my template",
        "template_type": "sms",
        "content": "template <b>content</b>",
        "service": str(sample_service.id),
        "created_by": str(sample_service.users[0].id),
        "parent_folder_id": str(uuid.uuid4()),
    }
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{sample_service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    assert response.status_code == 400
    assert json.loads(response.get_data(as_text=True))["message"] == "parent_folder_id not found"


def test_should_raise_error_if_service_does_not_exist_on_create(client, sample_user, fake_uuid):
    data = {
        "name": "my template",
        "template_type": SMS_TYPE,
        "content": "template content",
        "service": fake_uuid,
        "created_by": str(sample_user.id),
    }
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{fake_uuid}/template", headers=[("Content-Type", "application/json"), auth_header], data=data
    )
    json_resp = json.loads(response.get_data(as_text=True))
    assert response.status_code == 404
    assert json_resp["result"] == "error"
    assert json_resp["message"] == "No result found"


@pytest.mark.parametrize(
    "permissions, template_type, subject, expected_error",
    [
        ([EMAIL_TYPE], SMS_TYPE, None, {"template_type": ["Creating text message templates is not allowed"]}),
        ([SMS_TYPE], EMAIL_TYPE, "subject", {"template_type": ["Creating email templates is not allowed"]}),
        ([SMS_TYPE], LETTER_TYPE, "subject", {"template_type": ["Creating letter templates is not allowed"]}),
    ],
)
def test_should_raise_error_on_create_if_no_permission(
    client, sample_user, permissions, template_type, subject, expected_error
):
    service = create_service(service_permissions=permissions)
    data = {
        "name": "my template",
        "template_type": template_type,
        "content": "template content",
        "service": str(service.id),
        "created_by": str(sample_user.id),
    }
    if subject:
        data.update({"subject": subject})

    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    json_resp = json.loads(response.get_data(as_text=True))
    assert response.status_code == 403
    assert json_resp["result"] == "error"
    assert json_resp["message"] == expected_error


@pytest.mark.parametrize(
    "template_type, permissions, expected_error",
    [
        (SMS_TYPE, [EMAIL_TYPE], {"template_type": ["Updating text message templates is not allowed"]}),
        (EMAIL_TYPE, [LETTER_TYPE], {"template_type": ["Updating email templates is not allowed"]}),
        (LETTER_TYPE, [SMS_TYPE], {"template_type": ["Updating letter templates is not allowed"]}),
    ],
)
def test_should_be_error_on_update_if_no_permission(
    client,
    sample_user,
    notify_db_session,
    template_type,
    permissions,
    expected_error,
):
    service = create_service(service_permissions=permissions)
    template_without_permission = create_template(service, template_type=template_type)
    data = {"content": "new template content", "created_by": str(sample_user.id)}

    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    update_response = client.post(
        f"/service/{template_without_permission.service_id}/template/{template_without_permission.id}",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )

    json_resp = json.loads(update_response.get_data(as_text=True))
    assert update_response.status_code == 403
    assert json_resp["result"] == "error"
    assert json_resp["message"] == expected_error


def test_should_error_if_created_by_missing(client, sample_service):
    service_id = str(sample_service.id)
    data = {"name": "my template", "template_type": SMS_TYPE, "content": "template content", "service": service_id}
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{service_id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    json_resp = json.loads(response.get_data(as_text=True))
    assert response.status_code == 400
    assert json_resp["errors"][0]["error"] == "ValidationError"
    assert json_resp["errors"][0]["message"] == "created_by is a required property"


def test_should_be_error_if_service_does_not_exist_on_update(client, fake_uuid):
    data = {"name": "my template"}
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{fake_uuid}/template/{fake_uuid}",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    json_resp = json.loads(response.get_data(as_text=True))
    assert response.status_code == 404
    assert json_resp["result"] == "error"
    assert json_resp["message"] == "No result found"


@pytest.mark.parametrize("template_type", [EMAIL_TYPE, LETTER_TYPE])
def test_must_have_a_subject_on_an_email_or_letter_template(client, sample_user, sample_service, template_type):
    data = {
        "name": "my template",
        "template_type": template_type,
        "content": "template content",
        "service": str(sample_service.id),
        "created_by": str(sample_user.id),
    }
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{sample_service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    json_resp = json.loads(response.get_data(as_text=True))
    assert json_resp["errors"][0]["error"] == "ValidationError"
    assert json_resp["errors"][0]["message"] == "subject is a required property"


def test_update_should_update_a_template(client, sample_user):
    service = create_service(service_permissions=[LETTER_TYPE])
    template = create_template(service, template_type="letter", postage="second")

    assert template.created_by == service.created_by
    assert template.created_by != sample_user

    data = {"content": "my template has new content, swell!", "created_by": str(sample_user.id), "postage": "first"}
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    update_response = client.post(
        f"/service/{service.id}/template/{template.id}",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )

    assert update_response.status_code == 200
    update_json_resp = json.loads(update_response.get_data(as_text=True))
    assert update_json_resp["data"]["content"] == ("my template has new content, swell!")
    assert update_json_resp["data"]["postage"] == "first"
    assert update_json_resp["data"]["name"] == template.name
    assert update_json_resp["data"]["template_type"] == template.template_type
    assert update_json_resp["data"]["version"] == 2

    assert update_json_resp["data"]["created_by"] == str(sample_user.id)
    template_created_by_users = [template.created_by_id for template in TemplateHistory.query.all()]
    assert len(template_created_by_users) == 2
    assert service.created_by.id in template_created_by_users
    assert sample_user.id in template_created_by_users


@pytest.mark.parametrize(
    "post_data",
    (
        {},
        {"letter_welsh_subject": "", "letter_welsh_content": ""},
        {"letter_welsh_subject": None, "letter_welsh_content": None},
        pytest.param(
            {"letter_welsh_subject": "", "letter_welsh_content": None},
            marks=pytest.mark.xfail(
                raises=AssertionError,
                reason=(
                    "if `letter_languages` is not present, `letter_welsh_subject` and `letter_welsh_content` "
                    "data types must match (either null or string)"
                ),
            ),
        ),
        {
            "letter_languages": "welsh_then_english",
            "letter_welsh_subject": "subject",
            "letter_welsh_content": "content",
        },
        pytest.param(
            {"letter_languages": "welsh_then_english", "letter_welsh_subject": None, "letter_welsh_content": None},
            marks=pytest.mark.xfail(
                raises=AssertionError, reason="if welsh_then_english, welsh subject and content must be provided"
            ),
        ),
        {"letter_languages": "english", "letter_welsh_subject": None, "letter_welsh_content": None},
        pytest.param(
            {"letter_languages": "english", "letter_welsh_subject": "subject", "letter_welsh_content": "content"},
            marks=pytest.mark.xfail(raises=AssertionError, reason="if english, then welsh data must be nulled out"),
        ),
    ),
)
def test_update_template_language(client, sample_user, post_data):
    service = create_service(service_permissions=[LETTER_TYPE])
    template = create_template(service, template_type="letter", postage="second")

    assert template.created_by == service.created_by
    assert template.created_by != sample_user

    data = json.dumps(post_data)
    auth_header = create_admin_authorization_header()

    update_response = client.post(
        f"/service/{service.id}/template/{template.id}",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )

    assert update_response.status_code == 200
    update_json_resp = json.loads(update_response.get_data(as_text=True))
    assert update_json_resp["data"]["letter_languages"] == post_data.get("letter_languages", "english")
    assert update_json_resp["data"]["letter_welsh_subject"] == post_data.get("letter_welsh_subject", None)
    assert update_json_resp["data"]["letter_welsh_content"] == post_data.get("letter_welsh_content", None)


def test_should_be_able_to_archive_template(client, sample_template):
    data = {
        "name": sample_template.name,
        "template_type": sample_template.template_type,
        "content": sample_template.content,
        "archived": True,
        "service": str(sample_template.service.id),
        "created_by": str(sample_template.created_by.id),
    }

    json_data = json.dumps(data)

    auth_header = create_admin_authorization_header()

    resp = client.post(
        f"/service/{sample_template.service.id}/template/{sample_template.id}",
        headers=[("Content-Type", "application/json"), auth_header],
        data=json_data,
    )

    assert resp.status_code == 200
    assert Template.query.first().archived


def test_should_be_able_to_archive_template_should_remove_template_folders(client, sample_service):
    template_folder = create_template_folder(service=sample_service)
    template = create_template(service=sample_service, folder=template_folder)

    data = {
        "archived": True,
    }

    client.post(
        f"/service/{sample_service.id}/template/{template.id}",
        headers=[("Content-Type", "application/json"), create_admin_authorization_header()],
        data=json.dumps(data),
    )

    updated_template = Template.query.get(template.id)
    assert updated_template.archived
    assert not updated_template.folder


def test_get_precompiled_template_for_service(
    client,
    notify_user,
    sample_service,
):
    assert len(sample_service.templates) == 0

    response = client.get(
        f"/service/{sample_service.id}/template/precompiled",
        headers=[create_admin_authorization_header()],
    )
    assert response.status_code == 200
    assert len(sample_service.templates) == 1

    data = json.loads(response.get_data(as_text=True))
    assert data["name"] == "Pre-compiled PDF"
    assert data["hidden"] is True
    assert data["is_precompiled_letter"] is True


def test_get_precompiled_template_for_service_when_service_has_existing_precompiled_template(
    client,
    notify_user,
    sample_service,
):
    create_template(
        sample_service, template_name="Exisiting precompiled template", template_type=LETTER_TYPE, hidden=True
    )
    assert len(sample_service.templates) == 1

    response = client.get(
        f"/service/{sample_service.id}/template/precompiled",
        headers=[create_admin_authorization_header()],
    )

    assert response.status_code == 200
    assert len(sample_service.templates) == 1

    data = json.loads(response.get_data(as_text=True))
    assert data["name"] == "Exisiting precompiled template"
    assert data["hidden"] is True


def test_should_be_able_to_get_all_templates_for_a_service(client, sample_user, sample_service):
    data = {
        "name": "my template 1",
        "template_type": EMAIL_TYPE,
        "subject": "subject 1",
        "content": "template content",
        "service": str(sample_service.id),
        "created_by": str(sample_user.id),
    }
    data_1 = json.dumps(data)
    data = {
        "name": "my template 2",
        "template_type": EMAIL_TYPE,
        "subject": "subject 2",
        "content": "template content",
        "service": str(sample_service.id),
        "created_by": str(sample_user.id),
    }
    data_2 = json.dumps(data)
    auth_header = create_admin_authorization_header()
    client.post(
        f"/service/{sample_service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data_1,
    )
    auth_header = create_admin_authorization_header()

    client.post(
        f"/service/{sample_service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data_2,
    )

    auth_header = create_admin_authorization_header()

    response = client.get(f"/service/{sample_service.id}/template", headers=[auth_header])

    assert response.status_code == 200
    update_json_resp = json.loads(response.get_data(as_text=True))
    assert update_json_resp["data"][0]["name"] == "my template 1"
    assert update_json_resp["data"][0]["version"] == 1
    assert update_json_resp["data"][0]["created_at"]
    assert update_json_resp["data"][1]["name"] == "my template 2"
    assert update_json_resp["data"][1]["version"] == 1
    assert update_json_resp["data"][1]["created_at"]


def test_should_get_only_templates_for_that_service(admin_request, notify_db_session):
    service_1 = create_service(service_name="service_1")
    service_2 = create_service(service_name="service_2")
    id_1 = create_template(service_1).id
    id_2 = create_template(service_1).id
    id_3 = create_template(service_2).id

    json_resp_1 = admin_request.get("template.get_all_templates_for_service", service_id=service_1.id)
    json_resp_2 = admin_request.get("template.get_all_templates_for_service", service_id=service_2.id)

    assert {template["id"] for template in json_resp_1["data"]} == {str(id_1), str(id_2)}
    assert {template["id"] for template in json_resp_2["data"]} == {str(id_3)}


@pytest.mark.parametrize(
    "extra_args",
    (
        {},
        {"detailed": True},
        {"detailed": "True"},
    ),
)
def test_should_get_return_all_fields_by_default(
    admin_request,
    sample_email_template,
    extra_args,
):
    json_response = admin_request.get(
        "template.get_all_templates_for_service", service_id=sample_email_template.service.id, **extra_args
    )
    assert json_response["data"][0].keys() == {
        "archived",
        "content",
        "created_at",
        "created_by",
        "folder",
        "has_unsubscribe_link",
        "hidden",
        "id",
        "is_precompiled_letter",
        "letter_attachment",
        "letter_languages",
        "letter_welsh_content",
        "letter_welsh_subject",
        "name",
        "postage",
        "process_type",
        "redact_personalisation",
        "reply_to_text",
        "reply_to",
        "service_letter_contact",
        "service",
        "subject",
        "template_redacted",
        "template_type",
        "updated_at",
        "version",
    }


@pytest.mark.parametrize(
    "extra_args",
    (
        {"detailed": False},
        {"detailed": "False"},
    ),
)
@pytest.mark.parametrize(
    "template_type, expected_content",
    (
        (EMAIL_TYPE, None),
        (SMS_TYPE, None),
        (LETTER_TYPE, None),
    ),
)
def test_should_not_return_content_and_subject_if_requested(
    admin_request,
    sample_service,
    extra_args,
    template_type,
    expected_content,
):
    create_template(
        sample_service,
        template_type=template_type,
        content="This is a test",
    )
    json_response = admin_request.get(
        "template.get_all_templates_for_service", service_id=sample_service.id, **extra_args
    )
    assert json_response["data"][0].keys() == {
        "content",
        "folder",
        "id",
        "is_precompiled_letter",
        "name",
        "template_type",
    }
    assert json_response["data"][0]["content"] == expected_content


@pytest.mark.parametrize(
    "subject, content, template_type",
    [
        ("about your ((thing))", "hello ((name)) we’ve received your ((thing))", EMAIL_TYPE),
        (None, "hello ((name)) we’ve received your ((thing))", SMS_TYPE),
        ("about your ((thing))", "hello ((name)) we’ve received your ((thing))", LETTER_TYPE),
    ],
)
def test_should_get_a_single_template(client, sample_user, sample_service, subject, content, template_type):
    template = create_template(sample_service, template_type=template_type, subject=subject, content=content)

    response = client.get(
        f"/service/{sample_service.id}/template/{template.id}", headers=[create_admin_authorization_header()]
    )

    data = json.loads(response.get_data(as_text=True))["data"]

    assert response.status_code == 200
    assert data["content"] == content
    assert data["subject"] == subject
    assert data["process_type"] == "normal"
    assert not data["redact_personalisation"]
    assert data["letter_attachment"] is None


@pytest.mark.parametrize(
    "endpoint,extra_args",
    [("template.get_template_by_id_and_service_id", {}), ("template.get_template_version", {"version": 2})],
)
def test_get_template_returns_letter_attachment(admin_request, sample_letter_template, endpoint, extra_args):
    attachment = create_letter_attachment(created_by_id=sample_letter_template.created_by_id)
    sample_letter_template.letter_attachment_id = attachment.id
    dao_update_template(sample_letter_template)

    data = admin_request.get(
        endpoint,
        service_id=sample_letter_template.service_id,
        template_id=sample_letter_template.id,
        **extra_args,
    )

    assert data["data"]["letter_attachment"]["id"] == str(attachment.id)
    assert data["data"]["letter_attachment"]["page_count"] == attachment.page_count


@pytest.mark.parametrize(
    "subject, content, path, expected_subject, expected_content, expected_error",
    [
        (
            "about your thing",
            "hello user we’ve received your thing",
            "/service/{}/template/{}/preview",
            "about your thing",
            "hello user we’ve received your thing",
            None,
        ),
        (
            "about your ((thing))",
            "hello ((name)) we’ve received your ((thing))",
            "/service/{}/template/{}/preview?name=Amala&thing=document",
            "about your document",
            "hello Amala we’ve received your document",
            None,
        ),
        (
            "about your ((thing))",
            "hello ((name)) we’ve received your ((thing))",
            "/service/{}/template/{}/preview?eman=Amala&gniht=document",
            None,
            None,
            "Missing personalisation: thing, name",
        ),
        (
            "about your ((thing))",
            "hello ((name)) we’ve received your ((thing))",
            "/service/{}/template/{}/preview?name=Amala&thing=document&foo=bar",
            "about your document",
            "hello Amala we’ve received your document",
            None,
        ),
    ],
)
def test_should_preview_a_single_template(
    client, sample_service, subject, content, path, expected_subject, expected_content, expected_error
):
    template = create_template(sample_service, template_type=EMAIL_TYPE, subject=subject, content=content)

    response = client.get(path.format(sample_service.id, template.id), headers=[create_admin_authorization_header()])

    content = json.loads(response.get_data(as_text=True))

    if expected_error:
        assert response.status_code == 400
        assert content["message"]["template"] == [expected_error]
    else:
        assert response.status_code == 200
        assert content["content"] == expected_content
        assert content["subject"] == expected_subject


def test_should_return_empty_array_if_no_templates_for_service(client, sample_service):
    auth_header = create_admin_authorization_header()

    response = client.get(f"/service/{sample_service.id}/template", headers=[auth_header])

    assert response.status_code == 200
    json_resp = json.loads(response.get_data(as_text=True))
    assert len(json_resp["data"]) == 0


def test_should_return_404_if_no_templates_for_service_with_id(client, sample_service, fake_uuid):
    auth_header = create_admin_authorization_header()

    response = client.get(f"/service/{sample_service.id}/template/{fake_uuid}", headers=[auth_header])

    assert response.status_code == 404
    json_resp = json.loads(response.get_data(as_text=True))
    assert json_resp["result"] == "error"
    assert json_resp["message"] == "No result found"

def test_create_400_for_over_limit_content(
    client,
    notify_api,
     sample_user
):
    sample_service = create_service(service_permissions=[SMS_TYPE])
    content = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(SMS_CHAR_COUNT_LIMIT + 1))
    data = {
        "name": "too big template",
        "template_type": SMS_TYPE,
        "content": content,
        "service": str(sample_service.id),
        "created_by": str(sample_service.created_by.id),
    }
    data = json.dumps(data)
    auth_header = create_admin_authorization_header()

    response = client.post(
        f"/service/{sample_service.id}/template",
        headers=[("Content-Type", "application/json"), auth_header],
        data=data,
    )
    assert response.status_code == 400
    json_resp = json.loads(response.get_data(as_text=True))
    assert (f"Content has a character count greater than the limit of {SMS_CHAR_COUNT_LIMIT}") in json_resp["message"][
        "content"
    ]


def test_update_400_for_over_limit_content(client, notify_api, sample_user, sample_template):
    json_data = json.dumps(
        {
            "content": "".join(
                random.choice(string.ascii_uppercase + string.digits) for _ in range(SMS_CHAR_COUNT_LIMIT + 1)
            ),
            "created_by": str(sample_user.id),
        }
    )
    auth_header = create_admin_authorization_header()
    resp = client.post(
        f"/service/{sample_template.service.id}/template/{sample_template.id}",
        headers=[("Content-Type", "application/json"), auth_header],
        data=json_data,
    )
    assert resp.status_code == 400
    json_resp = json.loads(resp.get_data(as_text=True))
    assert (f"Content has a character count greater than the limit of {SMS_CHAR_COUNT_LIMIT}") in json_resp["message"][
        "content"
    ]


def test_should_return_all_template_versions_for_service_and_template_id(client, sample_template):
    original_content = sample_template.content
    from app.dao.templates_dao import dao_update_template

    sample_template.content = original_content + "1"
    dao_update_template(sample_template)
    sample_template.content = original_content + "2"
    dao_update_template(sample_template)

    auth_header = create_admin_authorization_header()
    resp = client.get(
        f"/service/{sample_template.service_id}/template/{sample_template.id}/versions",
        headers=[("Content-Type", "application/json"), auth_header],
    )
    assert resp.status_code == 200
    resp_json = json.loads(resp.get_data(as_text=True))["data"]
    assert len(resp_json) == 3
    for x in resp_json:
        if x["version"] == 1:
            assert x["content"] == original_content
        elif x["version"] == 2:
            assert x["content"] == original_content + "1"
        else:
            assert x["content"] == original_content + "2"


def test_update_does_not_create_new_version_when_there_is_no_change(client, sample_template):
    auth_header = create_admin_authorization_header()
    data = {
        "template_type": sample_template.template_type,
        "content": sample_template.content,
    }
    resp = client.post(
        f"/service/{sample_template.service_id}/template/{sample_template.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    assert resp.status_code == 200

    template = dao_get_template_by_id(sample_template.id)
    assert template.version == 1


def test_update_set_process_type_on_template(client, sample_template):
    auth_header = create_admin_authorization_header()
    data = {"process_type": "priority"}
    resp = client.post(
        f"/service/{sample_template.service_id}/template/{sample_template.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )
    assert resp.status_code == 200

    template = dao_get_template_by_id(sample_template.id)
    assert template.process_type == "priority"


def test_create_a_template_with_reply_to(admin_request, sample_user):
    service = create_service(service_permissions=["letter"])
    letter_contact = create_letter_contact(service, "Edinburgh, ED1 1AA")
    data = {
        "name": "my template",
        "subject": "subject",
        "template_type": "letter",
        "content": "template <b>content</b>",
        "service": str(service.id),
        "created_by": str(sample_user.id),
        "reply_to": str(letter_contact.id),
    }

    json_resp = admin_request.post("template.create_template", service_id=service.id, _data=data, _expected_status=201)

    assert json_resp["data"]["template_type"] == "letter"
    assert json_resp["data"]["reply_to"] == str(letter_contact.id)
    assert json_resp["data"]["reply_to_text"] == letter_contact.contact_block

    template = Template.query.get(json_resp["data"]["id"])
    from app.schemas import template_schema

    assert sorted(json_resp["data"]) == sorted(template_schema.dump(template))
    th = TemplateHistory.query.filter_by(id=template.id, version=1).one()
    assert th.service_letter_contact_id == letter_contact.id


def test_create_template_bilingual_letter(admin_request, sample_service_full_permissions, sample_user):
    letter_contact = create_letter_contact(sample_service_full_permissions, "Edinburgh, ED1 1AA")
    json_resp = admin_request.post(
        "template.create_template",
        service_id=sample_service_full_permissions.id,
        _data={
            "name": "my template",
            "template_type": "letter",
            "subject": "subject",
            "content": "content",
            "postage": "second",
            "service": str(sample_service_full_permissions.id),
            "created_by": str(sample_user.id),
            "reply_to": str(letter_contact.id),
            "letter_languages": "welsh_then_english",
            "letter_welsh_subject": "welsh subject",
            "letter_welsh_content": "welsh body",
        },
        _expected_status=201,
    )

    t = Template.query.get(json_resp["data"]["id"])
    assert t.letter_languages == "welsh_then_english"
    assert t.letter_welsh_subject == "welsh subject"
    assert t.letter_welsh_content == "welsh body"


def test_create_a_template_with_foreign_service_reply_to(admin_request, sample_user):
    service = create_service(service_permissions=["letter"])
    service2 = create_service(service_name="test service", service_permissions=["letter"])
    letter_contact = create_letter_contact(service2, "Edinburgh, ED1 1AA")
    data = {
        "name": "my template",
        "subject": "subject",
        "template_type": "letter",
        "content": "template <b>content</b>",
        "service": str(service.id),
        "created_by": str(sample_user.id),
        "reply_to": str(letter_contact.id),
    }

    json_resp = admin_request.post("template.create_template", service_id=service.id, _data=data, _expected_status=400)

    assert (
        json_resp["message"]
        == f"letter_contact_id {str(letter_contact.id)} does not exist in database for service id {str(service.id)}"
    )


@pytest.mark.parametrize(
    "post_data, expected_errors",
    [
        (
            {},
            [
                {"error": "ValidationError", "message": "subject is a required property"},
                {"error": "ValidationError", "message": "name is a required property"},
                {"error": "ValidationError", "message": "template_type is a required property"},
                {"error": "ValidationError", "message": "content is a required property"},
                {"error": "ValidationError", "message": "service is a required property"},
                {"error": "ValidationError", "message": "created_by is a required property"},
            ],
        ),
        (
            {
                "name": "my template",
                "template_type": "sms",
                "content": "hi",
                "postage": "third",
                "service": "1af43c02-b5a8-4923-ad7f-5279b75ff2d0",
                "created_by": "30587644-9083-44d8-a114-98887f07f1e3",
            },
            [
                {
                    "error": "ValidationError",
                    "message": "postage invalid. It must be either first or second.",
                },
            ],
        ),
    ],
)
def test_create_template_validates_against_json_schema(
    admin_request,
    sample_service_full_permissions,
    post_data,
    expected_errors,
):
    response = admin_request.post(
        "template.create_template", service_id=sample_service_full_permissions.id, _data=post_data, _expected_status=400
    )
    assert response["errors"] == expected_errors


def test_create_template_validates_qr_code_too_long(
    admin_request,
    sample_service_full_permissions,
):
    response = admin_request.post(
        "template.create_template",
        service_id=sample_service_full_permissions.id,
        _data={
            "name": "my template",
            "template_type": "letter",
            "subject": "subject",
            "content": "qr: " + ("too long " * 100),
            "postage": "second",
            "service": str(sample_service_full_permissions.id),
            "created_by": "30587644-9083-44d8-a114-98887f07f1e3",
        },
        _expected_status=400,
    )

    assert response == {"result": "error", "message": {"content": ["qr-code-too-long"]}}


@pytest.mark.parametrize(
    "template_default, service_default",
    [("template address", "service address"), (None, "service address"), ("template address", None), (None, None)],
)
def test_get_template_reply_to(client, sample_service, template_default, service_default):
    auth_header = create_admin_authorization_header()
    if service_default:
        create_letter_contact(service=sample_service, contact_block=service_default, is_default=True)
    if template_default:
        template_default_contact = create_letter_contact(
            service=sample_service, contact_block=template_default, is_default=False
        )
    reply_to_id = str(template_default_contact.id) if template_default else None
    template = create_template(service=sample_service, template_type="letter", reply_to=reply_to_id)

    resp = client.get(f"/service/{template.service_id}/template/{template.id}", headers=[auth_header])

    assert resp.status_code == 200, resp.get_data(as_text=True)
    json_resp = json.loads(resp.get_data(as_text=True))

    assert "service_letter_contact_id" not in json_resp["data"]
    assert json_resp["data"]["reply_to"] == reply_to_id
    assert json_resp["data"]["reply_to_text"] == template_default


def test_update_template_reply_to(client, sample_letter_template):
    auth_header = create_admin_authorization_header()
    letter_contact = create_letter_contact(sample_letter_template.service, "Edinburgh, ED1 1AA")
    data = {
        "reply_to": str(letter_contact.id),
    }

    resp = client.post(
        f"/service/{sample_letter_template.service_id}/template/{sample_letter_template.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)

    template = dao_get_template_by_id(sample_letter_template.id)
    assert template.service_letter_contact_id == letter_contact.id
    th = TemplateHistory.query.filter_by(id=sample_letter_template.id, version=2).one()
    assert th.service_letter_contact_id == letter_contact.id


def test_update_template_reply_to_does_not_overwrite_letter_attachment(admin_request, sample_letter_template):
    letter_contact = create_letter_contact(sample_letter_template.service, "Edinburgh, ED1 1AA")

    attachment = create_letter_attachment(created_by_id=sample_letter_template.created_by_id)
    sample_letter_template.letter_attachment_id = attachment.id
    dao_update_template(sample_letter_template)

    data = {"reply_to": str(letter_contact.id)}

    assert sample_letter_template.letter_attachment_id == attachment.id

    admin_request.post(
        "template.update_template",
        service_id=sample_letter_template.service_id,
        template_id=sample_letter_template.id,
        _data=data,
    )
    previous = TemplateHistory.query.filter_by(id=sample_letter_template.id, version=2).one()
    assert previous.letter_attachment_id == attachment.id
    assert previous.service_letter_contact_id is None

    latest = TemplateHistory.query.filter_by(id=sample_letter_template.id, version=3).one()
    assert latest.service_letter_contact_id == letter_contact.id
    assert latest.letter_attachment_id == attachment.id


def test_update_template_reply_to_set_to_blank(client, notify_db_session):
    auth_header = create_admin_authorization_header()
    service = create_service(service_permissions=["letter"])
    letter_contact = create_letter_contact(service, "Edinburgh, ED1 1AA")
    template = create_template(service=service, template_type="letter", reply_to=letter_contact.id)

    data = {
        "reply_to": None,
    }

    resp = client.post(
        f"/service/{template.service_id}/template/{template.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)

    template = dao_get_template_by_id(template.id)
    assert template.service_letter_contact_id is None
    th = TemplateHistory.query.filter_by(id=template.id, version=2).one()
    assert th.service_letter_contact_id is None


def test_update_template_validates_postage(admin_request, sample_service_full_permissions):
    template = create_template(service=sample_service_full_permissions, template_type="letter")

    response = admin_request.post(
        "template.update_template",
        service_id=sample_service_full_permissions.id,
        template_id=template.id,
        _data={"postage": "third"},
        _expected_status=400,
    )
    assert "postage invalid" in response["errors"][0]["message"]


def test_update_template_with_foreign_service_reply_to(client, sample_letter_template):
    auth_header = create_admin_authorization_header()

    service2 = create_service(service_name="test service", service_permissions=["letter"])
    letter_contact = create_letter_contact(service2, "Edinburgh, ED1 1AA")

    data = {
        "reply_to": str(letter_contact.id),
    }

    resp = client.post(
        f"/service/{sample_letter_template.service_id}/template/{sample_letter_template.id}",
        data=json.dumps(data),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    json_resp = json.loads(resp.get_data(as_text=True))

    assert json_resp["message"] == (
        f"letter_contact_id {str(letter_contact.id)} does not exist in database for service "
        f"id {str(sample_letter_template.service_id)}"
    )


def test_update_redact_template(admin_request, sample_template):
    assert sample_template.redact_personalisation is False

    data = {"redact_personalisation": True, "created_by": str(sample_template.created_by_id)}

    dt = datetime.now()

    with freeze_time(dt):
        resp = admin_request.post(
            "template.update_template",
            service_id=sample_template.service_id,
            template_id=sample_template.id,
            _data=data,
        )

    assert resp is None

    assert sample_template.redact_personalisation is True
    assert sample_template.template_redacted.updated_by_id == sample_template.created_by_id
    assert sample_template.template_redacted.updated_at == dt

    assert sample_template.version == 1


def test_update_redact_template_ignores_other_properties(admin_request, sample_template):
    data = {"name": "Foo", "redact_personalisation": True, "created_by": str(sample_template.created_by_id)}

    admin_request.post(
        "template.update_template", service_id=sample_template.service_id, template_id=sample_template.id, _data=data
    )

    assert sample_template.redact_personalisation is True
    assert sample_template.name != "Foo"


def test_update_redact_template_does_nothing_if_already_redacted(admin_request, sample_template):
    dt = datetime.now()
    with freeze_time(dt):
        dao_redact_template(sample_template, sample_template.created_by_id)

    data = {"redact_personalisation": True, "created_by": str(sample_template.created_by_id)}

    with freeze_time(dt + timedelta(days=1)):
        resp = admin_request.post(
            "template.update_template",
            service_id=sample_template.service_id,
            template_id=sample_template.id,
            _data=data,
        )

    assert resp is None

    assert sample_template.redact_personalisation is True
    # make sure that it hasn't been updated
    assert sample_template.template_redacted.updated_at == dt


def test_update_redact_template_400s_if_no_created_by(admin_request, sample_template):
    original_updated_time = sample_template.template_redacted.updated_at
    resp = admin_request.post(
        "template.update_template",
        service_id=sample_template.service_id,
        template_id=sample_template.id,
        _data={"redact_personalisation": True},
        _expected_status=400,
    )

    assert resp == {"result": "error", "message": {"created_by": ["Field is required"]}}

    assert sample_template.redact_personalisation is False
    assert sample_template.template_redacted.updated_at == original_updated_time


def test_update_template_400s_if_static_qr_code_too_long(admin_request, sample_service_full_permissions):
    sample_template = create_template(sample_service_full_permissions, template_type=LETTER_TYPE, content="before")
    resp = admin_request.post(
        "template.update_template",
        service_id=sample_template.service_id,
        template_id=sample_template.id,
        _data={"content": "qr: " + ("too long " * 100)},
        _expected_status=400,
    )

    assert resp == {"result": "error", "message": {"content": ["qr-code-too-long"]}}


def test_preview_letter_template_by_id_invalid_file_type(sample_letter_notification, admin_request):
    resp = admin_request.get(
        "template.preview_letter_template_by_notification_id",
        service_id=sample_letter_notification.service_id,
        template_id=sample_letter_notification.template_id,
        notification_id=sample_letter_notification.id,
        file_type="doc",
        _expected_status=400,
    )

    assert ["file_type must be pdf or png"] == resp["message"]["content"]


@freeze_time("2012-12-12")
@pytest.mark.parametrize("file_type", ("png", "pdf"))
def test_preview_letter_template_by_id_valid_file_type(
    notify_api,
    sample_letter_notification,
    admin_request,
    mock_onwards_request_headers,
    file_type,
):
    sample_letter_notification.created_at = datetime.utcnow()
    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        with requests_mock.Mocker() as request_mock:
            content = b"\x00\x01"

            mock_post = request_mock.post(
                f"http://localhost/notifications-template-preview/preview.{file_type}",
                content=content,
                headers={
                    "X-pdf-page-count": "1",
                    "some-onwards": "request-headers",
                },
                status_code=200,
            )

            resp = admin_request.get(
                "template.preview_letter_template_by_notification_id",
                service_id=sample_letter_notification.service_id,
                notification_id=sample_letter_notification.id,
                file_type=file_type,
            )

            post_json = mock_post.last_request.json()
            assert post_json["template"]["id"] == str(sample_letter_notification.template_id)
            assert post_json["values"] == {
                "address_line_1": "A1",
                "address_line_2": "A2",
                "address_line_3": "A3",
                "address_line_4": "A4",
                "address_line_5": "A5",
                "address_line_6": "A6",
                "postcode": "A_POST",
            }
            assert post_json["date"] == "2012-12-12T00:00:00"
            assert post_json["filename"] is None
            assert base64.b64decode(resp["content"]) == content


@freeze_time("2012-12-12")
def test_preview_letter_template_by_id_shows_template_version_used_by_notification(
    notify_api,
    sample_letter_notification,
    sample_letter_template,
    mock_onwards_request_headers,
    admin_request,
):
    sample_letter_notification.created_at = datetime.utcnow()
    assert sample_letter_notification.template_version == 1

    # Create a new template history to check that our preview doesn't use the newest version
    # but instead the one linked with the notification
    sample_letter_template.content = "new content"
    dao_update_template(sample_letter_template)
    versions = dao_get_template_versions(sample_letter_notification.service.id, sample_letter_template.id)
    assert len(versions) == 2

    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        with requests_mock.Mocker() as request_mock:
            content = b"\x00\x01"

            mock_post = request_mock.post(
                "http://localhost/notifications-template-preview/preview.png",
                content=content,
                headers={
                    "X-pdf-page-count": "1",
                    "some-onwards": "request-headers",
                },
                status_code=200,
            )

            admin_request.get(
                "template.preview_letter_template_by_notification_id",
                service_id=sample_letter_notification.service_id,
                notification_id=sample_letter_notification.id,
                file_type="png",
            )

            post_json = mock_post.last_request.json()
            assert post_json["template"]["id"] == str(sample_letter_notification.template_id)
            assert post_json["template"]["version"] == "1"


def test_preview_letter_template_by_id_template_preview_500(
    notify_api,
    client,
    admin_request,
    sample_letter_notification,
    mock_onwards_request_headers,
):
    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        import requests_mock

        with requests_mock.Mocker() as request_mock:
            content = b"\x00\x01"

            mock_post = request_mock.post(
                "http://localhost/notifications-template-preview/preview.pdf",
                content=content,
                headers={
                    "X-pdf-page-count": "1",
                    "some-onwards": "request-headers",
                },
                status_code=404,
            )

            resp = admin_request.get(
                "template.preview_letter_template_by_notification_id",
                service_id=sample_letter_notification.service_id,
                notification_id=sample_letter_notification.id,
                file_type="pdf",
                _expected_status=500,
            )

            assert mock_post.last_request.json()
            assert "Status code: 404" in resp["message"]
            assert f"Error generating preview letter for {sample_letter_notification.id}" in resp["message"]


def test_preview_letter_template_precompiled_pdf_file_type(notify_api, client, admin_request, sample_service, mocker):
    template = create_template(
        sample_service,
        template_type="letter",
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        hidden=True,
    )

    notification = create_notification(template)

    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        with requests_mock.Mocker():
            content = b"\x00\x01"

            mock_get_letter_pdf = mocker.patch(
                "app.template.rest.get_letter_pdf_and_metadata",
                return_value=(content, {"message": "", "invalid_pages": "", "page_count": "1"}),
            )

            resp = admin_request.get(
                "template.preview_letter_template_by_notification_id",
                service_id=notification.service_id,
                notification_id=notification.id,
                file_type="pdf",
            )

            assert mock_get_letter_pdf.called_once_with(notification)
            assert base64.b64decode(resp["content"]) == content


def test_preview_letter_template_precompiled_s3_error(notify_api, client, admin_request, sample_service, mocker):
    template = create_template(
        sample_service,
        template_type="letter",
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        hidden=True,
    )

    notification = create_notification(template)

    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        with requests_mock.Mocker():
            mocker.patch(
                "app.template.rest.get_letter_pdf_and_metadata",
                side_effect=botocore.exceptions.ClientError(
                    {"Error": {"Code": "403", "Message": "Unauthorized"}}, "GetObject"
                ),
            )

            request = admin_request.get(
                "template.preview_letter_template_by_notification_id",
                service_id=notification.service_id,
                notification_id=notification.id,
                file_type="pdf",
                _expected_status=500,
            )

            assert (
                request["message"]
                == f"Error extracting requested page from PDF file for notification_id {notification.id} type "
                "<class 'botocore.exceptions.ClientError'> An error occurred (403) "
                "when calling the GetObject operation: Unauthorized"
            )


@pytest.mark.parametrize(
    "requested_page, message, expected_post_url",
    [
        # page defaults to 1, page is valid, no overlay shown
        ("", "", "precompiled-preview.png"),
        # page is valid, no overlay shown
        ("1", "", "precompiled-preview.png"),
        # page is invalid but not because content is outside printable area so no overlay
        ("1", "letter-not-a4-portrait-oriented", "precompiled-preview.png"),
        # page is invalid, overlay shown
        ("1", "content-outside-printable-area", "precompiled/overlay.png?page_number=1"),
        # page is valid, no overlay shown
        ("2", "content-outside-printable-area", "precompiled-preview.png"),
        # page is invalid, overlay shown
        ("3", "content-outside-printable-area", "precompiled/overlay.png?page_number=3"),
    ],
)
def test_preview_letter_template_precompiled_for_png_shows_overlay_on_pages_with_content_outside_printable_area(
    notify_api,
    client,
    admin_request,
    sample_service,
    mocker,
    mock_onwards_request_headers,
    requested_page,
    message,
    expected_post_url,
):
    template = create_template(
        sample_service,
        template_type="letter",
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        hidden=True,
    )

    notification = create_notification(template)

    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        with requests_mock.Mocker() as request_mock:
            pdf_content = b"\x00\x01"
            expected_returned_content = b"\x00\x02"

            metadata = {"message": message, "invalid_pages": "[1,3]", "page_count": "4"}

            mock_get_letter_pdf = mocker.patch(
                "app.template.rest.get_letter_pdf_and_metadata", return_value=(pdf_content, metadata)
            )

            mocker.patch("app.template.rest.extract_page_from_pdf", return_value=pdf_content)

            mock_post = request_mock.post(
                f"http://localhost/notifications-template-preview/{expected_post_url}",
                content=expected_returned_content,
                headers={
                    "X-pdf-page-count": "4",
                    "some-onwards": "request-headers",
                },
                status_code=200,
            )

            response = admin_request.get(
                "template.preview_letter_template_by_notification_id",
                page=requested_page,
                service_id=notification.service_id,
                notification_id=notification.id,
                file_type="png",
            )

            with pytest.raises(ValueError):
                mock_post.last_request.json()
            assert mock_get_letter_pdf.called_once_with(notification)
            assert base64.b64decode(response["content"]) == expected_returned_content
            assert response["metadata"] == metadata


@pytest.mark.parametrize(
    "invalid_pages",
    [
        "[1,3]",
        "[2,4]",  # it shouldn't make a difference if the error was on the first page or not
    ],
)
def test_preview_letter_template_precompiled_for_pdf_shows_overlay_on_all_pages_if_content_outside_printable_area(
    notify_api,
    client,
    admin_request,
    sample_service,
    mocker,
    mock_onwards_request_headers,
    invalid_pages,
):
    template = create_template(
        sample_service,
        template_type="letter",
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        hidden=True,
    )

    notification = create_notification(template)

    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        with requests_mock.Mocker() as request_mock:
            pdf_content = b"\x00\x01"
            expected_returned_content = b"\x00\x02"

            metadata = {"message": "content-outside-printable-area", "invalid_pages": invalid_pages, "page_count": "4"}

            mock_get_letter_pdf = mocker.patch(
                "app.template.rest.get_letter_pdf_and_metadata", return_value=(pdf_content, metadata)
            )

            mocker.patch("app.template.rest.extract_page_from_pdf", return_value=pdf_content)

            mock_post = request_mock.post(
                "http://localhost/notifications-template-preview/precompiled/overlay.pdf",
                content=expected_returned_content,
                headers={
                    "X-pdf-page-count": "4",
                    "some-onwards": "request-headers",
                },
                status_code=200,
            )

            response = admin_request.get(
                "template.preview_letter_template_by_notification_id",
                service_id=notification.service_id,
                notification_id=notification.id,
                file_type="pdf",
            )

            with pytest.raises(ValueError):
                mock_post.last_request.json()
            assert mock_get_letter_pdf.called_once_with(notification)
            assert base64.b64decode(response["content"]) == expected_returned_content
            assert response["metadata"] == metadata


@pytest.mark.parametrize(
    "page_number,expect_preview_url",
    [
        ("", "http://localhost/notifications-template-preview/precompiled-preview.png?hide_notify=true"),
        ("1", "http://localhost/notifications-template-preview/precompiled-preview.png?hide_notify=true"),
        ("2", "http://localhost/notifications-template-preview/precompiled-preview.png"),
    ],
)
def test_preview_letter_template_precompiled_png_file_type_hide_notify_tag_only_on_first_page(
    notify_api, client, admin_request, sample_service, mocker, page_number, expect_preview_url
):
    template = create_template(
        sample_service,
        template_type="letter",
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        hidden=True,
    )

    notification = create_notification(template)

    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        pdf_content = b"\x00\x01"
        png_content = b"\x00\x02"
        encoded = base64.b64encode(png_content).decode("utf-8")

        mocker.patch(
            "app.template.rest.get_letter_pdf_and_metadata",
            return_value=(pdf_content, {"message": "", "invalid_pages": "", "page_count": "2"}),
        )
        mocker.patch("app.template.rest.extract_page_from_pdf", return_value=png_content)
        mock_get_png_preview = mocker.patch("app.template.rest._get_png_preview_or_overlaid_pdf", return_value=encoded)

        admin_request.get(
            "template.preview_letter_template_by_notification_id",
            service_id=notification.service_id,
            notification_id=notification.id,
            file_type="png",
            page=page_number,
        )

        mock_get_png_preview.assert_called_once_with(expect_preview_url, encoded, notification.id, json=False)


def test_preview_letter_template_precompiled_png_template_preview_500_error(
    notify_api,
    client,
    admin_request,
    sample_service,
    mocker,
    mock_onwards_request_headers,
):
    template = create_template(
        sample_service,
        template_type="letter",
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        hidden=True,
    )

    notification = create_notification(template)

    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        with requests_mock.Mocker() as request_mock:
            pdf_content = b"\x00\x01"
            png_content = b"\x00\x02"

            mocker.patch(
                "app.template.rest.get_letter_pdf_and_metadata",
                return_value=(pdf_content, {"message": "", "invalid_pages": "", "page_count": "1"}),
            )

            mocker.patch("app.template.rest.extract_page_from_pdf", return_value=pdf_content)

            mock_post = request_mock.post(
                "http://localhost/notifications-template-preview/precompiled-preview.png",
                content=png_content,
                headers={
                    "X-pdf-page-count": "1",
                    "some-onwards": "request-headers",
                },
                status_code=500,
            )

            admin_request.get(
                "template.preview_letter_template_by_notification_id",
                service_id=notification.service_id,
                notification_id=notification.id,
                file_type="png",
                _expected_status=500,
            )

            with pytest.raises(ValueError):
                mock_post.last_request.json()


def test_preview_letter_template_precompiled_png_template_preview_400_error(
    notify_api,
    client,
    admin_request,
    sample_service,
    mocker,
    mock_onwards_request_headers,
):
    template = create_template(
        sample_service,
        template_type="letter",
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        hidden=True,
    )

    notification = create_notification(template)

    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        with requests_mock.Mocker() as request_mock:
            pdf_content = b"\x00\x01"
            png_content = b"\x00\x02"

            mocker.patch(
                "app.template.rest.get_letter_pdf_and_metadata",
                return_value=(pdf_content, {"message": "", "invalid_pages": "", "page_count": "1"}),
            )

            mocker.patch("app.template.rest.extract_page_from_pdf", return_value=pdf_content)

            mock_post = request_mock.post(
                "http://localhost/notifications-template-preview/precompiled-preview.png",
                content=png_content,
                headers={
                    "X-pdf-page-count": "1",
                    "some-onwards": "request-headers",
                },
                status_code=404,
            )

            admin_request.get(
                "template.preview_letter_template_by_notification_id",
                service_id=notification.service_id,
                notification_id=notification.id,
                file_type="png",
                _expected_status=500,
            )

            with pytest.raises(ValueError):
                mock_post.last_request.json()


def test_preview_letter_template_precompiled_png_template_preview_pdf_error(
    notify_api,
    client,
    admin_request,
    sample_service,
    mocker,
    mock_onwards_request_headers,
):
    template = create_template(
        sample_service,
        template_type="letter",
        template_name="Pre-compiled PDF",
        subject="Pre-compiled PDF",
        hidden=True,
    )

    notification = create_notification(template)

    with set_config_values(
        notify_api,
        {
            "TEMPLATE_PREVIEW_API_HOST": "http://localhost/notifications-template-preview",
            "TEMPLATE_PREVIEW_API_KEY": "test-key",
        },
    ):
        with requests_mock.Mocker() as request_mock:
            pdf_content = b"\x00\x01"
            png_content = b"\x00\x02"

            mocker.patch(
                "app.template.rest.get_letter_pdf_and_metadata",
                return_value=(pdf_content, {"message": "", "invalid_pages": "", "page_count": "1"}),
            )

            error_message = "PDF Error message"
            mocker.patch("app.template.rest.extract_page_from_pdf", side_effect=PdfReadError(error_message))

            request_mock.post(
                "http://localhost/notifications-template-preview/precompiled-preview.png",
                content=png_content,
                headers={
                    "X-pdf-page-count": "1",
                    "some-onwards": "request-headers",
                },
                status_code=404,
            )

            request = admin_request.get(
                "template.preview_letter_template_by_notification_id",
                service_id=notification.service_id,
                notification_id=notification.id,
                file_type="png",
                _expected_status=500,
            )

            assert request["message"] == (
                f"Error extracting requested page from PDF file for notification_id {notification.id} "
                f"type {type(PdfReadError())} {error_message}"
            )
