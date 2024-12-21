import uuid

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.dao.service_sms_sender_dao import (
    archive_sms_sender,
    dao_add_sms_sender_for_service,
    dao_get_service_sms_senders_by_id,
    dao_get_sms_senders_by_service_id,
    dao_remove_inbound_sms_senders,
    dao_update_service_sms_sender,
    update_existing_sms_sender_with_inbound_number,
)
from app.exceptions import ArchiveValidationError
from app.models import ServiceSmsSender
from tests.app.db import (
    create_inbound_number,
    create_service,
    create_service_sms_sender,
    create_service_with_inbound_number,
)


def test_dao_get_service_sms_senders_id(notify_db_session):
    service = create_service()
    second_sender = dao_add_sms_sender_for_service(
        service_id=service.id, sms_sender="second", is_default=False, inbound_number_id=None
    )
    result = dao_get_service_sms_senders_by_id(service_id=service.id, service_sms_sender_id=second_sender.id)
    assert result.sms_sender == "second"
    assert not result.is_default


def test_dao_get_service_sms_senders_id_raise_exception_when_not_found(notify_db_session):
    service = create_service()
    with pytest.raises(expected_exception=SQLAlchemyError):
        dao_get_service_sms_senders_by_id(service_id=service.id, service_sms_sender_id=uuid.uuid4())


def test_dao_get_service_sms_senders_id_raises_exception_with_archived_sms_sender(notify_db_session):
    service = create_service()
    archived_sms_sender = create_service_sms_sender(
        service=service, sms_sender="second", is_default=False, archived=True
    )
    with pytest.raises(expected_exception=SQLAlchemyError):
        dao_get_service_sms_senders_by_id(service_id=service.id, service_sms_sender_id=archived_sms_sender.id)


def test_dao_get_sms_senders_by_service_id(notify_db_session):
    service = create_service()
    second_sender = dao_add_sms_sender_for_service(
        service_id=service.id, sms_sender="second", is_default=False, inbound_number_id=None
    )
    results = dao_get_sms_senders_by_service_id(service_id=service.id)
    assert len(results) == 2
    for x in results:
        if x.is_default:
            assert x.sms_sender == "testing"
        else:
            assert x == second_sender


def test_dao_get_sms_senders_by_service_id_does_not_return_archived_senders(notify_db_session):
    service = create_service()
    archived_sms_sender = create_service_sms_sender(
        service=service, sms_sender="second", is_default=False, archived=True
    )
    results = dao_get_sms_senders_by_service_id(service_id=service.id)

    assert len(results) == 1
    assert archived_sms_sender not in results


def test_dao_add_sms_sender_for_service(notify_db_session):
    service = create_service()
    new_sms_sender = dao_add_sms_sender_for_service(
        service_id=service.id, sms_sender="new_sms", is_default=False, inbound_number_id=None
    )

    service_sms_senders = ServiceSmsSender.query.order_by(ServiceSmsSender.created_at).all()
    assert len(service_sms_senders) == 2
    assert service_sms_senders[0].sms_sender == "testing"
    assert service_sms_senders[0].is_default
    assert not service_sms_senders[0].archived
    assert service_sms_senders[1] == new_sms_sender


def test_dao_add_sms_sender_for_service_switches_default(notify_db_session):
    service = create_service()
    new_sms_sender = dao_add_sms_sender_for_service(
        service_id=service.id, sms_sender="new_sms", is_default=True, inbound_number_id=None
    )

    service_sms_senders = ServiceSmsSender.query.order_by(ServiceSmsSender.created_at).all()
    assert len(service_sms_senders) == 2
    assert service_sms_senders[0].sms_sender == "testing"
    assert not service_sms_senders[0].is_default
    assert service_sms_senders[1] == new_sms_sender


def test_dao_update_service_sms_sender(notify_db_session):
    service = create_service()
    service_sms_senders = ServiceSmsSender.query.filter_by(service_id=service.id).all()
    assert len(service_sms_senders) == 1
    sms_sender_to_update = service_sms_senders[0]

    dao_update_service_sms_sender(
        service_id=service.id, service_sms_sender_id=sms_sender_to_update.id, is_default=True, sms_sender="updated"
    )
    sms_senders = ServiceSmsSender.query.filter_by(service_id=service.id).all()
    assert len(sms_senders) == 1
    assert sms_senders[0].is_default
    assert sms_senders[0].sms_sender == "updated"
    assert not sms_senders[0].inbound_number_id


def test_dao_update_service_sms_sender_switches_default(notify_db_session):
    service = create_service()
    sms_sender = dao_add_sms_sender_for_service(
        service_id=service.id, sms_sender="new_sms", is_default=False, inbound_number_id=None
    )
    dao_update_service_sms_sender(
        service_id=service.id, service_sms_sender_id=sms_sender.id, is_default=True, sms_sender="updated"
    )
    sms_senders = ServiceSmsSender.query.filter_by(service_id=service.id).order_by(ServiceSmsSender.created_at).all()
    assert len(sms_senders) == 2
    assert sms_senders[0].sms_sender == "testing"
    assert not sms_senders[0].is_default
    assert sms_senders[1].sms_sender == "updated"
    assert sms_senders[1].is_default


def test_dao_update_service_sms_sender_raises_exception_when_no_default_after_update(notify_db_session):
    service = create_service()
    sms_sender = dao_add_sms_sender_for_service(
        service_id=service.id, sms_sender="new_sms", is_default=True, inbound_number_id=None
    )
    with pytest.raises(expected_exception=Exception) as e:
        dao_update_service_sms_sender(
            service_id=service.id, service_sms_sender_id=sms_sender.id, is_default=False, sms_sender="updated"
        )
    assert "You must have at least one SMS sender as the default" in str(e.value)


def test_update_existing_sms_sender_with_inbound_number(notify_db_session):
    service = create_service()
    inbound_number = create_inbound_number(number="12345", service_id=service.id)

    existing_sms_sender = ServiceSmsSender.query.filter_by(service_id=service.id).one()
    sms_sender = update_existing_sms_sender_with_inbound_number(
        service_sms_sender=existing_sms_sender, sms_sender=inbound_number.number, inbound_number_id=inbound_number.id
    )

    assert sms_sender.inbound_number_id == inbound_number.id
    assert sms_sender.sms_sender == inbound_number.number
    assert sms_sender.is_default


def test_update_existing_sms_sender_with_inbound_number_raises_exception_if_inbound_number_does_not_exist(
    notify_db_session,
):
    service = create_service()
    existing_sms_sender = ServiceSmsSender.query.filter_by(service_id=service.id).one()
    with pytest.raises(expected_exception=SQLAlchemyError):
        update_existing_sms_sender_with_inbound_number(
            service_sms_sender=existing_sms_sender, sms_sender="blah", inbound_number_id=uuid.uuid4()
        )


def test_archive_sms_sender(notify_db_session):
    service = create_service()
    second_sms_sender = dao_add_sms_sender_for_service(service_id=service.id, sms_sender="second", is_default=False)

    archive_sms_sender(service_id=service.id, sms_sender_id=second_sms_sender.id)

    assert second_sms_sender.archived is True
    assert second_sms_sender.updated_at is not None


def test_archive_sms_sender_does_not_archive_a_sender_for_a_different_service(sample_service):
    service = create_service(service_name="First service")
    sms_sender = dao_add_sms_sender_for_service(service_id=sample_service.id, sms_sender="second", is_default=False)

    with pytest.raises(SQLAlchemyError):
        archive_sms_sender(service.id, sms_sender.id)

    assert not sms_sender.archived


def test_archive_sms_sender_raises_an_error_if_attempting_to_archive_a_default(notify_db_session):
    service = create_service()
    sms_sender = service.service_sms_senders[0]

    with pytest.raises(ArchiveValidationError) as e:
        archive_sms_sender(service_id=service.id, sms_sender_id=sms_sender.id)

    assert "You cannot delete a default sms sender" in str(e.value)


@pytest.mark.parametrize("is_default", [True, False])
def test_archive_sms_sender_raises_an_error_if_attempting_to_archive_an_inbound_number(notify_db_session, is_default):
    service = create_service_with_inbound_number(inbound_number="7654321")
    dao_add_sms_sender_for_service(service.id, "second", is_default=True)

    inbound_number = next(x for x in service.service_sms_senders if x.inbound_number_id)

    # regardless of whether inbound number is default or not, can't delete it
    dao_update_service_sms_sender(service.id, inbound_number.id, is_default=is_default)

    with pytest.raises(ArchiveValidationError) as e:
        archive_sms_sender(service_id=service.id, sms_sender_id=inbound_number.id)

    assert "You cannot delete an inbound number" in str(e.value)
    assert not inbound_number.archived


def test_dao_remove_inbound_sms_senders():
    inbound_number = "7654321"
    service = create_service_with_inbound_number(inbound_number, service_name="inbound number service")
    sms_senders = dao_get_sms_senders_by_service_id(service.id)
    assert len(sms_senders) == 1
    assert any(x.sms_sender == inbound_number for x in sms_senders) is True
    assert sms_senders[0].inbound_number_id is not None

    # adding null inbound_number_id for sms_sender
    dao_add_sms_sender_for_service(service.id, "second", is_default=True)

    sms_senders = dao_get_sms_senders_by_service_id(service.id)
    assert len(sms_senders) == 2
    assert any(x.inbound_number_id is None for x in sms_senders) is True

    # removing only rows that are not null
    dao_remove_inbound_sms_senders(service.id)

    sms_senders = dao_get_sms_senders_by_service_id(service.id)
    assert len(sms_senders) == 1

    # check value with null inbound_number_id is still present
    assert any(x.inbound_number_id is None for x in sms_senders) is True
    # check value that had inbound_number_id is gone
    assert any(x.inbound_number_id is not None for x in sms_senders) is False
