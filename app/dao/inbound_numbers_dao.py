from uuid import UUID

from app import db
from app.dao.dao_utils import autocommit
from app.models import InboundNumber


def dao_get_inbound_numbers():
    return InboundNumber.query.order_by(InboundNumber.updated_at).all()


def dao_get_available_inbound_numbers():
    return InboundNumber.query.filter(InboundNumber.active, InboundNumber.service_id.is_(None)).all()


def dao_get_inbound_number_for_service(service_id):
    return InboundNumber.query.filter(InboundNumber.service_id == service_id).first()


def dao_get_inbound_number(inbound_number_id):
    return InboundNumber.query.filter(InboundNumber.id == inbound_number_id).first()


@autocommit
def dao_set_inbound_number_to_service(service_id, inbound_number):
    inbound_number.service_id = service_id
    db.session.add(inbound_number)


@autocommit
def dao_set_inbound_number_active_flag(service_id, active):
    inbound_number = InboundNumber.query.filter(InboundNumber.service_id == service_id).first()
    inbound_number.active = active

    db.session.add(inbound_number)


@autocommit
def dao_allocate_number_for_service(service_id, inbound_number_id):
    updated = InboundNumber.query.filter_by(id=inbound_number_id, active=True, service_id=None).update(
        {"service_id": service_id}
    )
    if not updated:
        raise Exception(f"Inbound number: {inbound_number_id} is not available")
    return InboundNumber.query.get(inbound_number_id)


@autocommit
def archive_or_release_inbound_number_for_service(service_id: UUID, archive: bool):
    update_data = {
        "service_id": None,
    }

    if archive:
        update_data["active"] = False

    return InboundNumber.query.filter_by(service_id=service_id, active=True).update(
        update_data, synchronize_session="fetch"
    )
