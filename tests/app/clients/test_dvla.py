import string
import sys
import time
from datetime import datetime
from unittest.mock import Mock

import boto3
import freezegun
import jwt
import pytest
from moto import mock_ssm
from redis.exceptions import LockError
from requests import HTTPError

from app.clients.letter.dvla import DVLAClient, SSMParameter


@pytest.fixture
def ssm():
    with mock_ssm():
        ssm_client = boto3.client("ssm", "eu-west-1")
        ssm_client.put_parameter(
            Name="/notify/api/dvla_username",
            Value="some username",
            Type="SecureString",
        )
        ssm_client.put_parameter(
            Name="/notify/api/dvla_password",
            Value="some password",
            Type="SecureString",
        )
        ssm_client.put_parameter(
            Name="/notify/api/dvla_api_key",
            Value="some api key",
            Type="SecureString",
        )
        yield ssm_client


@pytest.fixture
def dvla_client(client, ssm):
    dvla_client = DVLAClient()
    dvla_client.init_app(region="eu-west-1", statsd_client=Mock())
    yield dvla_client


def test_set_ssm_creds_saves_in_cache(ssm):
    param = SSMParameter(key="/foo", ssm_client=ssm)

    today = datetime(2023, 1, 3, 12, 0, 1)

    assert param.last_read_at is None
    assert param._value is None

    with freezegun.freeze_time(today):
        param.set("new value")

    assert param._value == "new value"
    assert param.last_read_at == today


@pytest.mark.parametrize(
    "current_time, expected_value, expected_last_read_at",
    [
        (datetime(2023, 1, 3, 11, 59, 59), "old value", datetime(2023, 1, 2, 12, 0, 0)),
        (datetime(2023, 1, 3, 12, 0, 0), "new value", datetime(2023, 1, 3, 12, 0, 0)),
    ],
)
def test_get_ssm_creds_respects_ttl_when_fetching_value(ssm, current_time, expected_value, expected_last_read_at):
    param = SSMParameter(key="/foo", ssm_client=ssm)

    yesterday = datetime(2023, 1, 2, 12, 0, 0)

    param.last_read_at = yesterday
    param._value = "old value"

    ssm.put_parameter(Name="/foo", Value="new value", Type="SecureString")

    with freezegun.freeze_time(current_time):
        assert param.get() == expected_value

    assert param.last_read_at == expected_last_read_at


def test_get_ssm_creds_fetches_value_and_saves_in_cache_if_not_set(ssm):
    param = SSMParameter(key="/foo", ssm_client=ssm)
    ssm.put_parameter(Name="/foo", Value="bar", Type="SecureString")

    curr_time = datetime(2023, 1, 2, 12, 0, 0)

    assert param.last_read_at is None
    assert param._value is None

    with freezegun.freeze_time(curr_time):
        assert param.get() == "bar"

    assert param.last_read_at == curr_time
    assert param._value == "bar"


def test_get_ssm_creds(dvla_client, ssm):
    assert dvla_client.dvla_username.get() == "some username"
    assert dvla_client.dvla_password.get() == "some password"
    assert dvla_client.dvla_api_key.get() == "some api key"


def test_set_ssm_creds(dvla_client, ssm):
    dvla_client.dvla_username.set("some new username")
    dvla_client.dvla_password.set("some new password")
    dvla_client.dvla_api_key.set("some new api key")

    assert ssm.get_parameter(Name="/notify/api/dvla_username")["Parameter"]["Value"] == "some new username"
    assert ssm.get_parameter(Name="/notify/api/dvla_password")["Parameter"]["Value"] == "some new password"
    assert ssm.get_parameter(Name="/notify/api/dvla_api_key")["Parameter"]["Value"] == "some new api key"


def test_jwt_token_returns_jwt_if_set_and_not_expired_yet(dvla_client, rmock):
    curr_time = int(time.time())
    sample_token = jwt.encode(payload={"exp": curr_time + 3600}, key="foo")
    dvla_client._jwt_token = sample_token
    dvla_client._jwt_expires_at = curr_time + 3600

    assert dvla_client.jwt_token == sample_token


def test_jwt_token_calls_authenticate_if_not_set(dvla_client, rmock):
    assert dvla_client._jwt_token is None

    curr_time = int(time.time())
    sample_token = jwt.encode(payload={"exp": curr_time}, key="foo")

    endpoint = "https://test-dvla-api.com/thirdparty-access/v1/authenticate"
    mock_authenticate = rmock.request("POST", endpoint, json={"id-token": sample_token}, status_code=200)

    assert dvla_client.jwt_token == sample_token
    assert dvla_client._jwt_expires_at == int(time.time())

    # despite accessing value twice, we only called authenticate once
    assert mock_authenticate.called_once
    assert rmock.last_request.json() == {"userName": "some username", "password": "some password"}
    assert rmock.last_request.headers["Content-Type"] == "application/json"


def test_jwt_token_calls_authenticate_if_expiry_time_passed(dvla_client, rmock):
    prev_token_expiry_time = time.time()
    one_second_later = prev_token_expiry_time + 1
    one_hour_later = one_second_later + 3600

    old_token = jwt.encode(payload={"exp": prev_token_expiry_time}, key="foo")
    next_token = jwt.encode(payload={"exp": one_hour_later}, key="foo")

    dvla_client._jwt_token = jwt.encode(payload={"exp": prev_token_expiry_time}, key="foo")
    dvla_client._jwt_expires_at = prev_token_expiry_time

    endpoint = "https://test-dvla-api.com/thirdparty-access/v1/authenticate"
    mock_authenticate = rmock.request("POST", endpoint, json={"id-token": next_token}, status_code=200)

    with freezegun.freeze_time(datetime.fromtimestamp(one_second_later)):
        assert dvla_client.jwt_token != old_token
        assert dvla_client._jwt_expires_at == one_hour_later

    assert mock_authenticate.called_once


@pytest.mark.parametrize("_execution_number", range(100))
def test_generate_password_creates_passwords_that_meet_dvla_criteria(_execution_number):
    password = DVLAClient._generate_password()
    for character_set in (string.ascii_uppercase, string.ascii_lowercase, string.digits, string.punctuation):
        # assert the intersection of the character class, and the chars in the password is not empty to make sure
        # that all character classes are represented
        assert any(
            character in character_set for character in password
        ), f"{password} missing character from {character_set}"
    assert len(password) > 8


def test_change_password_calls_dvla(dvla_client, rmock, mocker):
    mocker.patch.object(dvla_client, "_generate_password", return_value="some new password")

    endpoint = "https://test-dvla-api.com/thirdparty-access/v1/password"
    mock_change_password = rmock.request(
        "POST", endpoint, json={"message": "Password successfully changed."}, status_code=200
    )

    dvla_client.change_password()

    assert mock_change_password.called_once is True
    assert rmock.last_request.json() == {
        "userName": "some username",
        "password": "some password",
        "newPassword": "some new password",
    }
    assert rmock.last_request.headers["Content-Type"] == "application/json"


def test_change_password_updates_ssm(dvla_client, rmock, mocker):
    mocker.patch.object(dvla_client, "_generate_password", return_value="some new password")
    mock_set_password = mocker.patch.object(dvla_client.dvla_password, "set")

    endpoint = "https://test-dvla-api.com/thirdparty-access/v1/password"
    rmock.request("POST", endpoint, json={"message": "Password successfully changed."}, status_code=200)

    dvla_client.change_password()

    mock_set_password.assert_called_once_with("some new password")


def test_change_password_does_not_update_ssm_if_dvla_throws_error(dvla_client, rmock, mocker):
    mocker.patch.object(dvla_client, "_generate_password", return_value="some new password")
    mock_set_password = mocker.patch.object(dvla_client.dvla_password, "set")

    endpoint = "https://test-dvla-api.com/thirdparty-access/v1/password"
    rmock.request("POST", endpoint, json={"message": "Unauthorized."}, status_code=401)

    with pytest.raises(HTTPError):
        dvla_client.change_password()

    assert mock_set_password.called is False


def test_change_password_raises_if_other_process_holds_lock(dvla_client, rmock, mocker):
    mocker.patch("notifications_utils.clients.redis.redis_client.StubLock.__enter__", side_effect=LockError)
    mock_set_password = mocker.patch.object(dvla_client.dvla_password, "set")

    with pytest.raises(LockError):
        dvla_client.change_password()

    assert rmock.called is False
    assert mock_set_password.called is False


def test_change_api_key_calls_dvla(dvla_client, rmock):
    endpoint = "https://test-dvla-api.com/thirdparty-access/v1/new-api-key"
    mock_change_api_key = rmock.request("POST", endpoint, json={"newApiKey": "some new api_key"}, status_code=200)
    dvla_client._jwt_token = "some jwt token"
    dvla_client._jwt_expires_at = sys.maxsize

    dvla_client.change_api_key()

    assert mock_change_api_key.called_once is True
    assert rmock.last_request.json() == {
        "userName": "some username",
        "password": "some password",
    }
    assert rmock.last_request.headers["x-api-key"] == "some api key"
    assert rmock.last_request.headers["Authorization"] == "some jwt token"
    assert rmock.last_request.headers["Content-Type"] == "application/json"


def test_change_api_key_updates_ssm(dvla_client, rmock, mocker):
    mock_set_api_key = mocker.patch.object(dvla_client.dvla_api_key, "set")
    dvla_client._jwt_token = "some jwt token"
    dvla_client._jwt_expires_at = sys.maxsize

    endpoint = "https://test-dvla-api.com/thirdparty-access/v1/new-api-key"
    rmock.request("POST", endpoint, json={"newApiKey": "some new api_key"}, status_code=200)

    dvla_client.change_api_key()

    mock_set_api_key.assert_called_once_with("some new api_key")


def test_change_api_key_does_not_update_ssm_if_dvla_throws_error(dvla_client, rmock, mocker):
    mock_set_api_key = mocker.patch.object(dvla_client.dvla_api_key, "set")
    dvla_client._jwt_token = "some jwt token"
    dvla_client._jwt_expires_at = sys.maxsize

    endpoint = "https://test-dvla-api.com/thirdparty-access/v1/new-api-key"
    rmock.request("POST", endpoint, json={"message": "Unauthorized"}, status_code=401)

    with pytest.raises(HTTPError):
        dvla_client.change_api_key()

    assert mock_set_api_key.called is False


def test_change_api_key_raises_if_other_process_holds_lock(dvla_client, rmock, mocker):
    mocker.patch("notifications_utils.clients.redis.redis_client.StubLock.__enter__", side_effect=LockError)
    mock_set_api_key = mocker.patch.object(dvla_client.dvla_api_key, "set")

    with pytest.raises(LockError):
        dvla_client.change_api_key()

    assert rmock.called is False
    assert mock_set_api_key.called is False
