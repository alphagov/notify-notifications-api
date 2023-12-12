import random
import uuid
from datetime import date, datetime, timedelta

from app import db
from app.constants import (
    EMAIL_TYPE,
    KEY_TYPE_NORMAL,
    LETTER_TYPE,
    MOBILE_TYPE,
    SMS_TYPE,
)
from app.dao import fact_processing_time_dao
from app.dao.email_branding_dao import dao_create_email_branding
from app.dao.inbound_sms_dao import dao_create_inbound_sms
from app.dao.invited_org_user_dao import save_invited_org_user
from app.dao.invited_user_dao import save_invited_user
from app.dao.jobs_dao import dao_create_job
from app.dao.notifications_dao import dao_create_notification
from app.dao.organisation_dao import (
    dao_add_service_to_organisation,
    dao_create_organisation,
)
from app.dao.permissions_dao import permission_dao
from app.dao.service_callback_api_dao import save_service_callback_api
from app.dao.service_data_retention_dao import insert_service_data_retention
from app.dao.service_inbound_api_dao import save_service_inbound_api
from app.dao.service_permissions_dao import dao_add_service_permission
from app.dao.service_sms_sender_dao import (
    dao_update_service_sms_sender,
    update_existing_sms_sender_with_inbound_number,
)
from app.dao.services_dao import dao_add_user_to_service, dao_create_service
from app.dao.templates_dao import dao_create_template, dao_update_template
from app.dao.unsubscribe_request_dao import create_unsubscribe_request_dao, create_unsubscribe_request_reports_dao
from app.dao.users_dao import save_model_user
from app.models import (
    AnnualBilling,
    ApiKey,
    Complaint,
    DailySortedLetter,
    Domain,
    EmailBranding,
    FactBilling,
    FactNotificationStatus,
    FactProcessingTime,
    InboundNumber,
    InboundSms,
    InvitedOrganisationUser,
    InvitedUser,
    Job,
    LetterAttachment,
    LetterBranding,
    LetterRate,
    Notification,
    NotificationHistory,
    Organisation,
    Permission,
    Rate,
    ReturnedLetter,
    Service,
    ServiceCallbackApi,
    ServiceContactList,
    ServiceEmailReplyTo,
    ServiceGuestList,
    ServiceInboundApi,
    ServiceLetterContact,
    ServicePermission,
    ServiceSmsSender,
    Template,
    TemplateFolder,
    UnsubscribeRequestReport,
    User,
    WebauthnCredential,
)


def create_user(*, mobile_number="+447700900986", email=None, state="active", id_=None, name="Test User", **kwargs):
    data = {
        "id": id_ or uuid.uuid4(),
        "name": name,
        "email_address": email or f"{uuid.uuid4()}@digital.cabinet-office.gov.uk",
        "password": "password",
        "mobile_number": mobile_number,
        "state": state,
        **kwargs,
    }
    user = User.query.filter_by(email_address=email).first()
    if not user:
        user = User(**data)
    save_model_user(user, validated_email_access=True)
    return user


def create_permissions(user, service, *permissions):
    permissions = [Permission(service_id=service.id, user_id=user.id, permission=p) for p in permissions]

    permission_dao.set_user_service_permission(user, service, permissions, _commit=True)


def create_service(
    user=None,
    service_name="Sample service",
    service_id=None,
    restricted=False,
    count_as_live=True,
    service_permissions=None,
    active=True,
    prefix_sms=True,
    email_message_limit=1000,
    sms_message_limit=1000,
    letter_message_limit=1000,
    organisation_type="central",
    check_if_service_exists=False,
    go_live_user=None,
    go_live_at=None,
    crown=True,
    organisation=None,
    purchase_order_number=None,
    billing_contact_names=None,
    billing_contact_email_addresses=None,
    billing_reference=None,
    contact_link=None,
):
    if check_if_service_exists:
        service = Service.query.filter_by(name=service_name).first()
    if (not check_if_service_exists) or (check_if_service_exists and not service):
        service = Service(
            name=service_name,
            email_message_limit=email_message_limit,
            sms_message_limit=sms_message_limit,
            letter_message_limit=letter_message_limit,
            restricted=restricted,
            custom_email_sender_name=None,
            created_by=user if user else create_user(email=f"{uuid.uuid4()}@digital.cabinet-office.gov.uk"),
            prefix_sms=prefix_sms,
            organisation_type=organisation_type,
            organisation=organisation,
            go_live_user=go_live_user,
            go_live_at=go_live_at,
            crown=crown,
            purchase_order_number=purchase_order_number,
            billing_contact_names=billing_contact_names,
            billing_contact_email_addresses=billing_contact_email_addresses,
            billing_reference=billing_reference,
            contact_link=contact_link,
        )
        if service_id:
            service.id = service_id
        dao_create_service(
            service,
            service.created_by,
            service_permissions=service_permissions,
        )

        service.active = active
        service.count_as_live = count_as_live
    else:
        if user and user not in service.users:
            dao_add_user_to_service(service, user)

    return service


def create_service_with_inbound_number(inbound_number="1234567", *args, **kwargs):
    service = create_service(*args, **kwargs)

    sms_sender = ServiceSmsSender.query.filter_by(service_id=service.id).first()
    inbound = create_inbound_number(number=inbound_number, service_id=service.id)
    update_existing_sms_sender_with_inbound_number(
        service_sms_sender=sms_sender, sms_sender=inbound_number, inbound_number_id=inbound.id
    )

    return service


def create_service_with_defined_sms_sender(sms_sender_value="1234567", *args, **kwargs):
    service = create_service(*args, **kwargs)

    sms_sender = ServiceSmsSender.query.filter_by(service_id=service.id).first()
    dao_update_service_sms_sender(
        service_id=service.id, service_sms_sender_id=sms_sender.id, is_default=True, sms_sender=sms_sender_value
    )

    return service


def create_template(
    service,
    template_type=SMS_TYPE,
    template_name=None,
    has_unsubscribe_link=False,
    subject="Template subject",
    content="Dear Sir/Madam, Hello. Yours Truly, The Government.",
    reply_to=None,
    hidden=False,
    archived=False,
    folder=None,
    postage=None,
    process_type="normal",
    contact_block_id=None,
):
    data = {
        "name": template_name or f"{template_type} Template Name",
        "template_type": template_type,
        "has_unsubscribe_link": has_unsubscribe_link,
        "content": content,
        "service": service,
        "created_by": service.created_by,
        "reply_to": reply_to,
        "hidden": hidden,
        "folder": folder,
        "process_type": process_type,
    }
    if template_type == LETTER_TYPE:
        data["postage"] = postage or "second"
        if contact_block_id:
            data["service_letter_contact_id"] = contact_block_id
    if template_type != SMS_TYPE:
        data["subject"] = subject
    template = Template(**data)
    dao_create_template(template)

    if archived:
        template.archived = archived
        dao_update_template(template)

    return template


def create_notification(
    template=None,
    job=None,
    job_row_number=None,
    to_field=None,
    status="created",
    reference=None,
    created_at=None,
    sent_at=None,
    updated_at=None,
    billable_units=1,
    personalisation=None,
    api_key=None,
    key_type=KEY_TYPE_NORMAL,
    sent_by=None,
    client_reference=None,
    rate_multiplier=None,
    international=False,
    phone_prefix=None,
    normalised_to=None,
    one_off=False,
    reply_to_text=None,
    created_by_id=None,
    postage=None,
    document_download_count=None,
    unsubscribe_link=None,
):
    assert job or template
    if job:
        template = job.template

    if created_at is None:
        created_at = datetime.utcnow()

    if to_field is None:
        to_field = "+447700900855" if template.template_type == SMS_TYPE else "test@example.com"

    if status not in ("created", "validation-failed", "virus-scan-failed", "pending-virus-check"):
        sent_at = sent_at or datetime.utcnow()
        updated_at = updated_at or datetime.utcnow()

    if not one_off and (job is None and api_key is None):
        # we did not specify in test - lets create it
        api_key = ApiKey.query.filter(ApiKey.service == template.service, ApiKey.key_type == key_type).first()
        if not api_key:
            api_key = create_api_key(template.service, key_type=key_type)

    if template.template_type == "letter" and postage is None:
        postage = "second"

    if template.template_type == "sms" and rate_multiplier is None:
        rate_multiplier = 1

    data = {
        "id": uuid.uuid4(),
        "to": to_field,
        "job_id": job and job.id,
        "job": job,
        "service_id": template.service.id,
        "service": template.service,
        "template_id": template.id,
        "template_version": template.version,
        "status": status,
        "reference": reference,
        "created_at": created_at,
        "sent_at": sent_at,
        "billable_units": billable_units,
        "personalisation": personalisation,
        "notification_type": template.template_type,
        "api_key": api_key,
        "api_key_id": api_key and api_key.id,
        "key_type": api_key.key_type if api_key else key_type,
        "sent_by": sent_by,
        "updated_at": updated_at,
        "client_reference": client_reference,
        "job_row_number": job_row_number,
        "rate_multiplier": rate_multiplier,
        "international": international,
        "phone_prefix": phone_prefix,
        "normalised_to": normalised_to,
        "reply_to_text": reply_to_text,
        "created_by_id": created_by_id,
        "postage": postage,
        "document_download_count": document_download_count,
        "unsubscribe_link": unsubscribe_link,
    }
    notification = Notification(**data)
    dao_create_notification(notification)

    return notification


def create_notification_history(
    template=None,
    job=None,
    job_row_number=None,
    status="created",
    reference=None,
    created_at=None,
    sent_at=None,
    updated_at=None,
    billable_units=1,
    api_key=None,
    key_type=KEY_TYPE_NORMAL,
    sent_by=None,
    client_reference=None,
    rate_multiplier=None,
    international=False,
    phone_prefix=None,
    created_by_id=None,
    postage=None,
    id=None,
):
    assert job or template
    if job:
        template = job.template

    if created_at is None:
        created_at = datetime.utcnow()

    if status != "created":
        sent_at = sent_at or datetime.utcnow()
        updated_at = updated_at or datetime.utcnow()

    if template.template_type == "letter" and postage is None:
        postage = "second"

    data = {
        "id": id or uuid.uuid4(),
        "job_id": job and job.id,
        "job": job,
        "service_id": template.service.id,
        "service": template.service,
        "template_id": template.id,
        "template_version": template.version,
        "status": status,
        "reference": reference,
        "created_at": created_at,
        "sent_at": sent_at,
        "billable_units": billable_units,
        "notification_type": template.template_type,
        "api_key": api_key,
        "api_key_id": api_key and api_key.id,
        "key_type": api_key.key_type if api_key else key_type,
        "sent_by": sent_by,
        "updated_at": updated_at,
        "client_reference": client_reference,
        "job_row_number": job_row_number,
        "rate_multiplier": rate_multiplier,
        "international": international,
        "phone_prefix": phone_prefix,
        "created_by_id": created_by_id,
        "postage": postage,
    }
    notification_history = NotificationHistory(**data)
    db.session.add(notification_history)
    db.session.commit()

    return notification_history


def create_job(
    template,
    notification_count=1,
    created_at=None,
    job_status="pending",
    scheduled_for=None,
    processing_started=None,
    processing_finished=None,
    original_file_name="some.csv",
    archived=False,
    contact_list_id=None,
):
    data = {
        "id": uuid.uuid4(),
        "service_id": template.service_id,
        "service": template.service,
        "template_id": template.id,
        "template_version": template.version,
        "original_file_name": original_file_name,
        "notification_count": notification_count,
        "created_at": created_at or datetime.utcnow(),
        "created_by": template.created_by,
        "job_status": job_status,
        "scheduled_for": scheduled_for,
        "processing_started": processing_started,
        "processing_finished": processing_finished,
        "archived": archived,
        "contact_list_id": contact_list_id,
    }
    job = Job(**data)
    dao_create_job(job)
    return job


def create_service_permission(service_id, permission=EMAIL_TYPE):
    dao_add_service_permission(service_id if service_id else create_service().id, permission)

    service_permissions = ServicePermission.query.all()

    return service_permissions


def create_inbound_sms(
    service,
    notify_number=None,
    user_number="447700900111",
    provider_date=None,
    provider_reference=None,
    content="Hello",
    provider="mmg",
    created_at=None,
):
    if not service.inbound_number:
        create_inbound_number(
            # create random inbound number
            notify_number or f"07{random.randint(0, 1e9 - 1):09}",
            provider=provider,
            service_id=service.id,
        )

    inbound = InboundSms(
        service=service,
        created_at=created_at or datetime.utcnow(),
        notify_number=service.get_inbound_number(),
        user_number=user_number,
        provider_date=provider_date or datetime.utcnow(),
        provider_reference=provider_reference or "foo",
        content=content,
        provider=provider,
    )
    dao_create_inbound_sms(inbound)
    return inbound


def create_service_inbound_api(
    service,
    url="https://something.com",
    bearer_token="some_super_secret",
):
    service_inbound_api = ServiceInboundApi(
        service_id=service.id, url=url, bearer_token=bearer_token, updated_by_id=service.users[0].id
    )
    save_service_inbound_api(service_inbound_api)
    return service_inbound_api


def create_service_callback_api(
    service, url="https://something.com", bearer_token="some_super_secret", callback_type="delivery_status"
):
    service_callback_api = ServiceCallbackApi(
        service_id=service.id,
        url=url,
        bearer_token=bearer_token,
        updated_by_id=service.users[0].id,
        callback_type=callback_type,
    )
    save_service_callback_api(service_callback_api)
    return service_callback_api


def create_email_branding(
    id=None, colour="blue", alt_text=None, logo="test_x2.png", name="test_org_1", text="DisplayName", **kwargs
):
    data = {"colour": colour, "logo": logo, "alt_text": alt_text, "name": name, "text": text, **kwargs}
    if id:
        data["id"] = id
    email_branding = EmailBranding(**data)
    dao_create_email_branding(email_branding)

    return email_branding


def create_rate(start_date, value, notification_type):
    rate = Rate(id=uuid.uuid4(), valid_from=start_date, rate=value, notification_type=notification_type)
    db.session.add(rate)
    db.session.commit()
    return rate


def create_letter_rate(start_date=None, end_date=None, crown=True, sheet_count=1, rate=0.33, post_class="second"):
    if start_date is None:
        start_date = datetime(2016, 1, 1)
    rate = LetterRate(
        id=uuid.uuid4(),
        start_date=start_date,
        end_date=end_date,
        crown=crown,
        sheet_count=sheet_count,
        rate=rate,
        post_class=post_class,
    )
    db.session.add(rate)
    db.session.commit()
    return rate


def create_api_key(service, key_type=KEY_TYPE_NORMAL, key_name=None):
    id_ = uuid.uuid4()

    name = key_name if key_name else f"{key_type} api key {id_}"

    api_key = ApiKey(
        service=service, name=name, created_by=service.created_by, key_type=key_type, id=id_, secret=uuid.uuid4()
    )
    db.session.add(api_key)
    db.session.commit()
    return api_key


def create_inbound_number(number, provider="mmg", active=True, service_id=None):
    inbound_number = InboundNumber(
        id=uuid.uuid4(), number=number, provider=provider, active=active, service_id=service_id
    )
    db.session.add(inbound_number)
    db.session.commit()
    return inbound_number


def create_reply_to_email(service, email_address, is_default=True, archived=False):
    data = {
        "service": service,
        "email_address": email_address,
        "is_default": is_default,
        "archived": archived,
    }
    reply_to = ServiceEmailReplyTo(**data)

    db.session.add(reply_to)
    db.session.commit()

    return reply_to


def create_service_sms_sender(service, sms_sender, is_default=True, inbound_number_id=None, archived=False):
    data = {
        "service_id": service.id,
        "sms_sender": sms_sender,
        "is_default": is_default,
        "inbound_number_id": inbound_number_id,
        "archived": archived,
    }
    service_sms_sender = ServiceSmsSender(**data)

    db.session.add(service_sms_sender)
    db.session.commit()

    return service_sms_sender


def create_letter_contact(service, contact_block, is_default=True, archived=False):
    data = {
        "service": service,
        "contact_block": contact_block,
        "is_default": is_default,
        "archived": archived,
    }
    letter_content = ServiceLetterContact(**data)

    db.session.add(letter_content)
    db.session.commit()

    return letter_content


def create_annual_billing(service_id, free_sms_fragment_limit, financial_year_start):
    annual_billing = AnnualBilling(
        service_id=service_id,
        free_sms_fragment_limit=free_sms_fragment_limit,
        financial_year_start=financial_year_start,
    )
    db.session.add(annual_billing)
    db.session.commit()

    return annual_billing


def create_domain(domain, organisation_id):
    domain = Domain(domain=domain, organisation_id=organisation_id)

    db.session.add(domain)
    db.session.commit()

    return domain


def create_organisation(
    name="test_org_1",
    active=True,
    organisation_type=None,
    domains=None,
    organisation_id=None,
    purchase_order_number=None,
    billing_contact_names=None,
    billing_contact_email_addresses=None,
    billing_reference=None,
    email_branding_id=None,
    letter_branding_id=None,
):
    data = {
        "id": organisation_id,
        "name": name,
        "active": active,
        "organisation_type": organisation_type,
        "purchase_order_number": purchase_order_number,
        "billing_contact_names": billing_contact_names,
        "billing_contact_email_addresses": billing_contact_email_addresses,
        "billing_reference": billing_reference,
        "email_branding_id": email_branding_id,
        "letter_branding_id": letter_branding_id,
    }
    organisation = Organisation(**data)
    dao_create_organisation(organisation)

    for domain in domains or []:
        create_domain(domain, organisation.id)

    return organisation


def create_invited_org_user(organisation, invited_by, email_address="invite@example.com"):
    invited_org_user = InvitedOrganisationUser(
        email_address=email_address,
        invited_by=invited_by,
        organisation=organisation,
        permissions="can_make_services_live",
    )
    save_invited_org_user(invited_org_user)
    return invited_org_user


def create_daily_sorted_letter(
    billing_day=None, file_name="Notify-20180118123.rs.txt", unsorted_count=0, sorted_count=0
):
    daily_sorted_letter = DailySortedLetter(
        billing_day=billing_day or date(2018, 1, 18),
        file_name=file_name,
        unsorted_count=unsorted_count,
        sorted_count=sorted_count,
    )

    db.session.add(daily_sorted_letter)
    db.session.commit()

    return daily_sorted_letter


def create_ft_billing(
    bst_date,
    template,
    *,
    provider="test",
    rate_multiplier=1,
    international=False,
    rate=0,
    billable_unit=1,
    notifications_sent=1,
    postage="none",
):
    data = FactBilling(
        bst_date=bst_date,
        service_id=template.service_id,
        template_id=template.id,
        notification_type=template.template_type,
        provider=provider,
        rate_multiplier=rate_multiplier,
        international=international,
        rate=rate,
        billable_units=billable_unit,
        notifications_sent=notifications_sent,
        postage=postage,
    )
    db.session.add(data)
    db.session.commit()
    return data


def create_ft_notification_status(
    bst_date,
    notification_type="sms",
    service=None,
    template=None,
    job=None,
    key_type="normal",
    notification_status="delivered",
    count=1,
):
    if job:
        template = job.template
    if template:
        service = template.service
        notification_type = template.template_type
    else:
        if not service:
            service = create_service()
        template = create_template(service=service, template_type=notification_type)

    data = FactNotificationStatus(
        bst_date=bst_date,
        template_id=template.id,
        service_id=service.id,
        job_id=job.id if job else uuid.UUID(int=0),
        notification_type=notification_type,
        key_type=key_type,
        notification_status=notification_status,
        notification_count=count,
    )
    db.session.add(data)
    db.session.commit()
    return data


def create_process_time(bst_date="2021-03-01", messages_total=35, messages_within_10_secs=34):
    data = FactProcessingTime(
        bst_date=bst_date, messages_total=messages_total, messages_within_10_secs=messages_within_10_secs
    )
    fact_processing_time_dao.insert_update_processing_time(data)


def create_service_guest_list(service, email_address=None, mobile_number=None):
    if email_address:
        guest_list_user = ServiceGuestList.from_string(service.id, EMAIL_TYPE, email_address)
    elif mobile_number:
        guest_list_user = ServiceGuestList.from_string(service.id, MOBILE_TYPE, mobile_number)
    else:
        guest_list_user = ServiceGuestList.from_string(service.id, EMAIL_TYPE, "guest_list_user@digital.gov.uk")

    db.session.add(guest_list_user)
    db.session.commit()
    return guest_list_user


def create_complaint(service=None, notification=None, created_at=None):
    if not service:
        service = create_service()
    if not notification:
        template = create_template(service=service, template_type="email")
        notification = create_notification(template=template)

    complaint = Complaint(
        notification_id=notification.id,
        service_id=service.id,
        ses_feedback_id=str(uuid.uuid4()),
        complaint_type="abuse",
        complaint_date=datetime.utcnow(),
        created_at=created_at if created_at else datetime.now(),
    )
    db.session.add(complaint)
    db.session.commit()
    return complaint


def ses_complaint_callback_malformed_message_id():
    return {
        "Signature": "bb",
        "SignatureVersion": "1",
        "MessageAttributes": {},
        "MessageId": "98c6e927-af5d-5f3b-9522-bab736f2cbde",
        "UnsubscribeUrl": "https://sns.eu-west-1.amazonaws.com",
        "TopicArn": "arn:ses_notifications",
        "Type": "Notification",
        "Timestamp": "2018-06-05T14:00:15.952Z",
        "Subject": None,
        "Message": '{"notificationType":"Complaint","complaint":{"complainedRecipients":[{"emailAddress":"recipient1@example.com"}],"timestamp":"2018-06-05T13:59:58.000Z","feedbackId":"ses_feedback_id"},"mail":{"timestamp":"2018-06-05T14:00:15.950Z","source":"\\"Some Service\\" <someservicenotifications.service.gov.uk>","sourceArn":"arn:identity/notifications.service.gov.uk","sourceIp":"52.208.24.161","sendingAccountId":"888450439860","badMessageId":"ref1","destination":["recipient1@example.com"]}}',  # noqa
        "SigningCertUrl": "https://sns.pem",
    }


def ses_complaint_callback_with_missing_complaint_type():
    """
    https://docs.aws.amazon.com/ses/latest/DeveloperGuide/notification-contents.html#complaint-object
    """
    return {
        "Signature": "bb",
        "SignatureVersion": "1",
        "MessageAttributes": {},
        "MessageId": "98c6e927-af5d-5f3b-9522-bab736f2cbde",
        "UnsubscribeUrl": "https://sns.eu-west-1.amazonaws.com",
        "TopicArn": "arn:ses_notifications",
        "Type": "Notification",
        "Timestamp": "2018-06-05T14:00:15.952Z",
        "Subject": None,
        "Message": '{"notificationType":"Complaint","complaint":{"complainedRecipients":[{"emailAddress":"recipient1@example.com"}],"timestamp":"2018-06-05T13:59:58.000Z","feedbackId":"ses_feedback_id"},"mail":{"timestamp":"2018-06-05T14:00:15.950Z","source":"\\"Some Service\\" <someservicenotifications.service.gov.uk>","sourceArn":"arn:identity/notifications.service.gov.uk","sourceIp":"52.208.24.161","sendingAccountId":"888450439860","messageId":"ref1","destination":["recipient1@example.com"]}}',  # noqa
        "SigningCertUrl": "https://sns.pem",
    }


def ses_complaint_callback():
    """
    https://docs.aws.amazon.com/ses/latest/DeveloperGuide/notification-contents.html#complaint-object
    """
    return {
        "Signature": "bb",
        "SignatureVersion": "1",
        "MessageAttributes": {},
        "MessageId": "98c6e927-af5d-5f3b-9522-bab736f2cbde",
        "UnsubscribeUrl": "https://sns.eu-west-1.amazonaws.com",
        "TopicArn": "arn:ses_notifications",
        "Type": "Notification",
        "Timestamp": "2018-06-05T14:00:15.952Z",
        "Subject": None,
        "Message": '{"notificationType":"Complaint","complaint":{"complaintFeedbackType": "abuse", "complainedRecipients":[{"emailAddress":"recipient1@example.com"}],"timestamp":"2018-06-05T13:59:58.000Z","feedbackId":"ses_feedback_id"},"mail":{"timestamp":"2018-06-05T14:00:15.950Z","source":"\\"Some Service\\" <someservicenotifications.service.gov.uk>","sourceArn":"arn:identity/notifications.service.gov.uk","sourceIp":"52.208.24.161","sendingAccountId":"888450439860","messageId":"ref1","destination":["recipient1@example.com"]}}',  # noqa
        "SigningCertUrl": "https://sns.pem",
    }


def ses_notification_callback():
    return (
        '{\n  "Type" : "Notification",\n  "MessageId" : "ref1",'
        '\n  "TopicArn" : "arn:aws:sns:eu-west-1:123456789012:testing",'
        '\n  "Message" : "{\\"notificationType\\":\\"Delivery\\",'
        '\\"mail\\":{\\"timestamp\\":\\"2016-03-14T12:35:25.909Z\\",'
        '\\"source\\":\\"test@test-domain.com\\",'
        '\\"sourceArn\\":\\"arn:aws:ses:eu-west-1:123456789012:identity/testing-notify\\",'
        '\\"sendingAccountId\\":\\"123456789012\\",'
        '\\"messageId\\":\\"ref1\\",'
        '\\"destination\\":[\\"testing@digital.cabinet-office.gov.uk\\"]},'
        '\\"delivery\\":{\\"timestamp\\":\\"2016-03-14T12:35:26.567Z\\",'
        '\\"processingTimeMillis\\":658,'
        '\\"recipients\\":[\\"testing@digital.cabinet-office.gov.uk\\"],'
        '\\"smtpResponse\\":\\"250 2.0.0 OK 1457958926 uo5si26480932wjc.221 - gsmtp\\",'
        '\\"reportingMTA\\":\\"a6-238.smtp-out.eu-west-1.amazonses.com\\"}}",'
        '\n  "Timestamp" : "2016-03-14T12:35:26.665Z",\n  "SignatureVersion" : "1",'
        '\n  "Signature" : "X8d7eTAOZ6wlnrdVVPYanrAlsX0SMPfOzhoTEBnQqYkrNWTqQY91C0f3bxtPdUhUt'
        "OowyPAOkTQ4KnZuzphfhVb2p1MyVYMxNKcBFB05/qaCX99+92fjw4x9LeUOwyGwMv5F0Vkfi5qZCcEw69uVrhYL"
        "VSTFTrzi/yCtru+yFULMQ6UhbY09GwiP6hjxZMVr8aROQy5lLHglqQzOuSZ4KeD85JjifHdKzlx8jjQ+uj+FLzHXPMA"
        'PmPU1JK9kpoHZ1oPshAFgPDpphJe+HwcJ8ezmk+3AEUr3wWli3xF+49y8Z2anASSVp6YI2YP95UT8Rlh3qT3T+V9V8rbSVislxA==",'
        '\n  "SigningCertURL" : "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-bb750'
        'dd426d95ee9390147a5624348ee.pem",'
        '\n  "UnsubscribeURL" : "https://sns.eu-west-1.amazonaws.com/?Action=Unsubscribe&S'
        'subscriptionArn=arn:aws:sns:eu-west-1:302763885840:preview-emails:d6aad3ef-83d6-4cf3-a470-54e2e75916da"\n}'
    )


def create_service_data_retention(service, notification_type="sms", days_of_retention=3):
    data_retention = insert_service_data_retention(
        service_id=service.id, notification_type=notification_type, days_of_retention=days_of_retention
    )
    return data_retention


def create_invited_user(service=None, to_email_address=None):
    if service is None:
        service = create_service()
    if to_email_address is None:
        to_email_address = "invited_user@digital.gov.uk"

    from_user = service.users[0]

    data = {
        "service": service,
        "email_address": to_email_address,
        "from_user": from_user,
        "permissions": "send_messages,manage_service,manage_api_keys",
        "folder_permissions": [str(uuid.uuid4()), str(uuid.uuid4())],
    }
    invited_user = InvitedUser(**data)
    save_invited_user(invited_user)
    return invited_user


def create_template_folder(service, name="foo", parent=None):
    tf = TemplateFolder(name=name, service=service, parent=parent)
    db.session.add(tf)
    db.session.commit()
    return tf


def create_letter_branding(name="HM Government", filename="hm-government", id=None):
    data = {
        "name": name,
        "filename": filename,
    }

    if id:
        data["id"] = id

    test_domain_branding = LetterBranding(**data)
    db.session.add(test_domain_branding)
    db.session.commit()
    return test_domain_branding


def set_up_usage_data(start_date):
    year = int(start_date.strftime("%Y"))
    one_week_earlier = start_date - timedelta(days=7)
    two_days_later = start_date + timedelta(days=2)
    one_week_later = start_date + timedelta(days=7)
    one_month_later = start_date + timedelta(days=31)

    # service with sms and letters:
    service_1_sms_and_letter = create_service(
        service_name="a - with sms and letter",
        purchase_order_number="service purchase order number",
        billing_contact_names="service billing contact names",
        billing_contact_email_addresses="service@billing.contact email@addresses.gov.uk",
        billing_reference="service billing reference",
    )
    letter_template_1 = create_template(service=service_1_sms_and_letter, template_type="letter")
    sms_template_1 = create_template(service=service_1_sms_and_letter, template_type="sms")
    create_annual_billing(service_id=service_1_sms_and_letter.id, free_sms_fragment_limit=10, financial_year_start=year)
    create_annual_billing(
        service_id=service_1_sms_and_letter.id, free_sms_fragment_limit=100, financial_year_start=year - 1
    )
    create_annual_billing(
        service_id=service_1_sms_and_letter.id, free_sms_fragment_limit=50, financial_year_start=year - 2
    )

    org_1 = create_organisation(
        name=f"Org for {service_1_sms_and_letter.name}",
        purchase_order_number="org1 purchase order number",
        billing_contact_names="org1 billing contact names",
        billing_contact_email_addresses="org1@billing.contact email@addresses.gov.uk",
        billing_reference="org1 billing reference",
    )
    dao_add_service_to_organisation(service=service_1_sms_and_letter, organisation_id=org_1.id)

    create_ft_billing(bst_date=one_week_earlier, template=sms_template_1, billable_unit=2, rate=0.11)
    create_ft_billing(bst_date=start_date, template=sms_template_1, billable_unit=2, rate=0.11)
    create_ft_billing(bst_date=two_days_later, template=sms_template_1, billable_unit=1, rate=0.11)

    create_ft_billing(
        bst_date=one_week_later,
        template=letter_template_1,
        notifications_sent=2,
        billable_unit=2,
        rate=0.35,
        postage="first",
    )
    create_ft_billing(
        bst_date=one_month_later,
        template=letter_template_1,
        notifications_sent=4,
        billable_unit=8,
        rate=0.45,
        postage="second",
    )
    create_ft_billing(
        bst_date=one_week_later,
        template=letter_template_1,
        notifications_sent=2,
        billable_unit=4,
        rate=0.45,
        postage="second",
    )

    # service with emails only:
    service_with_emails = create_service(service_name="b - emails")
    email_template = create_template(service=service_with_emails, template_type="email")
    org_2 = create_organisation(
        name=f"Org for {service_with_emails.name}",
    )
    dao_add_service_to_organisation(service=service_with_emails, organisation_id=org_2.id)
    create_annual_billing(service_id=service_with_emails.id, free_sms_fragment_limit=0, financial_year_start=year)

    create_ft_billing(bst_date=start_date, template=email_template, notifications_sent=10)

    # service with letters:
    service_with_letters = create_service(service_name="c - letters only")
    letter_template_3 = create_template(service=service_with_letters, template_type="letter")
    org_for_service_with_letters = create_organisation(
        name=f"Org for {service_with_letters.name}",
        purchase_order_number="org3 purchase order number",
        billing_contact_names="org3 billing contact names",
        billing_contact_email_addresses="org3@billing.contact email@addresses.gov.uk",
        billing_reference="org3 billing reference",
    )
    dao_add_service_to_organisation(service=service_with_letters, organisation_id=org_for_service_with_letters.id)
    create_annual_billing(service_id=service_with_letters.id, free_sms_fragment_limit=0, financial_year_start=year)

    create_ft_billing(
        bst_date=start_date,
        template=letter_template_3,
        notifications_sent=2,
        billable_unit=3,
        rate=0.50,
        postage="first",
    )
    create_ft_billing(
        bst_date=one_week_later,
        template=letter_template_3,
        notifications_sent=8,
        billable_unit=5,
        rate=0.65,
        postage="second",
    )
    create_ft_billing(
        bst_date=one_month_later,
        template=letter_template_3,
        notifications_sent=12,
        billable_unit=5,
        rate=0.65,
        postage="second",
    )

    # service with letters, without an organisation:
    service_with_letters_without_org = create_service(service_name="d - service without org")
    letter_template_4 = create_template(service=service_with_letters_without_org, template_type="letter")
    create_annual_billing(
        service_id=service_with_letters_without_org.id, free_sms_fragment_limit=0, financial_year_start=year
    )

    create_ft_billing(
        bst_date=two_days_later,
        template=letter_template_4,
        notifications_sent=7,
        billable_unit=4,
        rate=1.55,
        postage="rest-of-world",
    )
    create_ft_billing(
        bst_date=two_days_later,
        template=letter_template_4,
        notifications_sent=8,
        billable_unit=4,
        rate=1.55,
        postage="europe",
    )
    create_ft_billing(
        bst_date=two_days_later,
        template=letter_template_4,
        notifications_sent=2,
        billable_unit=1,
        rate=0.35,
        postage="second",
    )
    create_ft_billing(
        bst_date=two_days_later,
        template=letter_template_4,
        notifications_sent=1,
        billable_unit=1,
        rate=0.50,
        postage="first",
    )

    # service with chargeable SMS, without an organisation
    service_with_sms_without_org = create_service(
        service_name="b - chargeable sms",
        purchase_order_number="sms purchase order number",
        billing_contact_names="sms billing contact names",
        billing_contact_email_addresses="sms@billing.contact email@addresses.gov.uk",
        billing_reference="sms billing reference",
    )
    sms_template = create_template(service=service_with_sms_without_org, template_type="sms")
    create_annual_billing(
        service_id=service_with_sms_without_org.id, free_sms_fragment_limit=10, financial_year_start=year
    )
    create_ft_billing(bst_date=one_week_earlier, template=sms_template, rate=0.11, billable_unit=12)
    create_ft_billing(bst_date=two_days_later, template=sms_template, rate=0.11)
    create_ft_billing(bst_date=one_week_later, template=sms_template, billable_unit=2, rate=0.11)

    # service with SMS within free allowance
    service_with_sms_within_allowance = create_service(service_name="e - sms within allowance")
    sms_template_2 = create_template(service=service_with_sms_within_allowance, template_type="sms")
    create_annual_billing(
        service_id=service_with_sms_within_allowance.id, free_sms_fragment_limit=10, financial_year_start=year
    )
    create_ft_billing(bst_date=one_week_later, template=sms_template_2, billable_unit=2, rate=0.11)

    # service without ft_billing this year
    service_with_out_ft_billing_this_year = create_service(
        service_name="f - without ft_billing",
        purchase_order_number="sms purchase order number",
        billing_contact_names="sms billing contact names",
        billing_contact_email_addresses="sms@billing.contact email@addresses.gov.uk",
        billing_reference="sms billing reference",
    )
    create_annual_billing(
        service_id=service_with_out_ft_billing_this_year.id, free_sms_fragment_limit=10, financial_year_start=year
    )
    dao_add_service_to_organisation(service=service_with_out_ft_billing_this_year, organisation_id=org_1.id)

    # dictionary with services and orgs to return
    return {
        "org_1": org_1,
        "service_1_sms_and_letter": service_1_sms_and_letter,
        "org_2": org_2,
        "service_with_emails": service_with_emails,
        "org_for_service_with_letters": org_for_service_with_letters,
        "service_with_letters": service_with_letters,
        "service_with_letters_without_org": service_with_letters_without_org,
        "service_with_sms_without_org": service_with_sms_without_org,
        "service_with_sms_within_allowance": service_with_sms_within_allowance,
        "service_with_out_ft_billing_this_year": service_with_out_ft_billing_this_year,
    }


def create_returned_letter(service=None, reported_at=None, notification_id=None):
    if not service:
        service = create_service(service_name="a - with sms and letter")
    returned_letter = ReturnedLetter(
        service_id=service.id,
        reported_at=reported_at or datetime.utcnow(),
        notification_id=notification_id or uuid.uuid4(),
        created_at=datetime.utcnow(),
    )

    db.session.add(returned_letter)
    db.session.commit()
    return returned_letter


def create_service_contact_list(
    service=None,
    original_file_name="EmergencyContactList.xls",
    row_count=100,
    template_type="email",
    created_by_id=None,
    archived=False,
):
    if not service:
        service = create_service(service_name="service for contact list", user=create_user())

    contact_list = ServiceContactList(
        service_id=service.id,
        original_file_name=original_file_name,
        row_count=row_count,
        template_type=template_type,
        created_by_id=created_by_id or service.users[0].id,
        created_at=datetime.utcnow(),
        archived=archived,
    )
    db.session.add(contact_list)
    db.session.commit()
    return contact_list


def create_webauthn_credential(
    user,
    name="my key",
    *,
    credential_data="ABC123",
    registration_response="DEF456",
):
    webauthn_credential = WebauthnCredential(
        user=user, name=name, credential_data=credential_data, registration_response=registration_response
    )

    db.session.add(webauthn_credential)
    db.session.commit()
    return webauthn_credential


def create_letter_attachment(created_by_id):
    letter_attachment = LetterAttachment(
        created_by_id=created_by_id,
        original_filename="abc.pdf",
        page_count=1,
    )
    db.session.add(letter_attachment)
    db.session.commit()
    return letter_attachment


def create_unsubscribe_request(
    service,
    *,
    email_address=None,
    created_at=None,
    job=None,
    unsubscribe_request_report_id=None,
):
    template = (
        job.template
        if job
        else create_template(
            service,
            template_type=EMAIL_TYPE,
        )
    )
    notification = create_notification(
        template=template,
        job=job,
        to_field=email_address,
        sent_at=(created_at or datetime.now()) - timedelta(days=1),
    )
    return create_unsubscribe_request_dao(
        {
            "notification_id": notification.id,
            "template_id": notification.template_id,
            "template_version": notification.template_version,
            "service_id": notification.service_id,
            "email_address": notification.to,
            "created_at": created_at or datetime.now(),
            "unsubscribe_request_report_id": unsubscribe_request_report_id,
        }
    )


def create_unsubscribe_request_report(
    service,
    *,
    count=123,
    earliest_timestamp,
    latest_timestamp,
    processed_by_service_at=None,
    created_at=None,
):
    report = UnsubscribeRequestReport(
        id=uuid.uuid4(),
        count=count,
        earliest_timestamp=earliest_timestamp,
        latest_timestamp=latest_timestamp,
        service_id=service.id,
        processed_by_service_at=processed_by_service_at,
        created_at=created_at,
    )
    create_unsubscribe_request_reports_dao(report)
    return report
