import os
import re
from tempfile import NamedTemporaryFile

import boto3
from moto import mock_aws

from app.functional_tests_fixtures import _create_db_objects, apply_fixtures
from tests.conftest import set_config_values


def test_create_db_objects_sets_db_up(notify_api, notify_service):

    functional_test_password = "fake password"
    request_bin_api_token = "test_request_bin_token"
    environment = "dev-env"
    test_email_username = "notify-tests-preview"
    email_domain = "digital.cabinet-office.gov.uk"
    function_tests_govuk_key_name = "govuk_notify"
    function_tests_live_key_name = "functional_tests_service_live_key"
    function_tests_test_key_name = "functional_tests_service_test_key"
    govuk_service_id = str(notify_service.id)

    # repeat twice to test idempotence

    for _ in range(2):
        with set_config_values(
            notify_api,
            {
                "MMG_INBOUND_SMS_USERNAME": ["test_mmg_username"],
                "MMG_INBOUND_SMS_AUTH": ["test_mmg_password"],
                "INTERNAL_CLIENT_API_KEYS": {"notify-functional-tests": ["functional-tests-secret-key"]},
                "ADMIN_BASE_URL": "http://localhost:6012",
                "API_HOST_NAME": "http://localhost:6011",
            },
        ):
            variables = _create_db_objects(
                functional_test_password,
                request_bin_api_token,
                environment,
                test_email_username,
                email_domain,
                function_tests_govuk_key_name,
                function_tests_live_key_name,
                function_tests_test_key_name,
                govuk_service_id,
            )

    assert variables["FUNCTIONAL_TESTS_API_HOST"] == "http://localhost:6011"
    assert variables["FUNCTIONAL_TESTS_ADMIN_HOST"] == "http://localhost:6012"
    assert variables["ENVIRONMENT"] == "dev-env"
    assert variables["FUNCTIONAL_TEST_EMAIL"] == "notify-tests-preview+dev-env@digital.cabinet-office.gov.uk"
    assert "FUNCTIONAL_TEST_PASSWORD" in variables
    assert variables["TEST_NUMBER"] == "07700900001"
    assert variables["NOTIFY_SERVICE_API_KEY"].startswith("govuk_notify-")
    assert (
        variables["FUNCTIONAL_TESTS_SERVICE_EMAIL"]
        == "notify-tests-preview+dev-env-admin-tests@digital.cabinet-office.gov.uk"
    )
    assert (
        variables["FUNCTIONAL_TESTS_SERVICE_EMAIL_AUTH_ACCOUNT"]
        == "notify-tests-preview+dev-env-email-auth@digital.cabinet-office.gov.uk"
    )
    assert "FUNCTIONAL_TESTS_SERVICE_EMAIL_PASSWORD" in variables
    assert variables["FUNCTIONAL_TESTS_SERVICE_NUMBER"] == "07700900501"
    assert "FUNCTIONAL_TESTS_SERVICE_ID" in variables
    assert variables["FUNCTIONAL_TESTS_SERVICE_NAME"] == "Functional Tests"
    assert "FUNCTIONAL_TESTS_ORGANISATION_ID" in variables
    assert variables["FUNCTIONAL_TESTS_SERVICE_API_KEY"].startswith("functional_tests_service_live_key-")
    assert variables["FUNCTIONAL_TESTS_SERVICE_API_TEST_KEY"].startswith("functional_tests_service_test_key-")
    assert variables["FUNCTIONAL_TESTS_API_AUTH_SECRET"] == "functional-tests-secret-key"
    assert (
        variables["FUNCTIONAL_TESTS_SERVICE_EMAIL_REPLY_TO"]
        == "notify-tests-preview+dev-env-reply-to@digital.cabinet-office.gov.uk"
    )
    assert variables["FUNCTIONAL_TESTS_SERVICE_INBOUND_NUMBER"] == "07700900500"
    assert "FUNCTIONAL_TEST_SMS_TEMPLATE_ID" in variables
    assert "FUNCTIONAL_TEST_EMAIL_TEMPLATE_ID" in variables
    assert "FUNCTIONAL_TEST_LETTER_TEMPLATE_ID" in variables
    assert variables["MMG_INBOUND_SMS_USERNAME"] == "test_mmg_username"
    assert variables["MMG_INBOUND_SMS_AUTH"] == "test_mmg_password"
    assert variables["REQUEST_BIN_API_TOKEN"] == "test_request_bin_token"

    for value in variables:
        assert "'" not in value, "value cannot contain single quote"


@mock_aws
def test_function_test_fixtures_saves_to_disk_and_ssm(notify_api, os_environ, mocker):
    mocker.patch("app.functional_tests_fixtures._create_db_objects", return_value={"FOO": "BAR", "BAZ": "WAZ"})

    with NamedTemporaryFile(delete=False) as temp_file:
        temp_file_name = temp_file.name

    os.environ["FUNCTIONAL_TEST_ENV_FILE"] = temp_file_name
    os.environ["SSM_UPLOAD_PATH"] = "/test/ssm/environment"
    # AWS are changing from AWS_DEFAULT_REGION to AWS_REGION for v2 clients. Set both for now.
    os.environ["AWS_REGION"] = "eu-west-1"
    os.environ["AWS_DEFAULT_REGION"] = "eu-west-1"
    try:
        apply_fixtures()

        variables = {}
        full_file = ""
        with open(temp_file_name) as f:
            full_file = f.read()
            for line in full_file.splitlines():
                match = re.match(r"export (?P<key>[A-Z0-9_]+)='(?P<value>[^']+)'", line)
                variables[match["key"]] = match["value"]
        assert variables == {"FOO": "BAR", "BAZ": "WAZ"}

        # test that the SSM parameter was created and contains the same as the file
        ssm = boto3.client("ssm")
        response = ssm.get_parameter(Name="/test/ssm/environment", WithDecryption=True)
        assert response["Parameter"]["Value"] == full_file

    finally:
        os.remove(temp_file_name)
