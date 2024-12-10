import json
import random
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta

import requests
from flask import current_app, jsonify
from notifications_utils.local_vars import LazyLocalGetter
from notifications_utils.s3 import s3upload
from werkzeug.local import LocalProxy

from app import memo_resetters, notify_celery, signing
from app.aws.s3 import file_exists
from app.celery.process_ses_receipts_tasks import process_ses_results
from app.config import QueueNames
from app.constants import SMS_TYPE

# thread-local copies of persistent requests.Session
_requests_session_context_var: ContextVar[requests.Session] = ContextVar("research_mode_requests_session")
get_requests_session: LazyLocalGetter[requests.Session] = LazyLocalGetter(
    _requests_session_context_var,
    lambda: requests.Session(),
)
memo_resetters.append(lambda: get_requests_session.clear())
requests_session = LocalProxy(get_requests_session)

temp_fail = "7700900003"
perm_fail = "7700900002"
delivered = "7700900001"

delivered_email = "delivered@simulator.notify"
perm_fail_email = "perm-fail@simulator.notify"
temp_fail_email = "temp-fail@simulator.notify"


def send_sms_response(provider, reference, to):
    if provider == "mmg":
        body = mmg_callback(reference, to)
        headers = {"Content-type": "application/json"}
    else:
        headers = {"Content-type": "application/x-www-form-urlencoded"}
        body = firetext_callback(reference, to)
        # to simulate getting a temporary_failure from firetext
        # we need to send a pending status updated then a permanent-failure
        if body["status"] == "2":  # pending status
            make_request(SMS_TYPE, provider, body, headers)
            # 1 is a declined status for firetext, will result in a temp-failure
            body = {"mobile": to, "status": "1", "time": "2016-03-10 14:17:00", "reference": reference}

    make_request(SMS_TYPE, provider, body, headers)


def send_email_response(reference, to):
    if to == perm_fail_email:
        body = ses_hard_bounce_callback(reference)
    elif to == temp_fail_email:
        body = ses_soft_bounce_callback(reference)
    else:
        body = ses_notification_callback(reference)

    process_ses_results.apply_async([body], queue=QueueNames.RESEARCH_MODE)


def send_letter_response(notification_id: uuid.UUID, billable_units: int, postage: str):
    signed_notification_id = signing.encode(str(notification_id))
    api_call = (
        f"{current_app.config['API_HOST_NAME_INTERNAL']}/notifications/letter/status?token={signed_notification_id}"
    )

    headers = {"Content-type": "application/json"}
    data = _create_fake_letter_callback_data(notification_id, billable_units, postage)

    try:
        response = requests_session.request("POST", api_call, headers=headers, data=json.dumps(data), timeout=30)
        response.raise_for_status()
    except requests.HTTPError as e:
        current_app.logger.error("API POST request on %s failed with status %s", api_call, e.response.status_code)
        raise e
    finally:
        current_app.logger.info("Mocked letter callback request for %s finished", notification_id)

    return jsonify(result="success"), 200


def _create_fake_letter_callback_data(notification_id: uuid.UUID, billable_units: int, postage: str):
    if postage == "first":
        postage = "1ST"
        mailing_product = "UNCODED"
    elif postage == "second":
        postage = "2ND"
        mailing_product = "MM"
    elif postage == "europe":
        postage = "INTERNATIONAL"
        mailing_product = "INT EU"
    else:
        postage = "INTERNATIONAL"
        mailing_product = "INT ROW"

    return {
        "id": "1234",
        "source": "dvla:resource:osl:print:print-hub-fulfilment:5.18.0",
        "specVersion": "1",
        "type": "uk.gov.dvla.osl.osldatadictionaryschemas.print.messages.v2.PrintJobStatus",
        "time": "2024-04-01T00:00:00Z",
        "dataContentType": "application/json",
        "dataSchema": "https://osl-data-dictionary-schemas.engineering.dvla.gov.uk/print/messages/v2/print-job-status.json",
        "data": {
            "despatchProperties": [
                {"key": "totalSheets", "value": str(billable_units)},
                {"key": "postageClass", "value": postage},
                {"key": "mailingProduct", "value": mailing_product},
                {"key": "productionRunDate", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")},
            ],
            "jobId": str(notification_id),
            "jobType": "NOTIFY",
            "jobStatus": "DESPATCHED",
            "templateReference": "NOTIFY",
        },
        "metadata": {
            "handler": {"urn": "dvla:resource:osl:print:print-hub-fulfilment:5.18.0"},
            "origin": {"urn": "dvla:resource:osg:dev:printhub:1.0.1"},
            "correlationId": "b5d9b2bd-6e8f-4275-bdd3-c8086fe09c52",
        },
    }


def make_request(notification_type, provider, data, headers):
    api_call = f"{current_app.config['API_HOST_NAME_INTERNAL']}/notifications/{notification_type}/{provider}"

    try:
        response = requests_session.request("POST", api_call, headers=headers, data=data, timeout=60)
        response.raise_for_status()
    except requests.HTTPError as e:
        current_app.logger.error("API POST request on %s failed with status %s", api_call, e.response.status_code)
        raise e
    finally:
        current_app.logger.info("Mocked provider callback request finished")
    return response.json()


def mmg_callback(notification_id, to):
    """
    status: 3 - delivered
    status: 4 - expired (temp failure)
    status: 5 - rejected (perm failure)
    """

    if to.strip().endswith(temp_fail):
        status = "4"
    elif to.strip().endswith(perm_fail):
        status = "5"
    else:
        status = "3"

    return json.dumps(
        {
            "reference": "mmg_reference",
            "CID": str(notification_id),
            "MSISDN": to,
            "status": status,
            "deliverytime": "2016-04-05 16:01:07",
        }
    )


def firetext_callback(notification_id, to):
    """
    status: 0 - delivered
    status: 1 - perm failure
    """
    if to.strip().endswith(perm_fail):
        status = "1"
    elif to.strip().endswith(temp_fail):
        status = "2"
    else:
        status = "0"
    return {"mobile": to, "status": status, "time": "2016-03-10 14:17:00", "reference": notification_id}


@notify_celery.task(bind=True, name="create-fake-letter-response-file", max_retries=5, default_retry_delay=300)
def create_fake_letter_response_file(self, reference):
    now = datetime.utcnow()
    dvla_response_data = f"{reference}|Sent|0|Sorted|{now.date().isoformat()}"

    # try and find a filename that hasn't been taken yet - from a random time within the last 30 seconds
    for i in sorted(range(30), key=lambda _: random.random()):
        upload_file_name = "NOTIFY-{}-RSP.TXT".format((now - timedelta(seconds=i)).strftime("%Y%m%d%H%M%S"))
        if not file_exists(current_app.config["S3_BUCKET_DVLA_RESPONSE"], upload_file_name):
            break
    else:
        raise ValueError(
            f"cant create fake letter response file for {reference} - too many files for that time already exist on s3"
        )

    s3upload(
        filedata=dvla_response_data,
        region=current_app.config["AWS_REGION"],
        bucket_name=current_app.config["S3_BUCKET_DVLA_RESPONSE"],
        file_location=upload_file_name,
    )
    current_app.logger.info(
        "Fake DVLA response file %s, content [%s], uploaded to %s, created at %s",
        upload_file_name,
        dvla_response_data,
        current_app.config["S3_BUCKET_DVLA_RESPONSE"],
        now,
    )

    # on development we can't trigger SNS callbacks so we need to manually hit the DVLA callback endpoint
    if current_app.config["NOTIFY_ENVIRONMENT"] == "development":
        make_request("letter", "dvla", _fake_sns_s3_callback(upload_file_name), None)


def _fake_sns_s3_callback(filename):
    message_contents = '{"Records":[{"s3":{"object":{"key":"%s"}}}]}' % (filename)  # noqa
    return json.dumps({"Type": "Notification", "MessageId": "some-message-id", "Message": message_contents})


@notify_celery.task(bind=True, name="create-fake-letter-callback", max_retries=3, default_retry_delay=60)
def create_fake_letter_callback(self, notification_id: uuid.UUID, billable_units: int, postage: str):
    try:
        send_letter_response(notification_id, billable_units, postage)
    except Exception:
        try:
            self.retry()
        except self.MaxRetriesExceededError:
            current_app.logger.warning("Fake letter callback cound not be created for %s", notification_id)


def ses_notification_callback(reference):
    ses_message_body = {
        "delivery": {
            "processingTimeMillis": 2003,
            "recipients": ["success@simulator.amazonses.com"],
            "remoteMtaIp": "123.123.123.123",
            "reportingMTA": "a7-32.smtp-out.eu-west-1.amazonses.com",
            "smtpResponse": "250 2.6.0 Message received",
            "timestamp": "2017-11-17T12:14:03.646Z",
        },
        "mail": {
            "commonHeaders": {
                "from": ["TEST <TEST@notify.works>"],
                "subject": "lambda test",
                "to": ["success@simulator.amazonses.com"],
            },
            "destination": ["success@simulator.amazonses.com"],
            "headers": [
                {"name": "From", "value": "TEST <TEST@notify.works>"},
                {"name": "To", "value": "success@simulator.amazonses.com"},
                {"name": "Subject", "value": "lambda test"},
                {"name": "MIME-Version", "value": "1.0"},
                {
                    "name": "Content-Type",
                    "value": 'multipart/alternative; boundary="----=_Part_617203_1627511946.1510920841645"',
                },
            ],
            "headersTruncated": False,
            "messageId": reference,
            "sendingAccountId": "12341234",
            "source": '"TEST" <TEST@notify.works>',
            "sourceArn": "arn:aws:ses:eu-west-1:12341234:identity/notify.works",
            "sourceIp": "0.0.0.1",
            "timestamp": "2017-11-17T12:14:01.643Z",
        },
        "notificationType": "Delivery",
    }

    return {
        "Type": "Notification",
        "MessageId": "8e83c020-1234-1234-1234-92a8ee9baa0a",
        "TopicArn": "arn:aws:sns:eu-west-1:12341234:ses_notifications",
        "Subject": None,
        "Message": json.dumps(ses_message_body),
        "Timestamp": "2017-11-17T12:14:03.710Z",
        "SignatureVersion": "1",
        "Signature": "[REDACTED]",
        "SigningCertUrl": "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-[REDACTED].pem",
        "UnsubscribeUrl": "https://sns.eu-west-1.amazonaws.com/?Action=Unsubscribe&SubscriptionArn=[REACTED]",
        "MessageAttributes": {},
    }


def ses_hard_bounce_callback(reference):
    return _ses_bounce_callback(reference, "Permanent")


def ses_soft_bounce_callback(reference):
    return _ses_bounce_callback(reference, "Temporary")


def _ses_bounce_callback(reference, bounce_type):
    ses_message_body = {
        "bounce": {
            "bounceSubType": "General",
            "bounceType": bounce_type,
            "bouncedRecipients": [
                {
                    "action": "failed",
                    "diagnosticCode": "smtp; 550 5.1.1 user unknown",
                    "emailAddress": "bounce@simulator.amazonses.com",
                    "status": "5.1.1",
                }
            ],
            "feedbackId": "0102015fc9e676fb-12341234-1234-1234-1234-9301e86a4fa8-000000",
            "remoteMtaIp": "123.123.123.123",
            "reportingMTA": "dsn; a7-31.smtp-out.eu-west-1.amazonses.com",
            "timestamp": "2017-11-17T12:14:05.131Z",
        },
        "mail": {
            "commonHeaders": {
                "from": ["TEST <TEST@notify.works>"],
                "subject": "ses callback test",
                "to": ["bounce@simulator.amazonses.com"],
            },
            "destination": ["bounce@simulator.amazonses.com"],
            "headers": [
                {"name": "From", "value": "TEST <TEST@notify.works>"},
                {"name": "To", "value": "bounce@simulator.amazonses.com"},
                {"name": "Subject", "value": "lambda test"},
                {"name": "MIME-Version", "value": "1.0"},
                {
                    "name": "Content-Type",
                    "value": 'multipart/alternative; boundary="----=_Part_596529_2039165601.1510920843367"',
                },
            ],
            "headersTruncated": False,
            "messageId": reference,
            "sendingAccountId": "12341234",
            "source": '"TEST" <TEST@notify.works>',
            "sourceArn": "arn:aws:ses:eu-west-1:12341234:identity/notify.works",
            "sourceIp": "0.0.0.1",
            "timestamp": "2017-11-17T12:14:03.000Z",
        },
        "notificationType": "Bounce",
    }
    return {
        "Type": "Notification",
        "MessageId": "36e67c28-1234-1234-1234-2ea0172aa4a7",
        "TopicArn": "arn:aws:sns:eu-west-1:12341234:ses_notifications",
        "Subject": None,
        "Message": json.dumps(ses_message_body),
        "Timestamp": "2017-11-17T12:14:05.149Z",
        "SignatureVersion": "1",
        "Signature": "[REDACTED]",
        "SigningCertUrl": "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-[REDACTED]].pem",
        "UnsubscribeUrl": "https://sns.eu-west-1.amazonaws.com/?Action=Unsubscribe&SubscriptionArn=[REDACTED]]",
        "MessageAttributes": {},
    }
