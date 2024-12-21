import pytest
from sqlalchemy.exc import IntegrityError

from app.dao.inbound_numbers_dao import (
    archive_or_release_inbound_number_for_service,
    dao_allocate_number_for_service,
    dao_get_available_inbound_numbers,
    dao_get_inbound_number_for_service,
    dao_get_inbound_numbers,
    dao_set_inbound_number_active_flag,
    dao_set_inbound_number_to_service,
)
from app.models import InboundNumber
from tests.app.db import create_inbound_number, create_service


def test_get_inbound_numbers(notify_db_session, sample_inbound_numbers):
    res = dao_get_inbound_numbers()

    assert len(res) == len(sample_inbound_numbers)
    assert res == sample_inbound_numbers


def test_get_available_inbound_numbers(notify_db_session):
    inbound_number = create_inbound_number(number="1")

    res = dao_get_available_inbound_numbers()

    assert len(res) == 1
    assert res[0] == inbound_number


def test_set_service_id_on_inbound_number(notify_db_session, sample_inbound_numbers):
    service = create_service(service_name="test service")
    numbers = dao_get_available_inbound_numbers()

    dao_set_inbound_number_to_service(service.id, numbers[0])

    res = InboundNumber.query.filter(InboundNumber.service_id == service.id).all()

    assert len(res) == 1
    assert res[0].service_id == service.id


def test_after_setting_service_id_that_inbound_number_is_unavailable(notify_db_session, sample_inbound_numbers):
    service = create_service(service_name="test service")
    numbers = dao_get_available_inbound_numbers()

    assert len(numbers) == 1

    dao_set_inbound_number_to_service(service.id, numbers[0])

    res = dao_get_available_inbound_numbers()

    assert len(res) == 0


def test_setting_a_service_twice_will_raise_an_error(notify_db_session):
    create_inbound_number(number="1")
    create_inbound_number(number="2")
    service = create_service(service_name="test service")
    numbers = dao_get_available_inbound_numbers()

    dao_set_inbound_number_to_service(service.id, numbers[0])

    with pytest.raises(IntegrityError) as e:
        dao_set_inbound_number_to_service(service.id, numbers[1])

    assert "duplicate key value violates unique constraint" in str(e.value)


@pytest.mark.parametrize("active", [True, False])
def test_set_inbound_number_active_flag(notify_db_session, sample_service, active):
    inbound_number = create_inbound_number(number="1")
    dao_set_inbound_number_to_service(sample_service.id, inbound_number)

    dao_set_inbound_number_active_flag(sample_service.id, active=active)

    inbound_number = dao_get_inbound_number_for_service(sample_service.id)

    assert inbound_number.active is active


def test_dao_allocate_number_for_service(notify_db_session):
    number = "078945612"
    inbound_number = create_inbound_number(number=number)
    service = create_service()

    updated_inbound_number = dao_allocate_number_for_service(service_id=service.id, inbound_number_id=inbound_number.id)
    assert service.get_inbound_number() == number
    assert updated_inbound_number.service_id == service.id


def test_dao_allocate_number_for_service_raises_if_inbound_number_already_taken(notify_db_session, sample_service):
    number = "078945612"
    inbound_number = create_inbound_number(number=number, service_id=sample_service.id)
    service = create_service(service_name="Service needs an inbound number")
    with pytest.raises(Exception) as exc:
        dao_allocate_number_for_service(service_id=service.id, inbound_number_id=inbound_number.id)
    assert "is not available" in str(exc.value)


def test_dao_allocate_number_for_service_raises_if_invalid_inbound_number(notify_db_session, fake_uuid):
    service = create_service(service_name="Service needs an inbound number")
    with pytest.raises(Exception) as exc:
        dao_allocate_number_for_service(service_id=service.id, inbound_number_id=fake_uuid)
    assert "is not available" in str(exc.value)


def test_archive_or_release_inbound_number_for_service_archive(sample_service, sample_inbound_numbers):
    inbound = next((inbound for inbound in sample_inbound_numbers if inbound.service_id == sample_service.id), None)

    archive_or_release_inbound_number_for_service(sample_service.id, True)

    updated_inbound = InboundNumber.query.filter_by(number=inbound.number).one_or_none()

    assert updated_inbound.service_id is None
    assert updated_inbound.active is False


def test_archive_or_release_inbound_number_for_service_release(sample_service, sample_inbound_numbers):
    inbound = next((inbound for inbound in sample_inbound_numbers if inbound.service_id == sample_service.id), None)

    archive_or_release_inbound_number_for_service(sample_service.id, False)

    updated_inbound = InboundNumber.query.filter_by(number=inbound.number).one_or_none()

    assert updated_inbound.service_id is None
    assert updated_inbound.active is True
