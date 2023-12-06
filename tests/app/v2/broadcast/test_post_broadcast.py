from unittest.mock import ANY

import pytest
from flask import json

from app.dao.broadcast_message_dao import (
    dao_get_broadcast_message_by_id_and_service_id,
)
from tests import create_service_authorization_header
from tests.app.db import create_api_key

from . import sample_cap_xml_documents


def test_broadcast_for_service_without_permission_returns_400(
    client,
    sample_service,
):
    auth_header = create_service_authorization_header(service_id=sample_service.id)
    response = client.post(
        path="/v2/broadcast",
        data="",
        headers=[("Content-Type", "application/json"), auth_header],
    )

    assert response.status_code == 400
    assert response.get_json()["errors"][0]["message"] == ("Service is not allowed to send broadcast messages")


def test_post_broadcast_non_cap_xml_returns_415(
    client,
    sample_broadcast_service,
):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    response = client.post(
        path="/v2/broadcast",
        data=json.dumps(
            {
                "content": "This is a test",
                "reference": "abc123",
                "category": "Other",
                "areas": [
                    {
                        "name": "Hackney Marshes",
                        "polygons": [
                            [
                                [-0.038280487060546875, 51.55738264619775],
                                [-0.03184318542480469, 51.553913882566754],
                                [-0.023174285888671875, 51.55812972989382],
                                [-0.023174285888671999, 51.55812972989999],
                                [-0.029869079589843747, 51.56165153059717],
                                [-0.038280487060546875, 51.55738264619775],
                            ]
                        ],
                    },
                ],
            }
        ),
        headers=[("Content-Type", "application/json"), auth_header],
    )

    assert response.status_code == 415
    assert json.loads(response.get_data(as_text=True)) == {
        "errors": [{"error": "BadRequestError", "message": "Content type application/json not supported"}],
        "status_code": 415,
    }


def test_valid_post_cap_xml_broadcast_returns_201(
    client,
    sample_broadcast_service,
):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    response = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    assert response.status_code == 201

    response_json = json.loads(response.get_data(as_text=True))

    assert response_json["approved_at"] is None
    assert response_json["approved_by_id"] is None
    assert response_json["areas"]["names"] == ["River Steeping in Wainfleet All Saints"]
    assert response_json["cancelled_at"] is None
    assert response_json["cancelled_by_id"] is None
    assert response_json["content"].startswith("A severe flood warning has been issued. Storm Dennis")
    assert response_json["content"].endswith("closely monitoring the situation throughout the night. ")
    assert response_json["reference"] == "50385fcb0ab7aa447bbd46d848ce8466E"
    assert response_json["cap_event"] == "053/055 Issue Severe Flood Warning EA"
    assert response_json["created_at"]  # datetime generated by the DB so can’t freeze it
    assert response_json["created_by_id"] is None
    assert response_json["finishes_at"] is None
    assert response_json["id"] == ANY
    assert response_json["personalisation"] is None
    assert response_json["service_id"] == str(sample_broadcast_service.id)

    assert len(response_json["areas"]["simple_polygons"]) == 1
    assert len(response_json["areas"]["simple_polygons"][0]) == 29
    assert response_json["areas"]["simple_polygons"][0][0] == [53.10569, 0.24453]
    assert response_json["areas"]["simple_polygons"][0][-1] == [53.10569, 0.24453]
    assert response_json["areas"]["names"] == ["River Steeping in Wainfleet All Saints"]
    assert "ids" not in response_json["areas"]  # only for broadcasts created in Admin

    assert response_json["starts_at"] is None
    assert response_json["status"] == "pending-approval"
    assert response_json["template_id"] is None
    assert response_json["template_name"] is None
    assert response_json["template_version"] is None
    assert response_json["updated_at"] is None


@pytest.mark.parametrize("is_approved,expected_status", [[True, "cancelled"], [False, "rejected"]])
def test_valid_cancel_broadcast_request_calls_update_broadcast_message_status_and_returns_201(
    client, sample_broadcast_service, mocker, is_approved, expected_status
):
    api_key = create_api_key(service=sample_broadcast_service)
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    mock_redis_delete = mocker.patch("app.redis_store.delete")

    # create a broadcast
    response_for_create = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    assert response_for_create.status_code == 201

    response_json_for_create = json.loads(response_for_create.get_data(as_text=True))

    broadcast_message = dao_get_broadcast_message_by_id_and_service_id(
        response_json_for_create["id"], response_json_for_create["service_id"]
    )
    # approve broadcast
    if is_approved:
        broadcast_message.status = "broadcasting"

    mock_update = mocker.patch("app.v2.broadcast.post_broadcast.broadcast_utils.update_broadcast_message_status")

    # cancel broadcast
    response_for_cancel = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET_CANCEL_WITH_REFERENCES,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    assert response_for_cancel.status_code == 201
    mock_update.assert_called_once_with(broadcast_message, expected_status, api_key_id=api_key.id)
    mock_redis_delete.assert_called_once_with(
        f"service-{sample_broadcast_service.id}-broadcast-message-{broadcast_message.id}"
    )


@pytest.mark.parametrize(
    "cap_xml_document, expected_status, expected_error",
    (
        (
            sample_cap_xml_documents.WAINFLEET_CANCEL_WITH_REFERENCES,
            404,
            [{"error": "NoResultFound", "message": "No result found"}],
        ),
        (
            sample_cap_xml_documents.WAINFLEET_CANCEL_WITH_EMPTY_REFERENCES,
            404,
            [{"error": "NoResultFound", "message": "No result found"}],
        ),
        (
            sample_cap_xml_documents.WAINFLEET_CANCEL_WITH_MISSING_REFERENCES,
            400,
            [{"error": "BadRequestError", "message": "Missing <references>"}],
        ),
    ),
)
def test_cancel_request_does_not_cancel_broadcast_if_reference_does_not_match(
    client,
    sample_broadcast_service,
    cap_xml_document,
    expected_status,
    expected_error,
):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    # create a broadcast
    response_for_create = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WINDEMERE,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    assert response_for_create.status_code == 201

    response_json_for_create = json.loads(response_for_create.get_data(as_text=True))

    assert response_json_for_create["cancelled_at"] is None
    assert response_json_for_create["cancelled_by_id"] is None
    assert response_json_for_create["reference"] == "4f6d28b10ab7aa447bbd46d85f1e9effE"
    assert response_json_for_create["status"] == "pending-approval"

    # try to cancel broadcast, but reference doesn't match
    response_for_cancel = client.post(
        path="/v2/broadcast",
        data=cap_xml_document,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    response_for_cancel_json = json.loads(response_for_cancel.get_data(as_text=True))

    assert response_for_cancel.status_code == expected_status
    assert response_for_cancel_json["errors"] == expected_error


def test_cancel_raises_error_if_multiple_broadcasts_referenced(
    client,
    sample_broadcast_service,
):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    for cap_document in (
        sample_cap_xml_documents.WAINFLEET,
        sample_cap_xml_documents.WINDEMERE,
    ):
        response_for_create = client.post(
            path="/v2/broadcast",
            data=cap_document,
            headers=[("Content-Type", "application/cap+xml"), auth_header],
        )
        assert response_for_create.status_code == 201

    # try to cancel two broadcasts with one request
    response_for_cancel = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET_CANCEL_WITH_WINDMERE_REFERENCES,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    response_for_cancel_json = json.loads(response_for_cancel.get_data(as_text=True))

    assert response_for_cancel.status_code == 400
    assert response_for_cancel_json["errors"] == [
        {
            "error": "BadRequestError",
            "message": "Multiple alerts found - unclear which one to cancel",
        }
    ]


def test_cancel_request_does_not_cancel_broadcast_if_service_id_does_not_match(
    client, sample_broadcast_service, sample_broadcast_service_2
):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    # create a broadcast
    response_for_create = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    assert response_for_create.status_code == 201

    response_json_for_create = json.loads(response_for_create.get_data(as_text=True))

    assert response_json_for_create["cancelled_at"] is None
    assert response_json_for_create["cancelled_by_id"] is None
    assert response_json_for_create["reference"] == "50385fcb0ab7aa447bbd46d848ce8466E"
    assert response_json_for_create["status"] == "pending-approval"

    # try to cancel broadcast, but service id doesn't match
    auth_header_2 = create_service_authorization_header(service_id=sample_broadcast_service_2.id)
    response_for_cancel = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET_CANCEL_WITH_REFERENCES,
        headers=[("Content-Type", "application/cap+xml"), auth_header_2],
    )

    assert response_for_cancel.status_code == 404


@pytest.mark.parametrize(
    "is_approved, expected_cancel_tasks",
    (
        (True, 1),
        (False, 0),
    ),
)
def test_same_broadcast_cant_be_cancelled_twice(
    mocker,
    client,
    sample_broadcast_service,
    is_approved,
    expected_cancel_tasks,
):
    mock_send_broadcast_event_task = mocker.patch("app.celery.broadcast_message_tasks.send_broadcast_event.apply_async")
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    # create a broadcast
    response_for_create = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    assert response_for_create.status_code == 201

    response_json_for_create = json.loads(response_for_create.get_data(as_text=True))

    broadcast_message = dao_get_broadcast_message_by_id_and_service_id(
        response_json_for_create["id"], response_json_for_create["service_id"]
    )
    # approve broadcast
    if is_approved:
        broadcast_message.status = "broadcasting"

    first_response_for_cancel = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET_CANCEL_WITH_REFERENCES,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    assert first_response_for_cancel.status_code == 201

    second_response_for_cancel = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET_CANCEL_WITH_REFERENCES,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    assert second_response_for_cancel.status_code == 404

    assert len(mock_send_broadcast_event_task.call_args_list) == expected_cancel_tasks


def test_large_polygon_is_simplified(
    client,
    sample_broadcast_service,
):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)
    response = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WINDEMERE,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )
    assert response.status_code == 201

    response_json = json.loads(response.get_data(as_text=True))

    assert len(response_json["areas"]["simple_polygons"]) == 3
    assert len(response_json["areas"]["simple_polygons"][0]) == 6

    assert response_json["areas"]["simple_polygons"][0][0] == [54.377851258, -2.919733855]
    assert response_json["areas"]["simple_polygons"][0][-1] == [54.377851258, -2.919733855]


@pytest.mark.parametrize("training_mode_service", [True, False])
def test_valid_post_cap_xml_broadcast_sets_stubbed_to_true_for_training_mode_services(
    client, sample_broadcast_service, training_mode_service
):
    sample_broadcast_service.restricted = training_mode_service
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    response = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.WAINFLEET,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )

    assert response.status_code == 201
    response_json = json.loads(response.get_data(as_text=True))

    broadcast_message = dao_get_broadcast_message_by_id_and_service_id(response_json["id"], sample_broadcast_service.id)
    assert broadcast_message.stubbed == training_mode_service


@pytest.mark.parametrize(
    "xml_document",
    (
        "<alert>Oh no</alert>",
        '<?xml version="1.0" encoding="utf-8" ?><foo><bar/></foo>',
    ),
)
def test_invalid_post_cap_xml_broadcast_returns_400(
    client,
    sample_broadcast_service,
    xml_document,
):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    response = client.post(
        path="/v2/broadcast",
        data=xml_document,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )

    assert response.status_code == 400
    assert json.loads(response.get_data(as_text=True)) == {
        "errors": [{"error": "BadRequestError", "message": "Request data is not valid CAP XML"}],
        "status_code": 400,
    }


def test_unsupported_message_types_400(
    client,
    sample_broadcast_service,
):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)

    response = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.UPDATE,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )

    assert response.status_code == 400
    assert {
        "error": "ValidationError",
        "message": "msgType Update is not one of [Alert, Cancel]",
    } in (json.loads(response.get_data(as_text=True))["errors"])


@pytest.mark.parametrize(
    "xml_document, expected_error",
    (
        (
            sample_cap_xml_documents.LONG_UCS2,
            ("description must be 615 characters or fewer (because it " "could not be GSM7 encoded)"),
        ),
        (sample_cap_xml_documents.LONG_GSM7, ("description must be 1,395 characters or fewer")),
    ),
)
def test_content_too_long_returns_400(
    client,
    sample_broadcast_service,
    xml_document,
    expected_error,
):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)
    response = client.post(
        path="/v2/broadcast",
        data=xml_document,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )

    assert json.loads(response.get_data(as_text=True)) == {
        "errors": [
            {
                "error": "ValidationError",
                "message": expected_error,
            }
        ],
        "status_code": 400,
    }


def test_invalid_areas_returns_400(client, sample_broadcast_service):
    auth_header = create_service_authorization_header(service_id=sample_broadcast_service.id)
    response = client.post(
        path="/v2/broadcast",
        data=sample_cap_xml_documents.MISSING_AREA_NAMES,
        headers=[("Content-Type", "application/cap+xml"), auth_header],
    )

    assert json.loads(response.get_data(as_text=True)) == {
        "errors": [
            {
                "error": "ValidationError",
                # the blank spaces represent the blank areaDesc in the XML
                "message": "areas   does not match ([a-zA-Z1-9]+ )*[a-zA-Z1-9]+",
            }
        ],
        "status_code": 400,
    }
