from datetime import datetime
from itertools import product

from freezegun import freeze_time

from app import db
from app.constants import KEY_TYPE_NORMAL
from app.dao.inbound_numbers_dao import dao_get_inbound_number_for_service
from app.dao.inbound_sms_dao import (
    dao_count_inbound_sms_for_service,
    dao_get_inbound_sms_by_id,
    dao_get_inbound_sms_for_service,
    dao_get_most_recent_inbound_usage_date,
    dao_get_paginated_inbound_sms_for_service_for_public_api,
    dao_get_paginated_most_recent_inbound_sms_by_user_number_for_service,
    delete_inbound_sms_older_than_retention,
)
from app.models import InboundSmsHistory
from tests.app.db import (
    create_inbound_sms,
    create_job,
    create_notification,
    create_service,
    create_service_data_retention,
    create_template,
)
from tests.conftest import set_config


def test_get_all_inbound_sms(sample_service):
    inbound = create_inbound_sms(sample_service)

    res = dao_get_inbound_sms_for_service(sample_service.id)
    assert len(res) == 1
    assert res[0] == inbound


def test_get_all_inbound_sms_when_none_exist(sample_service):
    res = dao_get_inbound_sms_for_service(sample_service.id)
    assert len(res) == 0


def test_get_all_inbound_sms_limits_and_orders(sample_service):
    with freeze_time("2017-01-01"):
        create_inbound_sms(sample_service)
    with freeze_time("2017-01-03"):
        three = create_inbound_sms(sample_service)
    with freeze_time("2017-01-02"):
        two = create_inbound_sms(sample_service)

        res = dao_get_inbound_sms_for_service(sample_service.id, limit=2)

    assert len(res) == 2
    assert res[0] == three
    assert res[0].created_at == datetime(2017, 1, 3)
    assert res[1] == two
    assert res[1].created_at == datetime(2017, 1, 2)


def test_get_all_inbound_sms_filters_on_service(notify_db_session):
    service_one = create_service(service_name="one")
    service_two = create_service(service_name="two")

    sms_one = create_inbound_sms(service_one)
    create_inbound_sms(service_two)

    res = dao_get_inbound_sms_for_service(service_one.id)
    assert len(res) == 1
    assert res[0] == sms_one


def test_get_all_inbound_sms_filters_on_time(sample_service, notify_db_session):
    create_inbound_sms(sample_service, created_at=datetime(2017, 8, 6, 22, 59))  # sunday evening
    sms_two = create_inbound_sms(sample_service, created_at=datetime(2017, 8, 6, 23, 0))  # monday (7th) morning

    with freeze_time("2017-08-14 12:00"):
        res = dao_get_inbound_sms_for_service(sample_service.id, limit_days=7)

    assert len(res) == 1
    assert res[0] == sms_two


def test_count_inbound_sms_for_service(notify_db_session):
    service_one = create_service(service_name="one")
    service_two = create_service(service_name="two")

    create_inbound_sms(service_one)
    create_inbound_sms(service_one)
    create_inbound_sms(service_two)

    assert dao_count_inbound_sms_for_service(service_one.id, limit_days=1) == 2


def test_count_inbound_sms_for_service_filters_messages_older_than_n_days(sample_service):
    # test between evening sunday 2nd of june and morning of monday 3rd
    create_inbound_sms(sample_service, created_at=datetime(2019, 6, 2, 22, 59))
    create_inbound_sms(sample_service, created_at=datetime(2019, 6, 2, 22, 59))
    create_inbound_sms(sample_service, created_at=datetime(2019, 6, 2, 23, 1))

    with freeze_time("Monday 10th June 2019 12:00"):
        assert dao_count_inbound_sms_for_service(sample_service.id, limit_days=7) == 1


@freeze_time("2017-06-08 12:00:00")
def test_should_delete_inbound_sms_according_to_data_retention(notify_db_session):
    no_retention_service = create_service(service_name="no retention")
    short_retention_service = create_service(service_name="three days")
    long_retention_service = create_service(service_name="thirty days")

    services = [short_retention_service, no_retention_service, long_retention_service]

    create_service_data_retention(long_retention_service, notification_type="sms", days_of_retention=30)
    create_service_data_retention(short_retention_service, notification_type="sms", days_of_retention=3)
    create_service_data_retention(short_retention_service, notification_type="email", days_of_retention=4)

    dates = [
        datetime(2017, 6, 4, 23, 00),  # just before three days
        datetime(2017, 6, 4, 22, 59),  # older than three days
        datetime(2017, 5, 31, 23, 00),  # just before seven days
        datetime(2017, 5, 31, 22, 59),  # older than seven days
        datetime(2017, 5, 1, 0, 0),  # older than thirty days
    ]

    for date, service in product(dates, services):
        create_inbound_sms(service, created_at=date)

    deleted_count = delete_inbound_sms_older_than_retention()

    history = InboundSmsHistory.query.all()
    assert len(history) == 7

    # four deleted for the 3-day service, two for the default seven days one, one for the 30 day
    assert deleted_count == 7
    assert {x.created_at for x in dao_get_inbound_sms_for_service(short_retention_service.id)} == set(dates[:1])
    assert {x.created_at for x in dao_get_inbound_sms_for_service(no_retention_service.id)} == set(dates[:3])
    assert {x.created_at for x in dao_get_inbound_sms_for_service(long_retention_service.id)} == set(dates[:4])


@freeze_time("2019-12-20 12:00:00")
def test_insert_into_inbound_sms_history_when_deleting_inbound_sms(sample_service):
    create_inbound_sms(
        sample_service,
        created_at=datetime(2019, 12, 12, 20, 20),
        notify_number="07700900100",
        provider_date=datetime(2019, 12, 12, 20, 19),
        provider_reference="from daisy pie",
        provider="unicorn",
    )
    create_inbound_sms(sample_service, created_at=datetime(2019, 12, 19, 20, 19))

    delete_inbound_sms_older_than_retention()
    history = InboundSmsHistory.query.all()
    assert len(history) == 1

    for key_name in [
        "provider",
        "provider_date",
        "service_id",
        "created_at",
        "provider_reference",
        "notify_number",
        "id",
    ]:
        assert key_name in vars(history[0])

    for key_name in ["content", "user_number"]:
        assert key_name not in vars(history[0])

    assert history[0].notify_number == "07700900100"
    assert history[0].provider_date == datetime(2019, 12, 12, 20, 19)
    assert history[0].provider_reference == "from daisy pie"
    assert history[0].provider == "unicorn"
    assert history[0].created_at == datetime(2019, 12, 12, 20, 20)


@freeze_time("2019-12-20 12:00:00")
def test_delete_inbound_sms_older_than_retention_does_nothing_when_database_conflict_raised(sample_service):
    inbound_sms = create_inbound_sms(
        sample_service,
        created_at=datetime(2019, 12, 12, 20, 20),
        notify_number="07700900100",
        provider_date=datetime(2019, 12, 12, 20, 19),
        provider_reference="from daisy pie",
        provider="unicorn",
    )
    inbound_sms_id = inbound_sms.id

    # Insert data directly in to inbound_sms_history to mimic if we had run `delete_inbound_sms_older_than_retention`
    # before but for some reason the delete statement had failed
    conflict_creating_row = InboundSmsHistory(
        id=inbound_sms.id,
        service_id=inbound_sms.service.id,
        created_at=inbound_sms.created_at,
        notify_number=inbound_sms.notify_number,
        provider_date=inbound_sms.provider_date,
        provider_reference=inbound_sms.provider_reference,
        provider=inbound_sms.provider,
    )
    db.session.add(conflict_creating_row)
    db.session.commit()
    assert conflict_creating_row.id

    delete_inbound_sms_older_than_retention()

    history = InboundSmsHistory.query.all()
    assert len(history) == 1

    assert history[0].id == inbound_sms_id
    assert history[0].notify_number == "07700900100"
    assert history[0].provider_date == datetime(2019, 12, 12, 20, 19)
    assert history[0].provider_reference == "from daisy pie"
    assert history[0].provider == "unicorn"
    assert history[0].created_at == datetime(2019, 12, 12, 20, 20)


def test_get_inbound_sms_by_id_returns(sample_service):
    inbound_sms = create_inbound_sms(service=sample_service)
    inbound_from_db = dao_get_inbound_sms_by_id(inbound_sms.service.id, inbound_sms.id)

    assert inbound_sms == inbound_from_db


def test_dao_get_paginated_inbound_sms_for_service_for_public_api(sample_service):
    inbound_sms = create_inbound_sms(service=sample_service)
    inbound_from_db = dao_get_paginated_inbound_sms_for_service_for_public_api(inbound_sms.service.id)

    assert inbound_sms == inbound_from_db[0]


def test_dao_get_paginated_inbound_sms_for_service_for_public_api_return_only_for_service(sample_service):
    inbound_sms = create_inbound_sms(service=sample_service)
    another_service = create_service(service_name="another service")
    another_inbound_sms = create_inbound_sms(another_service)

    inbound_from_db = dao_get_paginated_inbound_sms_for_service_for_public_api(inbound_sms.service.id)

    assert inbound_sms in inbound_from_db
    assert another_inbound_sms not in inbound_from_db


def test_dao_get_paginated_inbound_sms_for_service_for_public_api_no_inbound_sms_returns_empty_list(sample_service):
    inbound_from_db = dao_get_paginated_inbound_sms_for_service_for_public_api(sample_service.id)

    assert inbound_from_db == []


def test_dao_get_paginated_inbound_sms_for_service_for_public_api_page_size_returns_correct_size(sample_service):
    inbound_sms_list = [
        create_inbound_sms(sample_service),
        create_inbound_sms(sample_service),
        create_inbound_sms(sample_service),
        create_inbound_sms(sample_service),
    ]
    reversed_inbound_sms = sorted(inbound_sms_list, key=lambda sms: sms.created_at, reverse=True)

    inbound_from_db = dao_get_paginated_inbound_sms_for_service_for_public_api(
        sample_service.id, older_than=reversed_inbound_sms[1].id, page_size=2
    )

    assert len(inbound_from_db) == 2


def test_dao_get_paginated_inbound_sms_for_service_for_public_api_older_than_returns_correct_list(sample_service):
    inbound_sms_list = [
        create_inbound_sms(sample_service),
        create_inbound_sms(sample_service),
        create_inbound_sms(sample_service),
        create_inbound_sms(sample_service),
    ]
    reversed_inbound_sms = sorted(inbound_sms_list, key=lambda sms: sms.created_at, reverse=True)

    inbound_from_db = dao_get_paginated_inbound_sms_for_service_for_public_api(
        sample_service.id, older_than=reversed_inbound_sms[1].id, page_size=2
    )

    expected_inbound_sms = reversed_inbound_sms[2:]

    assert expected_inbound_sms == inbound_from_db


def test_dao_get_paginated_inbound_sms_for_service_for_public_api_older_than_end_returns_empty_list(sample_service):
    inbound_sms_list = [
        create_inbound_sms(sample_service),
        create_inbound_sms(sample_service),
    ]
    reversed_inbound_sms = sorted(inbound_sms_list, key=lambda sms: sms.created_at, reverse=True)

    inbound_from_db = dao_get_paginated_inbound_sms_for_service_for_public_api(
        sample_service.id, older_than=reversed_inbound_sms[1].id, page_size=2
    )

    assert inbound_from_db == []


def test_most_recent_inbound_sms_only_returns_most_recent_for_each_number(notify_api, sample_service):
    create_inbound_sms(sample_service, user_number="447700900111", content="111 1", created_at=datetime(2017, 1, 1))
    create_inbound_sms(sample_service, user_number="447700900111", content="111 2", created_at=datetime(2017, 1, 2))
    create_inbound_sms(sample_service, user_number="447700900111", content="111 3", created_at=datetime(2017, 1, 3))
    create_inbound_sms(sample_service, user_number="447700900111", content="111 4", created_at=datetime(2017, 1, 4))
    create_inbound_sms(sample_service, user_number="447700900111", content="111 5", created_at=datetime(2017, 1, 5))
    create_inbound_sms(sample_service, user_number="447700900222", content="222 1", created_at=datetime(2017, 1, 1))
    create_inbound_sms(sample_service, user_number="447700900222", content="222 2", created_at=datetime(2017, 1, 2))

    with set_config(notify_api, "PAGE_SIZE", 3):
        with freeze_time("2017-01-02"):
            res = dao_get_paginated_most_recent_inbound_sms_by_user_number_for_service(
                sample_service.id, limit_days=7, page=1
            )

    assert len(res.items) == 2
    assert res.has_next is False
    assert res.per_page == 3
    assert res.items[0].content == "111 5"
    assert res.items[1].content == "222 2"


def test_most_recent_inbound_sms_paginates_properly(notify_api, sample_service):
    create_inbound_sms(sample_service, user_number="447700900111", content="111 1", created_at=datetime(2017, 1, 1))
    create_inbound_sms(sample_service, user_number="447700900111", content="111 2", created_at=datetime(2017, 1, 2))
    create_inbound_sms(sample_service, user_number="447700900222", content="222 1", created_at=datetime(2017, 1, 3))
    create_inbound_sms(sample_service, user_number="447700900222", content="222 2", created_at=datetime(2017, 1, 4))
    create_inbound_sms(sample_service, user_number="447700900333", content="333 1", created_at=datetime(2017, 1, 5))
    create_inbound_sms(sample_service, user_number="447700900333", content="333 2", created_at=datetime(2017, 1, 6))
    create_inbound_sms(sample_service, user_number="447700900444", content="444 1", created_at=datetime(2017, 1, 7))
    create_inbound_sms(sample_service, user_number="447700900444", content="444 2", created_at=datetime(2017, 1, 8))

    with set_config(notify_api, "PAGE_SIZE", 2):
        with freeze_time("2017-01-02"):
            # first page has most recent 444 and 333
            res = dao_get_paginated_most_recent_inbound_sms_by_user_number_for_service(
                sample_service.id, limit_days=7, page=1
            )
            assert len(res.items) == 2
            assert res.has_next is True
            assert res.per_page == 2
            assert res.items[0].content == "444 2"
            assert res.items[1].content == "333 2"

            # second page has no 444 or 333 - just most recent 222 and 111
            res = dao_get_paginated_most_recent_inbound_sms_by_user_number_for_service(
                sample_service.id, limit_days=7, page=2
            )
            assert len(res.items) == 2
            assert res.has_next is False
            assert res.items[0].content == "222 2"
            assert res.items[1].content == "111 2"


def test_most_recent_inbound_sms_only_returns_values_within_7_days(sample_service):
    # just out of bounds
    create_inbound_sms(sample_service, user_number="1", content="old", created_at=datetime(2017, 4, 2, 22, 59, 59))
    # just in bounds
    create_inbound_sms(sample_service, user_number="2", content="new", created_at=datetime(2017, 4, 2, 23, 0, 0))

    with freeze_time("Monday 10th April 2017 12:00:00"):
        res = dao_get_paginated_most_recent_inbound_sms_by_user_number_for_service(
            sample_service.id, limit_days=7, page=1
        )

    assert len(res.items) == 1
    assert res.items[0].content == "new"


def test_dao_get_most_recent_inbound_usage_date_notifications_table(sample_service, sample_inbound_numbers):
    template = create_template(service=sample_service)
    job = create_job(template=template)
    inbound = dao_get_inbound_number_for_service(sample_service.id)

    notification_time = datetime.utcnow()

    create_notification(
        template=template,
        job=job,
        job_row_number=None,
        to_field=None,
        status="created",
        reference=None,
        created_at=notification_time,
        sent_at=None,
        billable_units=1,
        personalisation=None,
        api_key=None,
        key_type=KEY_TYPE_NORMAL,
        reply_to_text=inbound.number,
    )

    most_recent_date = dao_get_most_recent_inbound_usage_date(sample_service.id, inbound)
    assert most_recent_date == notification_time


def test_dao_get_most_recent_inbound_usage_date_inbound_sms_table(sample_service, sample_inbound_numbers):
    sms_time = datetime.utcnow()
    create_inbound_sms(sample_service, created_at=sms_time)
    inbound = dao_get_inbound_number_for_service(sample_service.id)

    most_recent_date = dao_get_most_recent_inbound_usage_date(sample_service.id, inbound)
    assert most_recent_date == sms_time


def test_dao_get_most_recent_inbound_usage_date_inbound_sms_history_table(
    sample_service, sample_inbound_numbers, sample_inbound_sms_history
):
    inbound = next((x for x in sample_inbound_numbers if x.service_id == sample_service.id), None)
    most_recent_date = dao_get_most_recent_inbound_usage_date(sample_service.id, inbound)

    assert most_recent_date is not None
    assert most_recent_date.date() == sample_inbound_sms_history.created_at.date()


def test_dao_get_most_recent_inbound_usage_date_no_recent_notifications(sample_service, sample_inbound_numbers):
    inbound = next((x for x in sample_inbound_numbers if x.service_id == sample_service.id), None)
    assert dao_get_most_recent_inbound_usage_date(sample_service.id, inbound) is None
