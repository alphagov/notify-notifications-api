from itertools import product

import pytest

from app.constants import EMAIL_TYPE, TEMPLATE_TYPES
from tests.app.db import create_template


def test_get_all_templates_returns_200(api_client_request, sample_service):
    templates = [
        create_template(
            sample_service,
            template_type=tmp_type,
            subject=f"subject_{name}" if tmp_type == EMAIL_TYPE else "",
            template_name=name,
        )
        for name, tmp_type in product(("A", "B", "C"), TEMPLATE_TYPES)
    ]

    json_response = api_client_request.get(
        sample_service.id,
        "v2_templates.get_templates",
    )

    assert len(json_response["templates"]) == len(templates)

    for index, template in enumerate(json_response["templates"]):
        assert template["id"] == str(templates[index].id)
        assert template["body"] == templates[index].content
        assert template["type"] == templates[index].template_type
        if templates[index].template_type == EMAIL_TYPE:
            assert template["subject"] == templates[index].subject


@pytest.mark.parametrize("tmp_type", TEMPLATE_TYPES)
def test_get_all_templates_for_valid_type_returns_200(api_client_request, sample_service, tmp_type):
    templates = [
        create_template(
            sample_service,
            template_type=tmp_type,
            template_name=f"Template {i}",
            subject=f"subject_{i}" if tmp_type == EMAIL_TYPE else "",
        )
        for i in range(3)
    ]

    json_response = api_client_request.get(sample_service.id, "v2_templates.get_templates", type=tmp_type)

    assert len(json_response["templates"]) == len(templates)

    for index, template in enumerate(json_response["templates"]):
        assert template["id"] == str(templates[index].id)
        assert template["body"] == templates[index].content
        assert template["type"] == tmp_type
        if templates[index].template_type == EMAIL_TYPE:
            assert template["subject"] == templates[index].subject


@pytest.mark.parametrize("tmp_type", TEMPLATE_TYPES)
def test_get_correct_num_templates_for_valid_type_returns_200(api_client_request, sample_service, tmp_type):
    num_templates = 3

    templates = []
    for _ in range(num_templates):
        templates.append(create_template(sample_service, template_type=tmp_type))

    for other_type in TEMPLATE_TYPES:
        if other_type != tmp_type:
            templates.append(create_template(sample_service, template_type=other_type))

    json_response = api_client_request.get(sample_service.id, "v2_templates.get_templates", type=tmp_type)

    assert len(json_response["templates"]) == num_templates


def test_get_all_templates_for_invalid_type_returns_400(api_client_request, sample_service):
    invalid_type = "coconut"

    json_response = api_client_request.get(
        sample_service.id, "v2_templates.get_templates", type=invalid_type, _expected_status=400
    )

    assert json_response == {
        "status_code": 400,
        "errors": [{"message": "type coconut is not one of [sms, email, letter]", "error": "ValidationError"}],
    }
