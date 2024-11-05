import uuid

import pytest
from flask import json
from jsonschema.exceptions import ValidationError

from app.constants import EMAIL_TYPE, SMS_TYPE, TEMPLATE_TYPES
from app.schema_validation import validate
from app.v2.templates.templates_schemas import (
    get_all_template_request,
    get_all_template_response,
)

valid_json_get_all_response = [
    {
        "templates": [
            {
                "id": str(uuid.uuid4()),
                "type": SMS_TYPE,
                "created_at": "2017-01-10T18:25:43.511Z",
                "updated_at": None,
                "version": 1,
                "created_by": "someone@test.com",
                "body": "some body",
                "name": "some name",
            },
            {
                "id": str(uuid.uuid4()),
                "type": EMAIL_TYPE,
                "created_at": "2017-02-10T18:25:43.511Z",
                "updated_at": None,
                "version": 2,
                "created_by": "someone@test.com",
                "subject": "test subject",
                "body": "some body",
                "name": "some name",
            },
        ]
    },
    {
        "templates": [
            {
                "id": str(uuid.uuid4()),
                "type": SMS_TYPE,
                "created_at": "2017-02-10T18:25:43.511Z",
                "updated_at": None,
                "version": 2,
                "created_by": "someone@test.com",
                "body": "some body",
                "name": "some name",
            }
        ]
    },
    {"templates": []},
]

invalid_json_get_all_response = [
    (
        {
            "templates": [
                {
                    "id": "invalid_id",
                    "type": SMS_TYPE,
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "updated_at": None,
                    "version": 1,
                    "created_by": "someone@test.com",
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates is not a valid UUID"],
    ),
    (
        {
            "templates": [
                {
                    "id": str(uuid.uuid4()),
                    "type": SMS_TYPE,
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "updated_at": None,
                    "version": "invalid_version",
                    "created_by": "someone@test.com",
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates invalid_version is not of type integer"],
    ),
    (
        {
            "templates": [
                {
                    "id": str(uuid.uuid4()),
                    "type": SMS_TYPE,
                    "created_at": "invalid_created_at",
                    "updated_at": None,
                    "version": 1,
                    "created_by": "someone@test.com",
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates invalid_created_at is not a date-time"],
    ),
    ({}, ["templates is a required property"]),
    (
        {
            "templates": [
                {
                    "type": SMS_TYPE,
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "updated_at": None,
                    "version": 1,
                    "created_by": "someone@test.com",
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates id is a required property"],
    ),
    (
        {
            "templates": [
                {
                    "id": str(uuid.uuid4()),
                    "type": SMS_TYPE,
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "updated_at": None,
                    "version": 1,
                    "created_by": "someone@test.com",
                    "body": "some body",
                }
            ]
        },
        ["templates name is a required property"],
    ),
    (
        {
            "templates": [
                {
                    "id": str(uuid.uuid4()),
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "updated_at": None,
                    "version": 1,
                    "created_by": "someone@test.com",
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates type is a required property"],
    ),
    (
        {
            "templates": [
                {
                    "id": str(uuid.uuid4()),
                    "type": SMS_TYPE,
                    "updated_at": None,
                    "version": 1,
                    "created_by": "someone@test.com",
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates created_at is a required property"],
    ),
    (
        {
            "templates": [
                {
                    "id": str(uuid.uuid4()),
                    "type": SMS_TYPE,
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "version": 1,
                    "created_by": "someone@test.com",
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates updated_at is a required property"],
    ),
    (
        {
            "templates": [
                {
                    "id": str(uuid.uuid4()),
                    "type": SMS_TYPE,
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "updated_at": None,
                    "created_by": "someone@test.com",
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates version is a required property"],
    ),
    (
        {
            "templates": [
                {
                    "id": str(uuid.uuid4()),
                    "type": SMS_TYPE,
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "updated_at": None,
                    "version": 1,
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates created_by is a required property"],
    ),
    (
        {
            "templates": [
                {
                    "id": str(uuid.uuid4()),
                    "type": SMS_TYPE,
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "updated_at": None,
                    "version": 1,
                    "created_by": "someone@test.com",
                    "name": "some name",
                }
            ]
        },
        ["templates body is a required property"],
    ),
    (
        {
            "templates": [
                {
                    "type": SMS_TYPE,
                    "created_at": "2017-02-10T18:25:43.511Z",
                    "updated_at": None,
                    "created_by": "someone@test.com",
                    "body": "some body",
                    "name": "some name",
                }
            ]
        },
        ["templates id is a required property", "templates version is a required property"],
    ),
]


@pytest.mark.parametrize("template_type", TEMPLATE_TYPES)
def test_get_all_template_request_schema_against_no_args_is_valid(template_type):
    data = {}
    assert validate(data, get_all_template_request) == data


@pytest.mark.parametrize("template_type", TEMPLATE_TYPES)
def test_get_all_template_request_schema_against_valid_args_is_valid(template_type):
    data = {"type": template_type}
    assert validate(data, get_all_template_request) == data


def test_get_all_template_request_schema_against_invalid_args_is_invalid():
    data = {"type": "unknown"}

    with pytest.raises(ValidationError) as e:
        validate(data, get_all_template_request)
    errors = json.loads(str(e.value))

    assert errors["status_code"] == 400
    assert len(errors["errors"]) == 1
    assert errors["errors"][0]["message"] == "type unknown is not one of [sms, email, letter]"


@pytest.mark.parametrize("response", valid_json_get_all_response)
def test_valid_get_all_templates_response_schema_is_valid(response):
    assert validate(response, get_all_template_response) == response


@pytest.mark.parametrize("response,error_messages", invalid_json_get_all_response)
def test_invalid_get_all_templates_response_schema_is_invalid(response, error_messages):
    with pytest.raises(ValidationError) as e:
        validate(response, get_all_template_response)
    errors = json.loads(str(e.value))

    assert errors["status_code"] == 400
    assert len(errors["errors"]) == len(error_messages)
    for error in errors["errors"]:
        assert error["message"] in error_messages
