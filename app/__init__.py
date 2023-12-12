import os
import random
import string
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from time import monotonic

from celery import current_task
from flask import (
    current_app,
    g,
    has_request_context,
    jsonify,
    make_response,
    request,
)
from flask_marshmallow import Marshmallow
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from gds_metrics import GDSMetrics
from gds_metrics.metrics import Gauge, Histogram
from notifications_utils import logging, request_helper
from notifications_utils.celery import NotifyCelery
from notifications_utils.clients.redis.redis_client import RedisClient
from notifications_utils.clients.signing.signing_client import Signing
from notifications_utils.clients.statsd.statsd_client import StatsdClient
from notifications_utils.clients.zendesk.zendesk_client import ZendeskClient
from notifications_utils.local_vars import LazyLocalGetter
from sqlalchemy import event
from werkzeug.exceptions import HTTPException as WerkzeugHTTPException
from werkzeug.local import LocalProxy

from app.clients import NotificationProviderClients
from app.clients.document_download import DocumentDownloadClient
from app.clients.email.aws_ses import AwsSesClient
from app.clients.email.aws_ses_stub import AwsSesStubClient
from app.clients.letter.dvla import DVLAClient
from app.clients.sms.firetext import FiretextClient
from app.clients.sms.mmg import MMGClient

db = SQLAlchemy()
migrate = Migrate()
ma = Marshmallow()
notify_celery = NotifyCelery()
signing = Signing()
zendesk_client = ZendeskClient()
statsd_client = StatsdClient()
redis_store = RedisClient()
metrics = GDSMetrics()

api_user = LocalProxy(lambda: g.api_user)
authenticated_service = LocalProxy(lambda: g.authenticated_service)

CONCURRENT_REQUESTS = Gauge(
    "concurrent_web_request_count",
    "How many concurrent requests are currently being served",
)

memo_resetters: list[Callable] = []

#
# "clients" that need thread-local copies
#

_firetext_client_context_var: ContextVar[FiretextClient] = ContextVar("firetext_client")
get_firetext_client: LazyLocalGetter[FiretextClient] = LazyLocalGetter(
    _firetext_client_context_var,
    lambda: FiretextClient(current_app, statsd_client=statsd_client),
    expected_type=FiretextClient,
)
memo_resetters.append(lambda: get_firetext_client.clear())
firetext_client = LocalProxy(get_firetext_client)

_mmg_client_context_var: ContextVar[MMGClient] = ContextVar("mmg_client")
get_mmg_client: LazyLocalGetter[MMGClient] = LazyLocalGetter(
    _mmg_client_context_var,
    lambda: MMGClient(current_app, statsd_client=statsd_client),
    expected_type=MMGClient,
)
memo_resetters.append(lambda: get_mmg_client.clear())
mmg_client = LocalProxy(get_mmg_client)

_aws_ses_client_context_var: ContextVar[AwsSesClient] = ContextVar("aws_ses_client")
get_aws_ses_client: LazyLocalGetter[AwsSesClient] = LazyLocalGetter(
    _aws_ses_client_context_var,
    lambda: AwsSesClient(current_app.config["AWS_REGION"], statsd_client=statsd_client),
    expected_type=AwsSesClient,
)
memo_resetters.append(lambda: get_aws_ses_client.clear())
aws_ses_client = LocalProxy(get_aws_ses_client)

_aws_ses_stub_client_context_var: ContextVar[AwsSesStubClient] = ContextVar("aws_ses_stub_client")
get_aws_ses_stub_client: LazyLocalGetter[AwsSesStubClient] = LazyLocalGetter(
    _aws_ses_stub_client_context_var,
    lambda: AwsSesStubClient(
        current_app.config["AWS_REGION"],
        statsd_client=statsd_client,
        stub_url=current_app.config["SES_STUB_URL"],
    ),
    expected_type=AwsSesStubClient,
)
memo_resetters.append(lambda: get_aws_ses_stub_client.clear())
aws_ses_stub_client = LocalProxy(get_aws_ses_stub_client)

_notification_provider_clients_context_var: ContextVar[NotificationProviderClients] = ContextVar(
    "notification_provider_clients"
)
get_notification_provider_clients: LazyLocalGetter[NotificationProviderClients] = LazyLocalGetter(
    _notification_provider_clients_context_var,
    lambda: NotificationProviderClients(
        sms_clients={
            getter.expected_type.name: LocalProxy(getter)
            for getter in (
                get_firetext_client,
                get_mmg_client,
            )
        },
        email_clients={
            getter.expected_type.name: LocalProxy(getter)
            # If a stub url is provided for SES, then use the stub client rather
            # than the real SES boto client
            for getter in ((get_aws_ses_stub_client,) if current_app.config["SES_STUB_URL"] else (get_aws_ses_client,))
        },
    ),
)
memo_resetters.append(lambda: get_notification_provider_clients.clear())
notification_provider_clients = LocalProxy(get_notification_provider_clients)


_dvla_client_context_var: ContextVar[DVLAClient] = ContextVar("dvla_client")
get_dvla_client: LazyLocalGetter[DVLAClient] = LazyLocalGetter(
    _dvla_client_context_var,
    lambda: DVLAClient(current_app, statsd_client=statsd_client),
)
memo_resetters.append(lambda: get_dvla_client.clear())
dvla_client = LocalProxy(get_dvla_client)

_document_download_client_context_var: ContextVar[DocumentDownloadClient] = ContextVar("document_download_client")
get_document_download_client: LazyLocalGetter[DocumentDownloadClient] = LazyLocalGetter(
    _document_download_client_context_var,
    lambda: DocumentDownloadClient(current_app),
)
memo_resetters.append(lambda: get_document_download_client.clear())
document_download_client = LocalProxy(get_document_download_client)


def create_app(application):
    from app.config import Config, configs

    notify_environment = os.environ["NOTIFY_ENVIRONMENT"]

    if notify_environment in configs:
        application.config.from_object(configs[notify_environment])
    else:
        application.config.from_object(Config)

    application.config["NOTIFY_APP_NAME"] = application.name
    application.config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]["application_name"] = os.environ.get(
        "NOTIFY_APP_NAME", "api"
    )
    init_app(application)

    # Metrics intentionally high up to give the most accurate timing and reliability that the metric is recorded
    metrics.init_app(application)
    request_helper.init_app(application)
    db.init_app(application)
    migrate.init_app(application, db=db)
    ma.init_app(application)
    zendesk_client.init_app(application)
    statsd_client.init_app(application)
    logging.init_app(application, statsd_client)

    notify_celery.init_app(application)
    signing.init_app(application)
    redis_store.init_app(application)

    register_blueprint(application)
    register_v2_blueprints(application)

    # avoid circular imports by importing this file later
    from app.commands import setup_commands

    setup_commands(application)

    # set up sqlalchemy events
    setup_sqlalchemy_events(application)

    return application


def reset_memos():
    """
    Reset all memos registered in memo_resetters
    """
    for resetter in memo_resetters:
        resetter()


def register_blueprint(application):
    from app.authentication.auth import (
        requires_admin_auth,
        requires_functional_test_auth,
        requires_no_auth,
    )
    from app.billing.rest import billing_blueprint
    from app.complaint.complaint_rest import complaint_blueprint
    from app.email_branding.rest import email_branding_blueprint
    from app.events.rest import events as events_blueprint
    from app.functional_tests import test_blueprint
    from app.inbound_number.rest import inbound_number_blueprint
    from app.inbound_sms.rest import inbound_sms as inbound_sms_blueprint
    from app.job.rest import job_blueprint
    from app.letter_attachment.rest import letter_attachment_blueprint
    from app.letter_branding.letter_branding_rest import (
        letter_branding_blueprint,
    )
    from app.letters.rest import letter_job, letter_rates_blueprint
    from app.notifications.notifications_letter_callback import (
        letter_callback_blueprint,
    )
    from app.notifications.notifications_sms_callback import (
        sms_callback_blueprint,
    )
    from app.notifications.receive_notifications import (
        receive_notifications_blueprint,
    )
    from app.one_click_unsubscribe.rest import one_click_unsubscribe_blueprint
    from app.organisation.invite_rest import organisation_invite_blueprint
    from app.organisation.rest import organisation_blueprint
    from app.performance_dashboard.rest import performance_dashboard_blueprint
    from app.platform_admin.rest import platform_admin_blueprint
    from app.platform_stats.rest import platform_stats_blueprint
    from app.protected_sender_id.rest import protected_sender_id_blueprint
    from app.provider_details.rest import (
        provider_details as provider_details_blueprint,
    )
    from app.service.callback_rest import service_callback_blueprint
    from app.service.rest import service_blueprint
    from app.service_invite.rest import (
        service_invite as service_invite_blueprint,
    )
    from app.sms.rest import sms_rate_blueprint
    from app.status.healthcheck import status as status_blueprint
    from app.template.rest import template_blueprint
    from app.template_folder.rest import template_folder_blueprint
    from app.template_statistics.rest import (
        template_statistics as template_statistics_blueprint,
    )
    from app.upload.rest import upload_blueprint
    from app.user.rest import user_blueprint
    from app.webauthn.rest import webauthn_blueprint

    def ensure_user_id_attribute_before_request():
        g.user_id = None

    application.before_request(ensure_user_id_attribute_before_request)

    service_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(service_blueprint, url_prefix="/service")

    user_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(user_blueprint, url_prefix="/user")

    webauthn_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(webauthn_blueprint)

    template_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(template_blueprint)

    status_blueprint.before_request(requires_no_auth)
    application.register_blueprint(status_blueprint)

    # delivery receipts
    sms_callback_blueprint.before_request(requires_no_auth)
    application.register_blueprint(sms_callback_blueprint)

    # inbound sms
    receive_notifications_blueprint.before_request(requires_no_auth)
    application.register_blueprint(receive_notifications_blueprint)

    job_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(job_blueprint)

    service_invite_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(service_invite_blueprint)

    organisation_invite_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(organisation_invite_blueprint)

    inbound_number_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(inbound_number_blueprint)

    inbound_sms_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(inbound_sms_blueprint)

    template_statistics_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(template_statistics_blueprint)

    events_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(events_blueprint)

    provider_details_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(provider_details_blueprint, url_prefix="/provider-details")

    email_branding_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(email_branding_blueprint, url_prefix="/email-branding")

    letter_job.before_request(requires_admin_auth)
    application.register_blueprint(letter_job)

    letter_rates_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(letter_rates_blueprint)

    letter_callback_blueprint.before_request(requires_no_auth)
    application.register_blueprint(letter_callback_blueprint)

    billing_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(billing_blueprint)

    service_callback_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(service_callback_blueprint)

    organisation_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(organisation_blueprint, url_prefix="/organisations")

    complaint_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(complaint_blueprint)

    performance_dashboard_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(performance_dashboard_blueprint)

    platform_stats_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(platform_stats_blueprint, url_prefix="/platform-stats")

    protected_sender_id_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(protected_sender_id_blueprint, url_prefix="/protected-sender-id")

    template_folder_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(template_folder_blueprint)

    letter_branding_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(letter_branding_blueprint)

    upload_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(upload_blueprint)

    platform_admin_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(platform_admin_blueprint, url_prefix="/platform-admin")

    letter_attachment_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(letter_attachment_blueprint)

    sms_rate_blueprint.before_request(requires_admin_auth)
    application.register_blueprint(sms_rate_blueprint)

    one_click_unsubscribe_blueprint.before_request(requires_no_auth)
    application.register_blueprint(one_click_unsubscribe_blueprint)

    if application.config["REGISTER_FUNCTIONAL_TESTING_BLUEPRINT"]:
        test_blueprint.before_request(requires_functional_test_auth)
        application.register_blueprint(test_blueprint)


def register_v2_blueprints(application):
    from app.authentication.auth import requires_auth
    from app.v2.inbound_sms.get_inbound_sms import v2_inbound_sms_blueprint
    from app.v2.notifications import (  # noqa
        get_notifications,
        post_notifications,
        v2_notification_blueprint,
    )
    from app.v2.template import (  # noqa
        get_template,
        post_template,
        v2_template_blueprint,
    )
    from app.v2.templates.get_templates import v2_templates_blueprint

    v2_notification_blueprint.before_request(requires_auth)
    application.register_blueprint(v2_notification_blueprint)

    v2_templates_blueprint.before_request(requires_auth)
    application.register_blueprint(v2_templates_blueprint)

    v2_template_blueprint.before_request(requires_auth)
    application.register_blueprint(v2_template_blueprint)

    v2_inbound_sms_blueprint.before_request(requires_auth)
    application.register_blueprint(v2_inbound_sms_blueprint)


def init_app(app):
    @app.before_request
    def record_request_details():
        CONCURRENT_REQUESTS.inc()

        g.start = monotonic()
        g.endpoint = request.endpoint

    @app.after_request
    def after_request(response):
        CONCURRENT_REQUESTS.dec()

        response.headers.add("Access-Control-Allow-Origin", "*")
        response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE")
        return response

    @app.errorhandler(Exception)
    def exception(error):
        app.logger.exception(error)
        # error.code is set for our exception types.
        msg = getattr(error, "message", str(error))
        code = getattr(error, "code", 500)
        return jsonify(result="error", message=msg), code

    @app.errorhandler(WerkzeugHTTPException)
    def werkzeug_exception(e):
        return make_response(jsonify(result="error", message=e.description), e.code, e.get_headers())

    @app.errorhandler(404)
    def page_not_found(e):
        msg = e.description or "Not found"
        return jsonify(result="error", message=msg), 404


def create_uuid():
    return str(uuid.uuid4())


def create_random_identifier():
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(16))


def setup_sqlalchemy_events(app):
    TOTAL_DB_CONNECTIONS = Gauge(
        "db_connection_total_connected",
        "How many db connections are currently held (potentially idle) by the server",
    )

    TOTAL_CHECKED_OUT_DB_CONNECTIONS = Gauge(
        "db_connection_total_checked_out",
        "How many db connections are currently checked out by web requests",
    )

    DB_CONNECTION_OPEN_DURATION_SECONDS = Histogram(
        "db_connection_open_duration_seconds",
        "How long db connections are held open for in seconds",
        ["method", "host", "path"],
    )

    # need this or db.engine isn't accessible
    with app.app_context():

        @event.listens_for(db.engine, "connect")
        def connect(dbapi_connection, connection_record):
            # connection first opened with db
            TOTAL_DB_CONNECTIONS.inc()

            # by default disable parallel query because it allows large analytic-style
            # queries to consume more resources than smaller transactional queries
            # typically will, and if anything we want to prioritize the small
            # transactional queries. this can be re-enabled on a case-by-case basis by
            # executing SET LOCAL max_parallel_workers_per_gather = ... before the
            # intended query.
            #
            # because this is only done once at connection-creation time, there's a small
            # danger that SET max_par... (instead of SET LOCAL max_par...) will be used
            # by the application somewhere, which would persist across checkouts.
            # however, (re-)setting this on every checkout would likely add a database
            # round-trip of latency to every request.
            dbapi_connection.cursor().execute("SET max_parallel_workers_per_gather = 0")

        @event.listens_for(db.engine, "close")
        def close(dbapi_connection, connection_record):
            # connection closed (probably only happens with overflow connections)
            TOTAL_DB_CONNECTIONS.dec()

        @event.listens_for(db.engine, "checkout")
        def checkout(dbapi_connection, connection_record, connection_proxy):
            try:
                # connection given to a web worker
                TOTAL_CHECKED_OUT_DB_CONNECTIONS.inc()

                # this will overwrite any previous checkout_at timestamp
                connection_record.info["checkout_at"] = time.monotonic()

                # checkin runs after the request is already torn down, therefore we add the request_data onto the
                # connection_record as otherwise it won't have that information when checkin actually runs.
                # Note: this is not a problem for checkouts as the checkout always happens within a web request or task

                # web requests
                if has_request_context():
                    connection_record.info["request_data"] = {
                        "method": request.method,
                        "host": request.host,
                        "url_rule": request.url_rule.rule if request.url_rule else "No endpoint",
                    }
                # celery apps
                elif current_task:
                    connection_record.info["request_data"] = {
                        "method": "celery",
                        "host": current_app.config["NOTIFY_APP_NAME"],  # worker name
                        "url_rule": current_task.name,  # task name
                    }
                # anything else. migrations possibly, or flask cli commands.
                else:
                    current_app.logger.warning("Checked out sqlalchemy connection from outside of request/task")
                    connection_record.info["request_data"] = {
                        "method": "unknown",
                        "host": "unknown",
                        "url_rule": "unknown",
                    }
            except Exception:
                current_app.logger.exception("Exception caught for checkout event.")

        @event.listens_for(db.engine, "checkin")
        def checkin(dbapi_connection, connection_record):
            if "checkout_at" not in connection_record.info or "request_data" not in connection_record.info:
                # we can get in this inconsistent state if the database is shutting down
                return

            try:
                # connection returned by a web worker
                TOTAL_CHECKED_OUT_DB_CONNECTIONS.dec()

                # duration that connection was held by a single web request
                duration = time.monotonic() - connection_record.info["checkout_at"]

                DB_CONNECTION_OPEN_DURATION_SECONDS.labels(
                    connection_record.info["request_data"]["method"],
                    connection_record.info["request_data"]["host"],
                    connection_record.info["request_data"]["url_rule"],
                ).observe(duration)
            except Exception:
                current_app.logger.exception("Exception caught for checkin event.")
