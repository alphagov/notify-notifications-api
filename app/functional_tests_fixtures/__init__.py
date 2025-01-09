import os
import random
from datetime import datetime
from uuid import uuid4

import boto3
from flask import current_app
from sqlalchemy.exc import NoResultFound

from app.constants import (
    CAN_ASK_TO_JOIN_SERVICE,
    EDIT_FOLDER_PERMISSIONS,
    EMAIL_AUTH,
    EXTRA_LETTER_FORMATTING,
    INBOUND_SMS_TYPE,
    SECOND_CLASS,
    SEND_EMAILS,
    SEND_LETTERS,
    SEND_TEXTS,
    VIEW_ACTIVITY,
)
from app.dao.annual_billing_dao import (
    set_default_free_allowance_for_service,
)
from app.dao.api_key_dao import (
    get_model_api_keys,
    save_model_api_key,
)
from app.dao.inbound_numbers_dao import (
    dao_get_inbound_number_for_service,
    dao_set_inbound_number_to_service,
)
from app.dao.inbound_sms_dao import dao_create_inbound_sms, dao_get_inbound_sms_for_service
from app.dao.organisation_dao import (
    dao_create_organisation,
    dao_get_organisation_by_id,
    dao_get_organisations_by_partial_name,
    dao_update_organisation,
)
from app.dao.permissions_dao import permission_dao
from app.dao.service_email_reply_to_dao import (
    add_reply_to_email_address_for_service,
    dao_get_reply_to_by_service_id,
)
from app.dao.service_inbound_api_dao import get_service_inbound_api_for_service, save_service_inbound_api
from app.dao.service_letter_contact_dao import add_letter_contact_for_service, dao_get_letter_contacts_by_service_id
from app.dao.service_permissions_dao import (
    dao_add_service_permission,
    dao_fetch_service_permissions,
)
from app.dao.service_sms_sender_dao import (
    dao_add_sms_sender_for_service,
    dao_get_sms_senders_by_service_id,
)
from app.dao.services_dao import (
    dao_add_user_to_service,
    dao_create_service,
    dao_fetch_service_by_id,
    get_services_by_partial_name,
)
from app.dao.templates_dao import (
    dao_create_template,
    dao_get_all_templates_for_service,
    dao_get_template_versions,
    dao_update_template,
)
from app.dao.users_dao import get_user_by_email, save_model_user
from app.models import (
    InboundNumber,
    InboundSms,
    Organisation,
    Permission,
    Service,
    ServiceEmailReplyTo,
    ServiceInboundApi,
    User,
)
from app.schemas import api_key_schema, template_schema

"""
This function creates a set of database fixtures that functional tests use to run against.

It is intended for use in non-production environments only.

The function will create all the database rows required for the functional tests and output
an environment file that the functional tests can use to execute against the environment.

The environment file can be outputted to a file or uploaded to AWS SSM. The file is intended
for local testing, and SSM is for the pipeline.
"""


def apply_fixtures():
    functional_test_password = str(uuid4())
    functional_test_env_file = os.getenv("FUNCTIONAL_TEST_ENV_FILE", "/tmp/functional_test_env.sh")
    request_bin_api_token = os.getenv("REQUEST_BIN_API_TOKEN")
    environment = current_app.config["NOTIFY_ENVIRONMENT"]
    test_email_username = os.getenv("TEST_EMAIL_USERNAME", "notify-tests-preview")
    email_domain = "digital.cabinet-office.gov.uk"
    function_tests_govuk_key_name = "govuk_notify"
    function_tests_live_key_name = "functional_tests_service_live_key"
    function_tests_test_key_name = "functional_tests_service_test_key"
    govuk_service_id = current_app.config["NOTIFY_SERVICE_ID"]
    ssm_upload_path = os.getenv("SSM_UPLOAD_PATH")

    current_app.logger.info("Creating functional test fixtures for %s:", environment)

    current_app.logger.info("--> Ensure organisation exists")
    org = _create_organiation(email_domain)

    current_app.logger.info("--> Ensure users exists")
    func_test_user = _create_user(
        name="Functional Tests",
        email_address=f"{test_email_username}+{environment}@{email_domain}",
        password=functional_test_password,
        mobile_number="07700900001",
    )
    email_auth_user = _create_user(
        name="Functional Tests Email Auth",
        email_address=f"{test_email_username}+{environment}-email-auth@{email_domain}",
        password=functional_test_password,
        auth_type="email_auth",
    )
    service_admin_user = _create_user(
        name="Preview admin tests user",
        email_address=f"{test_email_username}+{environment}-admin-tests@{email_domain}",
        password=functional_test_password,
        organisations=[dao_get_organisation_by_id(org.id)],
        mobile_number="07700900501",
    )

    current_app.logger.info("--> Ensure service exists")

    for counter in range(301, 500):
        service2 = _create_service2(org.id, service_admin_user, uuid4())
        current_app.logger.info("%s %f", service2.id, counter)

    service = _create_service(org.id, service_admin_user)

    current_app.logger.info("--> Ensure users are added to service")
    dao_add_user_to_service(service, service_admin_user)
    dao_add_user_to_service(service, email_auth_user)

    _grant_permissions(service, email_auth_user)

    current_app.logger.info("--> Ensure api keys exists")
    api_key_notify = _create_api_key(function_tests_govuk_key_name, govuk_service_id, service_admin_user.id, "normal")
    api_key_live_key = _create_api_key(function_tests_live_key_name, service.id, service_admin_user.id, "normal")
    api_key_test_key = _create_api_key(function_tests_test_key_name, service.id, service_admin_user.id, "test")

    current_app.logger.info("--> Ensure inbound number exists")
    inbound_number_id = _create_inbound_numbers(service.id, service_admin_user.id)

    current_app.logger.info("--> Ensure service letter contact exists")
    letter_contact = _create_service_letter_contact(
        service.id,
        (
            "Government Digital Service\n"
            "The White Chapel Building\n"
            "10 Whitechapel High Street\n"
            "London\n"
            "E1 8QS\n"
            "United Kingdom"
        ),
        True,
    )

    email_template = _create_email_template(
        service=service,
        user_id=service_admin_user.id,
        name="Functional Tests - CSV Email Template with Build ID",
        subject="Functional Tests - CSV Email",
        content="The quick brown fox jumped over the lazy dog. Build id: ((build_id)).",
    )
    sms_template = _create_sms_template(
        service=service,
        user_id=service_admin_user.id,
        name="Functional Tests - CSV SMS Template with Build ID",
        content="The quick brown fox jumped over the lazy dog. Build id: ((build_id)).",
    )
    letter_template = _create_letter_template(
        service=service,
        user_id=service_admin_user.id,
        name="Functional Tests - CSV Letter Template with Build ID and Letter Contact",
        subject="Functional Tests - CSV Letter",
        content="The quick brown fox jumped over the lazy dog. Build id: ((build_id)).",
        letter_contact_id=letter_contact.id,
    )

    api_client_integration_test_email_template = _create_email_template(
        service=service,
        user_id=service_admin_user.id,
        name="Client Functional test email template",
        subject="Functional Tests are good",
        content="Functional test help make our world a better place",
    )
    api_client_integration_test_sms_template = _create_sms_template(
        service=service,
        user_id=service_admin_user.id,
        name="Example text message template",
        content="Hey ((name)), I’m trying out Notify. Today is ((day of week)) and my favourite colour is ((colour)).",
    )
    api_client_integration_test_letter_template = _create_letter_template(
        service=service,
        user_id=service_admin_user.id,
        name="Untitled",
        subject="Main heading",
        content="Body",
    )

    if len(dao_get_template_versions(service.id, api_client_integration_test_email_template.id)) < 2:
        _update_template(
            api_client_integration_test_email_template,
            content="Hello ((name))\r\n\r\nFunctional test help make our world a better place",
        )

    if len(dao_get_template_versions(service.id, api_client_integration_test_sms_template.id)) < 2:
        _update_template(
            api_client_integration_test_sms_template,
            name="Client Functional test sms template",
            content="Hello ((name))\r\n\r\nFunctional Tests make our world a better place",
        )

    if len(dao_get_template_versions(service.id, api_client_integration_test_letter_template.id)) < 2:
        _update_template(
            api_client_integration_test_letter_template,
            name="Client functional letter template",
            content="Hello ((address_line_1))",
            letter_contact_id=letter_contact.id,
        )

    current_app.logger.info("--> Ensure service email reply to exists")
    _create_service_email_reply_to(
        service.id, f"{test_email_username}+{environment}-reply-to-default@{email_domain}", True
    )
    email_reply_to = _create_service_email_reply_to(
        service.id, f"{test_email_username}+{environment}-reply-to@{email_domain}", False
    )

    current_app.logger.info("--> Ensure service permissions exists")
    _create_service_permissions(service.id)

    current_app.logger.info("--> Ensure service sms senders exists")
    _create_service_sms_senders(service.id, "07700900500", True, inbound_number_id)
    sms_sender = _create_service_sms_senders(service.id, "func tests", False, None)

    current_app.logger.info("--> Ensure service inbound api exists")
    _create_service_inbound_api(service.id, service_admin_user.id)

    current_app.logger.info("--> Ensure dummy inbound SMS objects exist")
    _create_inbound_sms(service, 3)

    functional_test_config = f"""

# Functional test environment

export FUNCTIONAL_TESTS_API_HOST='{current_app.config['API_HOST_NAME']}'
export FUNCTIONAL_TESTS_ADMIN_HOST='{current_app.config['ADMIN_BASE_URL']}'

export ENVIRONMENT='{current_app.config['NOTIFY_ENVIRONMENT']}'

export FUNCTIONAL_TEST_EMAIL='{func_test_user.email_address}'
export FUNCTIONAL_TEST_PASSWORD='{functional_test_password}'
export TEST_NUMBER='07700900001'

export NOTIFY_SERVICE_API_KEY='{function_tests_govuk_key_name}-{govuk_service_id}-{api_key_notify.secret}'

export FUNCTIONAL_TESTS_SERVICE_EMAIL='{service_admin_user.email_address}'
export FUNCTIONAL_TESTS_SERVICE_EMAIL_AUTH_ACCOUNT='{email_auth_user.email_address}'
export FUNCTIONAL_TESTS_SERVICE_EMAIL_PASSWORD='{functional_test_password}'
export FUNCTIONAL_TESTS_SERVICE_NUMBER='07700900501'

export FUNCTIONAL_TESTS_SERVICE_ID='{service.id}'
export FUNCTIONAL_TESTS_SERVICE_NAME='{service.name}'
export FUNCTIONAL_TESTS_ORGANISATION_ID='{org.id}'
export FUNCTIONAL_TESTS_SERVICE_API_KEY='{function_tests_live_key_name}-{service.id}-{api_key_live_key.secret}'
export FUNCTIONAL_TESTS_SERVICE_API_TEST_KEY='{function_tests_test_key_name}-{service.id}-{api_key_test_key.secret}'
export FUNCTIONAL_TESTS_API_AUTH_SECRET='{current_app.config['INTERNAL_CLIENT_API_KEYS']['notify-functional-tests'][0]}'

export FUNCTIONAL_TESTS_SERVICE_EMAIL_REPLY_TO='{test_email_username}+{environment}-reply-to@{email_domain}'
export FUNCTIONAL_TESTS_SERVICE_INBOUND_NUMBER='07700900500'

export FUNCTIONAL_TEST_SMS_TEMPLATE_ID='{sms_template.id}'
export FUNCTIONAL_TEST_EMAIL_TEMPLATE_ID='{email_template.id}'
export FUNCTIONAL_TEST_LETTER_TEMPLATE_ID='{letter_template.id}'

export MMG_INBOUND_SMS_USERNAME='{current_app.config['MMG_INBOUND_SMS_USERNAME'][0]}'
export MMG_INBOUND_SMS_AUTH='{current_app.config['MMG_INBOUND_SMS_AUTH'][0]}'

export REQUEST_BIN_API_TOKEN='{request_bin_api_token}'


# API client integration test environment

export SERVICE_ID='{service.id}'
export FUNCTIONAL_TEST_EMAIL='{func_test_user.email_address}'
export FUNCTIONAL_TEST_NUMBER='07700900500'
export EMAIL_TEMPLATE_ID='{api_client_integration_test_email_template.id}'
export SMS_TEMPLATE_ID='{api_client_integration_test_sms_template.id}'
export LETTER_TEMPLATE_ID='{api_client_integration_test_letter_template.id}'
export EMAIL_REPLY_TO_ID='{email_reply_to.id}'
export SMS_SENDER_ID='{sms_sender.id}'
export NOTIFY_API_URL='{current_app.config['API_HOST_NAME']}'

export API_KEY='{function_tests_test_key_name}-{service.id}-{api_key_test_key.secret}'
export API_SENDING_KEY='{function_tests_live_key_name}-{service.id}-{api_key_live_key.secret}'
export INBOUND_SMS_QUERY_KEY='{function_tests_test_key_name}-{service.id}-{api_key_test_key.secret}'

"""

    if functional_test_env_file != "":
        with open(functional_test_env_file, "w") as f:
            f.write(functional_test_config)

    if ssm_upload_path:
        ssm = boto3.client("ssm")
        response = ssm.put_parameter(
            Name=ssm_upload_path,
            Value=functional_test_config,
            Type="SecureString",
            Overwrite=True,
        )

        if response["ResponseMetadata"]["HTTPStatusCode"] != 200:
            raise Exception("Failed to upload to SSM")

    current_app.logger.info("--> Functional test fixtures completed successfully")


def _create_user(name, email_address, password, auth_type="sms_auth", organisations=None, mobile_number=None):
    if organisations is None:
        organisations = []
    user = None
    try:
        user = get_user_by_email(email_address)
    except NoResultFound:
        user = User()
        user.name = name
        user.email_address = email_address
        user.mobile_number = mobile_number
        user.auth_type = auth_type

        user.organisations = organisations

        save_model_user(user, password=password, validated_email_access=True)

    user.state = "active"
    user.auth_type = auth_type
    save_model_user(user, password=password)

    return user


def _create_organiation(email_domain, org_name="Functional Tests Org"):

    organisations = dao_get_organisations_by_partial_name(org_name)

    org = None
    for organisation in organisations:
        if organisation.name == org_name:
            org = organisation

    if org is None:

        org = Organisation(name=org_name, active=True, crown=False, organisation_type="central")

        dao_create_organisation(org)

    org.set_permissions_list([CAN_ASK_TO_JOIN_SERVICE])
    dao_update_organisation(org.id, domains=[email_domain], can_approve_own_go_live_requests=True)

    return org


def _create_service2(org_id, user, counter):

    data = {
        "name": f"Functional Tests {counter}",
        "restricted": False,
        "organisation_id": org_id,
        "organisation_type": "central",
        "created_by": user.id,
        "sms_message_limit": 1000,
        "letter_message_limit": 1000,
        "email_message_limit": 1000,
        "contact_link": current_app.config["ADMIN_BASE_URL"],
        "go_live_at": "2017-04-01 11:09:00.061258",
    }
    service = Service.from_json(data)
    dao_create_service(service, user)

    set_default_free_allowance_for_service(service=service, year_start=None)

    return service


def _create_service(org_id, user, service_name="Functional Tests"):

    services = get_services_by_partial_name(service_name)

    service = None
    for s in services:
        if s.name == service_name:
            service = s

    if service is None:

        service = Service.from_json(
            {
                "name": service_name,
                "restricted": False,
                "organisation_id": org_id,
                "organisation_type": "central",
                "created_by": user.id,
                "sms_message_limit": 1000,
                "letter_message_limit": 1000,
                "email_message_limit": 1000,
                "contact_link": current_app.config["ADMIN_BASE_URL"],
            }
        )
        dao_create_service(service, user)

    set_default_free_allowance_for_service(service=service, year_start=None)

    return service


def _grant_permissions(service, user):

    permission_list = [
        Permission(service_id=service.id, user_id=user.id, permission=SEND_LETTERS),
        Permission(service_id=service.id, user_id=user.id, permission=VIEW_ACTIVITY),
        Permission(service_id=service.id, user_id=user.id, permission=SEND_TEXTS),
        Permission(service_id=service.id, user_id=user.id, permission=SEND_EMAILS),
    ]

    permission_dao.set_user_service_permission(user, service, permission_list, _commit=True, replace=True)


def _create_api_key(name, service_id, user_id, key_type="normal"):

    api_keys = get_model_api_keys(service_id=service_id)
    for key in api_keys:
        if key.name == name:
            return key

    request = {"created_by": user_id, "key_type": key_type, "name": name}

    fetched_service = dao_fetch_service_by_id(service_id=service_id)
    valid_api_key = api_key_schema.load(request)
    valid_api_key.service = fetched_service
    save_model_api_key(valid_api_key)

    return valid_api_key


def _create_inbound_numbers(service_id, user_id, number="07700900", provider="mmg"):

    inbound_number = dao_get_inbound_number_for_service(service_id=service_id)

    if inbound_number is not None:
        return inbound_number.id

    inbound_number = InboundNumber()
    inbound_number.number = number + str(random.randrange(1000))
    inbound_number.provider = provider
    inbound_number.service_id = service_id
    inbound_number.active = True
    inbound_number.created_by = user_id

    dao_set_inbound_number_to_service(service_id, inbound_number)

    return inbound_number.id


def _create_service_letter_contact(service_id, contact_block, is_default):

    letter_contacts = dao_get_letter_contacts_by_service_id(service_id)

    for letter_contact in letter_contacts:
        if letter_contact.contact_block == contact_block:
            return letter_contact

    return add_letter_contact_for_service(service_id, contact_block, is_default)


def _create_email_template(service, user_id, name, subject, content):

    templates = dao_get_all_templates_for_service(service_id=service.id)

    for template in templates:
        if template.name == name:
            return template

    new_template = template_schema.load(
        {
            "name": name,
            "template_type": "email",
            "content": content,
            "subject": subject,
            "created_by": user_id,
        }
    )

    new_template.service = service

    dao_create_template(new_template)

    return new_template


def _create_sms_template(service, user_id, name, content):

    templates = dao_get_all_templates_for_service(service_id=service.id)

    for template in templates:
        if template.name == name:
            return template

    new_template = template_schema.load(
        {
            "name": name,
            "template_type": "sms",
            "content": content,
            "created_by": user_id,
        }
    )

    new_template.service = service

    dao_create_template(new_template)

    return new_template


def _create_letter_template(service, user_id, name, subject, content, letter_contact_id=None):

    templates = dao_get_all_templates_for_service(service_id=service.id)

    for template in templates:
        if template.name == name:
            return template

    new_template = template_schema.load(
        {
            "name": name,
            "template_type": "letter",
            "content": content,
            "subject": subject,
            "created_by": user_id,
        }
    )

    new_template.service = service
    new_template.postage = SECOND_CLASS
    new_template.service_letter_contact_id = letter_contact_id

    dao_create_template(new_template)

    return new_template


def _update_template(template, name=None, subject=None, content=None, letter_contact_id=None):
    if name is not None:
        template.name = name

    if subject is not None:
        template.subject = subject

    if content is not None:
        template.content = content

    if letter_contact_id is not None:
        template.service_letter_contact_id = letter_contact_id

    dao_update_template(template)


def _create_service_email_reply_to(service_id, email_address, is_default):

    service_email_reply_tos = dao_get_reply_to_by_service_id(service_id=service_id)

    for service_email_reply_to in service_email_reply_tos:
        if service_email_reply_to.email_address == email_address:
            return service_email_reply_to

    service_email_reply_to = ServiceEmailReplyTo()

    service_email_reply_to.service_id = service_id
    service_email_reply_to.is_default = is_default
    service_email_reply_to.email_address = email_address

    return add_reply_to_email_address_for_service(service_id, email_address, is_default)


def _create_service_permissions(service_id, permissions=None):
    if permissions is None:
        permissions = [EDIT_FOLDER_PERMISSIONS, EXTRA_LETTER_FORMATTING, INBOUND_SMS_TYPE, EMAIL_AUTH]

    service_permissions = dao_fetch_service_permissions(service_id)

    for permission in permissions:
        found = False
        for service_permission in service_permissions:
            if service_permission.permission == permission:
                found = True

        if not found:
            dao_add_service_permission(service_id, permission)


def _create_service_sms_senders(service_id, sms_sender, is_default, inbound_number_id):

    service_sms_senders = dao_get_sms_senders_by_service_id(service_id)

    for service_sms_sender in service_sms_senders:
        if service_sms_sender.sms_sender == sms_sender:
            return service_sms_sender

    return dao_add_sms_sender_for_service(service_id, sms_sender, is_default, inbound_number_id)


def _create_service_inbound_api(service_id, user_id):

    inbound_api = get_service_inbound_api_for_service(service_id)

    if inbound_api is None:
        inbound_api = ServiceInboundApi(
            service_id=service_id, url="https://5c6b93352e82dab5d82d02e5178c2d57.m.pipedream.net", updated_by_id=user_id
        )

    inbound_api.bearer_token = "1234567890"

    save_service_inbound_api(inbound_api)


def _create_inbound_sms(service, count):

    num_existing = len(dao_get_inbound_sms_for_service(service.id, limit=count))

    for _ in range(num_existing, count):
        dao_create_inbound_sms(
            InboundSms(
                service=service,
                notify_number=service.get_inbound_number(),
                user_number="00000000000",
                provider_date=datetime.utcnow(),
                provider_reference="SOME-MMG-SPECIFIC-ID",
                content="This is a test message",
                provider="mmg",
            )
        )
