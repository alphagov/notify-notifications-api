import os
import subprocess
from collections import namedtuple
from contextlib import contextmanager
from urllib.parse import urlparse

import freezegun
import pytest
import sqlalchemy

from app import create_app, db, reset_memos
from app.authentication.auth import requires_admin_auth, requires_no_auth
from app.dao.provider_details_dao import get_provider_details_by_identifier
from app.notify_api_flask_app import NotifyApiFlaskApp
from tests.routes import test_admin_auth_blueprint, test_no_auth_blueprint

# Freezegun has a negative interaction with prompt_toolkit that ends up suppressing text written on the prompt of ipdb
# so let's ignore that module
# https://stackoverflow.com/questions/71584885/ipdb-stops-showing-prompt-text-after-carriage-return
# https://github.com/spulec/freezegun/pull/481
freezegun.configure(extend_ignore_list=["prompt_toolkit"])


@pytest.fixture(scope="session")
def notify_api():
    app = NotifyApiFlaskApp("test")
    create_app(app)

    # Attach some routes that can be used as helpers for specific tests.
    test_no_auth_blueprint.before_request(requires_no_auth)
    app.register_blueprint(test_no_auth_blueprint, url_prefix="/test")
    test_admin_auth_blueprint.before_request(requires_admin_auth)
    app.register_blueprint(test_admin_auth_blueprint, url_prefix="/admin-test")

    # deattach server-error error handlers - error_handler_spec looks like:
    #   {'blueprint_name': {
    #       status_code: [error_handlers],
    #       None: { ExceptionClass: error_handler }
    # }}
    for error_handlers in app.error_handler_spec.values():
        error_handlers.pop(500, None)
        if None in error_handlers:
            error_handlers[None] = {
                exc_class: error_handler
                for exc_class, error_handler in error_handlers[None].items()
                if exc_class != Exception
            }
            if error_handlers[None] == []:
                error_handlers.pop(None)

    ctx = app.app_context()
    ctx.push()

    yield app

    ctx.pop()
    reset_memos()


@pytest.fixture(scope="function")
def client(notify_api):
    with notify_api.test_request_context(), notify_api.test_client() as client:
        yield client


def create_test_db(database_uri):
    # get the
    db_uri_parts = database_uri.split("/")
    postgres_db_uri = "/".join(db_uri_parts[:-1] + ["postgres"])

    postgres_db = sqlalchemy.create_engine(
        postgres_db_uri, echo=False, isolation_level="AUTOCOMMIT", client_encoding="utf8"
    )
    try:
        result = postgres_db.execute(sqlalchemy.sql.text(f"CREATE DATABASE {db_uri_parts[-1]}"))
        result.close()
    except sqlalchemy.exc.ProgrammingError:
        # database "test_notification_api_master" already exists
        pass
    finally:
        postgres_db.dispose()


@pytest.fixture(scope="session")
def _notify_db(notify_api, worker_id):
    """
    Manages the connection to the database. Generally this shouldn't be used, instead you should use the
    `notify_db_session` fixture which also cleans up any data you've got left over after your test run.
    """
    from flask import current_app

    # the path as used with urlparse has a leading slash
    db_name = f"/test_notification_api_{worker_id}"
    db_uri = urlparse(str(db.engine.url))._replace(path=db_name).geturl()

    # create a database for this worker thread -
    current_app.config["SQLALCHEMY_DATABASE_URI"] = db_uri

    # reinitalise the db so it picks up on the new test database name
    db.init_app(notify_api)
    create_test_db(current_app.config["SQLALCHEMY_DATABASE_URI"])

    # Run this in a subprocess - alembic loads a lot of logging config that will otherwise splatter over our desired
    # app logging config and breaks pytest.caplog.
    result = subprocess.run(
        ["flask", "db", "upgrade"],
        env={
            **os.environ,
            "SQLALCHEMY_DATABASE_URI": current_app.config["SQLALCHEMY_DATABASE_URI"],
            "FLASK_APP": "application:application",
        },
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode()

    # now db is initialised, run cleanup on it to remove any artifacts from migrations (such as the notify service and
    # templates). Otherwise the very first test executed by a worker will be running on a different db setup to
    # other tests that run later.
    _clean_database(db)

    with notify_api.app_context():
        yield db

        db.session.remove()
        db.engine.dispose()


@pytest.fixture(scope="function")
def sms_providers(_notify_db):
    """
    In production we randomly choose which provider to use based on their priority. To guarantee tests run the same each
    time, make sure we always choose mmg. You'll need to override them in your tests if you wish to do something
    different.
    """
    get_provider_details_by_identifier("mmg").priority = 100
    get_provider_details_by_identifier("firetext").priority = 0


@pytest.fixture(scope="function")
def notify_db_session(_notify_db, sms_providers):
    """
    This fixture clears down all non static data after your test run. It yields the sqlalchemy session variable
    so you can manually add, commit, etc if needed.

    `notify_db_session.commit()`
    """
    yield _notify_db.session

    _clean_database(_notify_db)


def _clean_database(_db):
    _db.session.remove()
    for tbl in reversed(_db.metadata.sorted_tables):
        if tbl.name not in [
            "provider_details",
            "key_types",
            "branding_type",
            "job_status",
            "provider_details_history",
            "template_process_type",
            "notifications_all_time_view",
            "notification_status_types",
            "organisation_types",
            "service_permission_types",
            "auth_type",
            "invite_status_type",
            "service_callback_type",
            "default_annual_allowance",
        ]:
            _db.engine.execute(tbl.delete())
    _db.session.commit()


@pytest.fixture
def os_environ():
    """
    clear os.environ, and restore it after the test runs
    """
    # for use whenever you expect code to edit environment variables
    old_env = os.environ.copy()
    os.environ.clear()

    yield

    # clear afterwards in case anything extra was added to the environment during the test
    os.environ.clear()
    for k, v in old_env.items():
        os.environ[k] = v


@pytest.fixture(scope="session")
def hostnames(notify_api):
    api_url = notify_api.config["API_HOST_NAME"]
    admin_url = notify_api.config["ADMIN_BASE_URL"]
    template_preview_url = notify_api.config["TEMPLATE_PREVIEW_API_HOST"]

    return namedtuple("NotifyHostnames", ["api", "admin", "template_preview"])(
        api=api_url, admin=admin_url, template_preview=template_preview_url
    )


def pytest_generate_tests(metafunc):
    # Copied from https://gist.github.com/pfctdayelise/5719730
    idparametrize = metafunc.definition.get_closest_marker("idparametrize")
    if idparametrize:
        argnames, testdata = idparametrize.args
        ids, argvalues = zip(*sorted(testdata.items()), strict=True)
        metafunc.parametrize(argnames, argvalues, ids=ids)


@contextmanager
def set_config(app, name, value):
    old_val = app.config.get(name)
    app.config[name] = value
    try:
        yield
    finally:
        app.config[name] = old_val


@contextmanager
def set_config_values(app, dict):
    old_values = {}

    for key in dict:
        old_values[key] = app.config.get(key)
        app.config[key] = dict[key]

    try:
        yield
    finally:
        for key in dict:
            app.config[key] = old_values[key]


class Matcher:
    def __init__(self, description, key):
        self.description = description
        self.key = key

    def __eq__(self, other):
        return self.key(other)

    def __repr__(self):
        return f"<Matcher: {self.description}>"
