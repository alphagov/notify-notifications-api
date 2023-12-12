# ruff: noqa: T201

import csv
import functools
import itertools
import logging
import os
import random
import uuid
from datetime import date, datetime, timedelta
from time import monotonic
from unittest import mock

import click
import flask
from click_datetime import Datetime as click_dt
from dateutil import rrule
from flask import current_app, json
from notifications_utils.recipients import RecipientCSV
from notifications_utils.statsd_decorators import statsd
from notifications_utils.template import SMSMessageTemplate
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import NoResultFound

from app import db
from app.aws import s3
from app.celery.letters_pdf_tasks import (
    get_pdf_for_templated_letter,
    resanitise_pdf,
)
from app.celery.tasks import process_row, record_daily_sorted_counts
from app.config import QueueNames
from app.constants import KEY_TYPE_TEST, NOTIFICATION_CREATED, SMS_TYPE
from app.dao.annual_billing_dao import (
    dao_create_or_update_annual_billing_for_year,
    set_default_free_allowance_for_service,
)
from app.dao.fact_billing_dao import (
    delete_billing_data_for_day,
    fetch_billing_data_for_day,
    update_ft_billing,
)
from app.dao.jobs_dao import dao_get_job_by_id
from app.dao.notifications_dao import move_notifications_to_notification_history
from app.dao.organisation_dao import (
    dao_add_service_to_organisation,
    dao_get_organisation_by_email_address,
    dao_get_organisation_by_id,
)
from app.dao.services_dao import (
    dao_create_service,
    dao_fetch_all_services_by_user,
    dao_fetch_all_services_created_by_user,
    dao_fetch_service_by_id,
    dao_update_service,
    delete_service_and_all_associated_db_objects,
)
from app.dao.templates_dao import dao_create_template, dao_get_template_by_id
from app.dao.users_dao import (
    delete_model_user,
    delete_user_verify_codes,
    get_user_by_email,
)
from app.functional_tests_fixtures import apply_fixtures
from app.models import (
    Domain,
    EmailBranding,
    LetterBranding,
    Notification,
    Organisation,
    Service,
    Template,
    User,
)


@click.group(name="command", help="Additional commands")
def command_group():
    pass


class notify_command:
    def __init__(self, name=None):
        self.name = name

    def __call__(self, func):
        decorators = [
            functools.wraps(func),  # carry through function name, docstrings, etc.
            click.command(name=self.name),  # turn it into a click.Command
        ]

        # in the test environment the app context is already provided and having
        # another will lead to the test db connection being closed prematurely
        if os.getenv("NOTIFY_ENVIRONMENT", "") != "test":
            # with_appcontext ensures the config is loaded, db connected, etc.
            decorators.insert(0, flask.cli.with_appcontext)

        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        for decorator in decorators:
            # this syntax is equivalent to e.g. "@flask.cli.with_appcontext"
            wrapper = decorator(wrapper)

        command_group.add_command(wrapper)
        return wrapper


@notify_command()
@click.option(
    "-u",
    "--user_email_prefix",
    required=True,
    help="""
    Functional test user email prefix. eg "notify-test-preview"
""",
)  # noqa
def purge_functional_test_data(user_email_prefix):
    """
    Remove non-seeded functional test data

    users, services, etc. Give an email prefix. Probably "notify-tests-preview".
    """
    users = User.query.filter(User.email_address.like(f"{user_email_prefix}%")).all()
    for usr in users:
        # Make sure the full email includes a uuid in it
        # Just in case someone decides to use a similar email address.
        try:
            uuid.UUID(usr.email_address.split("@")[0].split("+")[1])
        except ValueError:
            print(f"Skipping {usr.email_address} as the user email doesn't contain a UUID.")
        else:
            services = dao_fetch_all_services_by_user(usr.id)
            if services:
                print(f"Deleting user {usr.id} which is part of services")
                for service in services:
                    delete_service_and_all_associated_db_objects(service)
            else:
                services_created_by_this_user = dao_fetch_all_services_created_by_user(usr.id)
                if services_created_by_this_user:
                    # user is not part of any services but may still have been the one to create the service
                    # sometimes things get in this state if the tests fail half way through
                    # Remove the service they created (but are not a part of) so we can then remove the user
                    print(f"Deleting services created by {usr.id}")
                    for service in services_created_by_this_user:
                        delete_service_and_all_associated_db_objects(service)

                print(f"Deleting user {usr.id} which is not part of any services")
                delete_user_verify_codes(usr)
                delete_model_user(usr)


@notify_command()
def backfill_notification_statuses():
    """
    DEPRECATED. Populates notification_status.

    This will be used to populate the new `Notification._status_fkey` with the old
    `Notification._status_enum`
    """
    LIMIT = 250000
    subq = f"SELECT id FROM notification_history WHERE notification_status is NULL LIMIT {LIMIT}"
    update = f"UPDATE notification_history SET notification_status = status WHERE id in ({subq})"
    result = db.session.execute(subq).fetchall()

    while len(result) > 0:
        db.session.execute(update)
        print(f"commit {LIMIT} updates at {datetime.utcnow()}")
        db.session.commit()
        result = db.session.execute(subq).fetchall()


@notify_command()
def update_notification_international_flag():
    """
    DEPRECATED. Set notifications.international=false.
    """
    # 250,000 rows takes 30 seconds to update.
    subq = "select id from notifications where international is null limit 250000"
    update = f"update notifications set international = False where id in ({subq})"
    result = db.session.execute(subq).fetchall()

    while len(result) > 0:
        db.session.execute(update)
        print(f"commit 250000 updates at {datetime.utcnow()}")
        db.session.commit()
        result = db.session.execute(subq).fetchall()

    # Now update notification_history
    subq_history = "select id from notification_history where international is null limit 250000"
    update_history = f"update notification_history set international = False where id in ({subq_history})"
    result_history = db.session.execute(subq_history).fetchall()
    while len(result_history) > 0:
        db.session.execute(update_history)
        print(f"commit 250000 updates at {datetime.utcnow()}")
        db.session.commit()
        result_history = db.session.execute(subq_history).fetchall()


@notify_command()
def fix_notification_statuses_not_in_sync():
    """
    DEPRECATED.
    This will be used to correct an issue where Notification._status_enum and NotificationHistory._status_fkey
    became out of sync. See 979e90a.

    Notification._status_enum is the source of truth so NotificationHistory._status_fkey will be updated with
    these values.
    """
    MAX = 10000

    subq = f"SELECT id FROM notifications WHERE cast (status as text) != notification_status LIMIT {MAX}"
    update = f"UPDATE notifications SET notification_status = status WHERE id in ({subq})"
    result = db.session.execute(subq).fetchall()

    while len(result) > 0:
        db.session.execute(update)
        print(f"Committed {len(result)} updates at {datetime.utcnow()}")
        db.session.commit()
        result = db.session.execute(subq).fetchall()

    subq_hist = f"SELECT id FROM notification_history WHERE cast (status as text) != notification_status LIMIT {MAX}"
    update = f"UPDATE notification_history SET notification_status = status WHERE id in ({subq_hist})"
    result = db.session.execute(subq_hist).fetchall()

    while len(result) > 0:
        db.session.execute(update)
        print(f"Committed {len(result)} updates at {datetime.utcnow()}")
        db.session.commit()
        result = db.session.execute(subq_hist).fetchall()


@notify_command(name="insert-inbound-numbers")
@click.option(
    "-f",
    "--file_name",
    required=True,
    help="""Full path of the file to upload, file is a contains inbound numbers,
              one number per line. The number must have the format of 07... not 447....""",
)
def insert_inbound_numbers_from_file(file_name):
    print(f"Inserting inbound numbers from {file_name}")
    with open(file_name) as file:
        sql = "insert into inbound_numbers values('{}', '{}', 'mmg', null, True, now(), null);"

        for line in file:
            line = line.strip()
            if line:
                print(line)
                db.session.execute(sql.format(uuid.uuid4(), line))
                db.session.commit()


@notify_command(name="replay-create-pdf-for-templated-letter")
@click.option(
    "-n",
    "--notification_id",
    type=click.UUID,
    required=True,
    help="Notification id of the letter that needs the get_pdf_for_templated_letter task replayed",
)
def replay_create_pdf_for_templated_letter(notification_id):
    print(f"Create task to get_pdf_for_templated_letter for notification: {notification_id}")
    get_pdf_for_templated_letter.apply_async([str(notification_id)], queue=QueueNames.CREATE_LETTERS_PDF)


@notify_command(name="recreate-pdf-for-precompiled-or-uploaded-letter")
@click.option(
    "-n",
    "--notification_id",
    type=click.UUID,
    required=True,
    help="Notification ID of the precompiled or uploaded letter",
)
def recreate_pdf_for_precompiled_or_uploaded_letter(notification_id):
    print(f"Call resanitise_pdf task for notification: {notification_id}")
    resanitise_pdf.apply_async([str(notification_id)], queue=QueueNames.LETTERS)


def setup_commands(application):
    application.cli.add_command(command_group)


@notify_command(name="rebuild-ft-billing-for-day")
@click.option("-s", "--service_id", required=False, type=click.UUID)
@click.option(
    "-d", "--day", help="The date to recalculate, as YYYY-MM-DD", required=True, type=click_dt(format="%Y-%m-%d")
)
def rebuild_ft_billing_for_day(service_id, day: date):
    """
    Rebuild the data in ft_billing for a given day, optionally filtering by service_id
    """

    def rebuild_ft_data(process_day: date, service_ids=None):
        deleted_rows = delete_billing_data_for_day(process_day=day, service_ids=service_ids)
        current_app.logger.info("deleted %s existing billing rows for %s", deleted_rows, process_day)

        billing_data = fetch_billing_data_for_day(process_day=process_day, service_ids=service_ids)
        update_ft_billing(billing_data, process_day)
        current_app.logger.info("added/updated %s billing rows for %s", len(billing_data), process_day)

    if service_id:
        # get the service to confirm it exists
        dao_fetch_service_by_id(service_id)
        rebuild_ft_data(day, service_ids=[service_id])
    else:
        rebuild_ft_data(day)


@notify_command(name="bulk-invite-user-to-service")
@click.option(
    "-f",
    "--file_name",
    required=True,
    help="Full path of the file containing a list of email address for people to invite to a service",
)
@click.option("-s", "--service_id", required=True, help="The id of the service that the invite is for")
@click.option("-u", "--user_id", required=True, help="The id of the user that the invite is from")
@click.option(
    "-a",
    "--auth_type",
    required=False,
    help="The authentication type for the user, sms_auth or email_auth. Defaults to sms_auth if not provided",
)
@click.option("-p", "--permissions", required=True, help="Comma separated list of permissions.")
def bulk_invite_user_to_service(file_name, service_id, user_id, auth_type, permissions):
    #  permissions
    #  manage_users | manage_templates | manage_settings
    #  send messages ==> send_texts | send_emails | send_letters
    #  Access API keys manage_api_keys
    #  platform_admin
    #  view_activity
    # "send_texts,send_emails,send_letters,view_activity"
    from app.service_invite.rest import create_invited_user

    file = open(file_name)
    for email_address in file:
        data = {
            "service": service_id,
            "email_address": email_address.strip(),
            "from_user": user_id,
            "permissions": permissions,
            "auth_type": auth_type,
            "invite_link_host": current_app.config["ADMIN_BASE_URL"],
        }
        with current_app.test_request_context(
            path=f"/service/{service_id}/invite/",
            method="POST",
            data=json.dumps(data),
            headers={"Content-Type": "application/json"},
        ):
            try:
                response = create_invited_user(service_id)
                if response[1] != 201:
                    print(f"*** ERROR occurred for email address: {email_address.strip()}")
                print(response[0].get_data(as_text=True))
            except Exception as e:
                print(f"*** ERROR occurred for email address: {email_address.strip()}. \n{e}")

    file.close()


@notify_command(name="populate-notification-postage")
@click.option(
    "-s", "--start_date", default=datetime(2017, 2, 1), help="start date inclusive", type=click_dt(format="%Y-%m-%d")
)
@statsd(namespace="tasks")
def populate_notification_postage(start_date):
    current_app.logger.info("populating historical notification postage")

    total_updated = 0

    while start_date < datetime.utcnow():
        # process in ten day chunks
        end_date = start_date + timedelta(days=10)

        sql = """
            UPDATE {}
            SET postage = 'second'
            WHERE notification_type = 'letter' AND
            postage IS NULL AND
            created_at BETWEEN :start AND :end
            """

        execution_start = datetime.utcnow()

        if end_date > datetime.utcnow() - timedelta(days=8):
            print("Updating notifications table as well")
            db.session.execute(sql.format("notifications"), {"start": start_date, "end": end_date})

        result = db.session.execute(sql.format("notification_history"), {"start": start_date, "end": end_date})
        db.session.commit()

        current_app.logger.info(
            "notification postage took %sms. Migrated %s rows for %s to %s",
            datetime.utcnow() - execution_start,
            result.rowcount,
            start_date,
            end_date,
        )

        start_date += timedelta(days=10)

        total_updated += result.rowcount

    current_app.logger.info("Total inserted/updated records = %s", total_updated)


@notify_command(name="archive-jobs-created-between-dates")
@click.option("-s", "--start_date", required=True, help="start date inclusive", type=click_dt(format="%Y-%m-%d"))
@click.option("-e", "--end_date", required=True, help="end date inclusive", type=click_dt(format="%Y-%m-%d"))
@statsd(namespace="tasks")
def update_jobs_archived_flag(start_date, end_date):
    current_app.logger.info("Archiving jobs created between %s to %s", start_date, end_date)

    process_date = start_date
    total_updated = 0

    while process_date < end_date:
        start_time = datetime.utcnow()
        sql = """update
                    jobs set archived = true
                where
                    created_at >= (date :start + time '00:00:00') at time zone 'Europe/London'
                    at time zone 'UTC'
                    and created_at < (date :end + time '00:00:00') at time zone 'Europe/London' at time zone 'UTC'"""

        result = db.session.execute(sql, {"start": process_date, "end": process_date + timedelta(days=1)})
        db.session.commit()
        current_app.logger.info(
            "jobs: --- Completed took %sms. Archived %s jobs for %s",
            datetime.now() - start_time,
            result.rowcount,
            process_date,
        )

        process_date += timedelta(days=1)

        total_updated += result.rowcount
    current_app.logger.info("Total archived jobs = %s", total_updated)


@notify_command(name="update-emails-to-remove-gsi")
@click.option("-s", "--service_id", required=True, help="service id. Update all user.email_address to remove .gsi")
@statsd(namespace="tasks")
def update_emails_to_remove_gsi(service_id):
    users_to_update = """SELECT u.id user_id, u.name, email_address, s.id, s.name
                           FROM users u
                           JOIN user_to_service us on (u.id = us.user_id)
                           JOIN services s on (s.id = us.service_id)
                          WHERE s.id = :service_id
                            AND u.email_address ilike ('%.gsi.gov.uk%')
    """
    results = db.session.execute(users_to_update, {"service_id": service_id})
    print(f"Updating {results.rowcount} users.")

    for user in results:
        print(f"User with id {user.user_id} updated")

        update_stmt = """
        UPDATE users
           SET email_address = replace(replace(email_address, '.gsi.gov.uk', '.gov.uk'), '.GSI.GOV.UK', '.GOV.UK'),
               updated_at = now()
         WHERE id = :user_id
        """
        db.session.execute(update_stmt, {"user_id": str(user.user_id)})
        db.session.commit()


@notify_command(name="replay-daily-sorted-count-files")
@click.option("-f", "--file_extension", required=False, help="File extension to search for, defaults to rs.txt")
@statsd(namespace="tasks")
def replay_daily_sorted_count_files(file_extension):
    bucket_location = "{}-ftp".format(current_app.config["NOTIFY_EMAIL_DOMAIN"])
    for filename in s3.get_list_of_files_by_suffix(
        bucket_name=bucket_location, subfolder="root/dispatch", suffix=file_extension or ".rs.txt"
    ):
        print("Create task to record daily sorted counts for file: ", filename)
        record_daily_sorted_counts.apply_async([filename], queue=QueueNames.NOTIFY)


@notify_command(name="populate-organisations-from-file")
@click.option(
    "-f",
    "--file_name",
    required=True,
    help="Pipe delimited file containing organisation name, sector, crown, argeement_signed, domains",
)
def populate_organisations_from_file(file_name):  # noqa: C901
    # [0] organisation name:: name of the organisation insert if organisation is missing.
    # [1] sector:: Central | Local | NHS only
    # [2] crown:: TRUE | FALSE only
    # [3] argeement_signed:: TRUE | FALSE
    # [4] domains:: comma separated list of domains related to the organisation
    # [5] email branding name: name of the default email branding for the org
    # [6] letter branding name: name of the default letter branding for the org

    # The expectation is that the organisation, organisation_to_service
    # and user_to_organisation will be cleared before running this command.
    # Ignoring duplicates allows us to run the command again with the same file or same file with new rows.
    with open(file_name) as f:

        def boolean_or_none(field):
            if field == "1":
                return True
            elif field == "0":
                return False
            elif field == "":
                return None

        for line in itertools.islice(f, 1, None):
            columns = line.split("|")
            print(columns)
            email_branding = None
            email_branding_column = columns[5].strip()
            if len(email_branding_column) > 0:
                email_branding = EmailBranding.query.filter(EmailBranding.name == email_branding_column).one()
            letter_branding = None
            letter_branding_column = columns[6].strip()
            if len(letter_branding_column) > 0:
                letter_branding = LetterBranding.query.filter(LetterBranding.name == letter_branding_column).one()
            data = {
                "name": columns[0],
                "active": True,
                "agreement_signed": boolean_or_none(columns[3]),
                "crown": boolean_or_none(columns[2]),
                "organisation_type": columns[1].lower(),
                "email_branding_id": email_branding.id if email_branding else None,
                "letter_branding_id": letter_branding.id if letter_branding else None,
            }
            org = Organisation(**data)
            try:
                db.session.add(org)
                db.session.commit()
            except IntegrityError:
                print("duplicate org", org.name)
                db.session.rollback()
            domains = columns[4].split(",")
            for d in domains:
                if len(d.strip()) > 0:
                    domain = Domain(domain=d.strip(), organisation_id=org.id)
                    try:
                        db.session.add(domain)
                        db.session.commit()
                    except IntegrityError:
                        print("duplicate domain", d.strip())
                        db.session.rollback()


@notify_command(name="populate-organisation-agreement-details-from-file")
@click.option(
    "-f",
    "--file_name",
    required=True,
    help="CSV file containing id, agreement_signed_version, agreement_signed_on_behalf_of_name, agreement_signed_at",
)
def populate_organisation_agreement_details_from_file(file_name):
    """
    The input file should be a comma separated CSV file with a header row and 4 columns
    id: the organisation ID
    agreement_signed_version
    agreement_signed_on_behalf_of_name
    agreement_signed_at: The date the agreement was signed in the format of 'dd/mm/yyyy'
    """
    with open(file_name) as f:
        csv_reader = csv.reader(f)

        # ignore the header row
        next(csv_reader)

        for row in csv_reader:
            org = dao_get_organisation_by_id(row[0])

            current_app.logger.info("Updating %s", org.name)

            assert org.agreement_signed

            org.agreement_signed_version = float(row[1])
            org.agreement_signed_on_behalf_of_name = row[2].strip()
            org.agreement_signed_at = datetime.strptime(row[3], "%d/%m/%Y")

            db.session.add(org)
            db.session.commit()


@notify_command(name="get-notification-and-service-ids-for-letters-that-failed-to-print")
@click.option(
    "-f",
    "--file_name",
    required=True,
    help="""Full path of the file to upload, file should contain letter filenames, one per line""",
)
def get_notification_and_service_ids_for_letters_that_failed_to_print(file_name):
    print(f"Getting service and notification ids for letter filenames list {file_name}")
    file = open(file_name)
    references = tuple([row[7:23] for row in file])

    get_letters_data_from_references(tuple(references))
    file.close()


def get_letters_data_from_references(notification_references):
    sql = """
        SELECT id, service_id, template_id, reference, job_id, created_at
        FROM notifications
        WHERE reference IN :notification_references
        ORDER BY service_id, job_id"""
    result = db.session.execute(sql, {"notification_references": notification_references}).fetchall()

    with open("zips_sent_details.csv", "w") as csvfile:
        csv_writer = csv.writer(csvfile)
        csv_writer.writerow(["notification_id", "service_id", "template_id", "reference", "job_id", "created_at"])

        for row in result:
            csv_writer.writerow(row)


@notify_command(name="associate-services-to-organisations")
def associate_services_to_organisations():
    services = Service.get_history_model().query.filter_by(version=1).all()

    for s in services:
        created_by_user = User.query.filter_by(id=s.created_by_id).first()
        organisation = dao_get_organisation_by_email_address(created_by_user.email_address)
        service = dao_fetch_service_by_id(service_id=s.id)
        if organisation:
            dao_add_service_to_organisation(service=service, organisation_id=organisation.id)

    print("finished associating services to organisations")


@notify_command(name="populate-service-volume-intentions")
@click.option("-f", "--file_name", required=True, help="Pipe delimited file containing service_id, SMS, email, letters")
def populate_service_volume_intentions(file_name):
    # [0] service_id
    # [1] SMS:: volume intentions for service
    # [2] Email:: volume intentions for service
    # [3] Letters:: volume intentions for service

    with open(file_name) as f:
        for line in itertools.islice(f, 1, None):
            columns = line.split(",")
            print(columns)
            service = dao_fetch_service_by_id(columns[0])
            service.volume_sms = columns[1]
            service.volume_email = columns[2]
            service.volume_letter = columns[3]
            dao_update_service(service)
    print("populate-service-volume-intentions complete")


@notify_command(name="populate-go-live")
@click.option("-f", "--file_name", required=True, help="CSV file containing live service data")
def populate_go_live(file_name):
    # 0 - count, 1- Link, 2- Service ID, 3- DEPT, 4- Service Name, 5- Main contact,
    # 6- Contact detail, 7-MOU, 8- LIVE date, 9- SMS, 10 - Email, 11 - Letters, 12 -CRM, 13 - Blue badge
    import csv

    print("Populate go live user and date")
    with open(file_name) as f:
        rows = csv.reader(
            f,
            quoting=csv.QUOTE_MINIMAL,
            skipinitialspace=True,
        )
        print(next(rows))  # ignore header row
        for index, row in enumerate(rows):
            print(index, row)
            service_id = row[2]
            go_live_email = row[6]
            go_live_date = datetime.strptime(row[8], "%d/%m/%Y") + timedelta(hours=12)
            print(service_id, go_live_email, go_live_date)
            try:
                if go_live_email:
                    go_live_user = get_user_by_email(go_live_email)
                else:
                    go_live_user = None
            except NoResultFound:
                print("No user found for email address: ", go_live_email)
                continue
            try:
                service = dao_fetch_service_by_id(service_id)
            except NoResultFound:
                print("No service found for: ", service_id)
                continue
            service.go_live_user = go_live_user
            service.go_live_at = go_live_date
            dao_update_service(service)


@notify_command(name="fix-billable-units")
def fix_billable_units():
    query = Notification.query.filter(
        Notification.notification_type == SMS_TYPE,
        Notification.status != NOTIFICATION_CREATED,
        Notification.sent_at == None,  # noqa
        Notification.billable_units == 0,
        Notification.key_type != KEY_TYPE_TEST,
    )

    for notification in query.all():
        template_model = dao_get_template_by_id(notification.template_id, notification.template_version)

        template = SMSMessageTemplate(
            template_model.__dict__,
            values=notification.personalisation,
            prefix=notification.service.name,
            show_prefix=notification.service.prefix_sms,
        )
        print(f"Updating notification: {notification.id} with {template.fragment_count} billable_units")

        Notification.query.filter(Notification.id == notification.id).update(
            {"billable_units": template.fragment_count}
        )
    db.session.commit()
    print("End fix_billable_units")


@notify_command(name="process-row-from-job")
@click.option("-j", "--job_id", required=True, help="Job id")
@click.option("-n", "--job_row_number", type=int, required=True, help="Job id")
def process_row_from_job(job_id, job_row_number):
    job = dao_get_job_by_id(job_id)
    db_template = dao_get_template_by_id(job.template_id, job.template_version)

    template = db_template._as_utils_template()

    for row in RecipientCSV(
        s3.get_job_from_s3(str(job.service_id), str(job.id)),
        template_type=template.template_type,
        placeholders=template.placeholders,
    ).get_rows():
        if row.index == job_row_number:
            notification_id = process_row(row, template, job, job.service)
            current_app.logger.info(
                "Process row %s for job %s created notification_id: %s", job_row_number, job_id, notification_id
            )


@notify_command(name="populate-annual-billing-with-the-previous-years-allowance")
@click.option(
    "-y", "--year", required=True, type=int, help="""The year to populate the annual billing data for, i.e. 2019"""
)
def populate_annual_billing_with_the_previous_years_allowance(year):
    """
    add annual_billing for given year.
    """
    sql = """
        Select id from services where active = true
        except
        select service_id
        from annual_billing
        where financial_year_start = :year
    """
    services_without_annual_billing = db.session.execute(sql, {"year": year})
    for row in services_without_annual_billing:
        latest_annual_billing = """
            Select free_sms_fragment_limit
            from annual_billing
            where service_id = :service_id
            order by financial_year_start desc limit 1
        """
        free_allowance_rows = db.session.execute(latest_annual_billing, {"service_id": row.id})
        free_allowance = [x[0] for x in free_allowance_rows]
        print(f"create free limit of {free_allowance[0]} for service: {row.id}")
        dao_create_or_update_annual_billing_for_year(
            service_id=row.id, free_sms_fragment_limit=free_allowance[0], financial_year_start=int(year)
        )


@notify_command(name="functional-test-fixtures")
def functional_test_fixtures():
    """
    Apply fixtures for functional tests. Not intended for production use.

    The command will create all the database rows required for the funcitonal tests in an idempotent
    way and output an environment file the functional tests can use to execute against the environment.

    The environment file can be outputed to a file or uploaded to AWS SSM. The file is intended for local
    testing and ssm for the pipeline.

    It expects the following config to be set:

        NOTIFY_ENVIRONMENT
        MMG_INBOUND_SMS_USERNAME
        MMG_INBOUND_SMS_AUTH
        SQLALCHEMY_DATABASE_URI
        REDIS_URL
        SECRET_KEY
        INTERNAL_CLIENT_API_KEYS
        ADMIN_BASE_URL
        API_HOST_NAME
        FROM_NUMBER

    and the following environment variables:

        REQUEST_BIN_API_TOKEN - request bin token to be used by functional tests

        FUNCTIONAL_TEST_ENV_FILE - (optional) output file for the environment variables

        SSM_UPLOAD_PATH - (optional) path to upload the environment variables to AWS SSM

    """
    if current_app.config["REGISTER_FUNCTIONAL_TESTING_BLUEPRINT"]:
        apply_fixtures()
    else:
        print("Functional test fixtures are disabled. Set REGISTER_FUNCTIONAL_TESTING_BLUEPRINT to True in config.")
        raise SystemExit(1)


@click.option("-u", "--user-id", required=True)
@notify_command(name="generate-bulktest-data")
def generate_bulktest_data(user_id):
    if os.getenv("NOTIFY_ENVIRONMENT", "") not in ["development", "test"]:
        current_app.logger.error("Can only be run in development")
        return

    # Our logging setup spams lots of WARNING output for checking out DB conns outside of a request - hide them
    current_app.logger.setLevel(logging.ERROR)

    start = monotonic()
    user = User.query.get(user_id)

    def pprint(msg):
        now = monotonic()
        print(f"[{(now - start):>7.2f}]: {msg}")

    pprint("Building org...")
    org = Organisation(
        name="BULKTEST: Big Ol' Org",
        organisation_type="central",
    )
    db.session.add(org)
    pprint(" -> Sending org to DB...")
    db.session.flush()
    pprint(" -> Done.")

    pprint("Building services...")
    services = []
    for batch in range(100):
        service = Service(
            organisation_id=org.id,
            name=f"BULKTEST: Service {batch}",
            created_by_id=user_id,
            active=True,
            restricted=False,
            organisation_type="central",
            email_message_limit=250_000,
            sms_message_limit=250_000,
            letter_message_limit=250_000,
        )
        services.append(service)

        dao_create_service(service, user)
        set_default_free_allowance_for_service(service=service, year_start=None)

    pprint(" -> Sending services to DB...")
    db.session.flush()
    pprint(" -> Done.")

    # Not bothering to make a template for each service. For our purposes it shouldn't matter.
    pprint("Building templates...")
    TEMPLATES = {
        "email": Template(
            name="BULKTEST: email",
            service_id=services[0].id,
            template_type="email",
            subject="email",
            content="email body",
            created_by_id=user_id,
        ),
        "sms": Template(
            name="BULKTEST: sms",
            service_id=services[0].id,
            template_type="sms",
            subject="sms",
            content="sms body",
            created_by_id=user_id,
        ),
        "letter": Template(
            name="BULKTEST: letter",
            service_id=services[0].id,
            template_type="letter",
            subject="letter",
            content="letter body",
            postage="second",
            created_by_id=user_id,
        ),
    }

    dao_create_template(TEMPLATES["email"])
    dao_create_template(TEMPLATES["sms"])
    dao_create_template(TEMPLATES["letter"])
    pprint(" -> Sending templates to DB...")
    db.session.flush()
    pprint(" -> Done.")

    num_batches = 5
    batch_size = 1_000_000
    pprint(f"Building {batch_size * num_batches:,} notifications in batches of {batch_size:,}...")
    service_ids = [str(service.id) for service in services]
    last_new_year = datetime(datetime.today().year - 1, 1, 1, 12, 0, 0)
    daily_dates_since_last_new_year = list(rrule.rrule(freq=rrule.DAILY, dtstart=last_new_year, until=datetime.today()))
    for batch in range(num_batches):
        pprint(f" -> Building batch #{batch + 1}...")
        notifications_batch = []
        for i in range(batch_size):
            notification_num = (batch * batch_size) + i
            notification_type = random.choice(["sms", "letter", "email"])
            extra_kwargs = {"postage": "second"} if notification_type == "letter" else {}
            template = TEMPLATES[notification_type]
            notifications_batch.append(
                Notification(
                    to=f"BULKTEST-{notification_num}@notify.works",
                    normalised_to=f"BULKTEST-{notification_num}@notify.works",
                    job_id=None,
                    job_row_number=None,
                    service_id=random.choice(service_ids),
                    template_id=template.id,
                    template_version=1,
                    api_key_id=None,
                    key_type="normal",
                    billable_units=1,
                    rate_multiplier=1,
                    notification_type=notification_type,
                    created_at=random.choice(daily_dates_since_last_new_year),
                    status="delivered",
                    client_reference=f"BULKTEST: {notification_num}",
                    **extra_kwargs,
                )
            )
        pprint(f" -> Sending batch {batch + 1} to DB...")
        db.session.bulk_save_objects(notifications_batch)
        pprint(" -> Done.")

    pprint("Moving notifications older than 7 days to notification_history...")
    for i, service_id in enumerate(service_ids):
        for notification_type in ["email", "sms", "letter"]:
            with mock.patch("app.dao.notifications_dao._delete_letters_from_s3"):
                move_notifications_to_notification_history(
                    notification_type, service_id, datetime.utcnow() - timedelta(days=7), qry_limit=500_000
                )
        pprint(f" -> Service {i} done.")

    pprint("Building ft_billing for all periods")
    for dt in daily_dates_since_last_new_year:
        dt_date = dt.date()
        delete_billing_data_for_day(dt_date, service_ids)
        billing_data = fetch_billing_data_for_day(process_day=dt_date, service_ids=service_ids)
        update_ft_billing(billing_data, dt_date)
        pprint(f" -> Done {dt_date}")

    pprint("Committing...")
    db.session.commit()
    pprint("Finished.")
