from uuid import UUID

from sqlalchemy import desc

from app import db
from app.dao.dao_utils import autocommit
from app.exceptions import ArchiveValidationError
from app.models import ServiceSmsSender


def insert_service_sms_sender(service, sms_sender):
    """
    This method is called from create_service which is wrapped in a transaction.
    """
    new_sms_sender = ServiceSmsSender(sms_sender=sms_sender, service=service, is_default=True)
    db.session.add(new_sms_sender)


def dao_get_service_sms_senders_by_id(service_id, service_sms_sender_id):
    return ServiceSmsSender.query.filter_by(id=service_sms_sender_id, service_id=service_id, archived=False).one()


def dao_get_sms_senders_by_service_id(service_id):
    return (
        ServiceSmsSender.query.filter_by(service_id=service_id, archived=False)
        .order_by(desc(ServiceSmsSender.is_default))
        .all()
    )


@autocommit
def dao_add_sms_sender_for_service(service_id, sms_sender, is_default, inbound_number_id=None):
    old_default = _get_existing_default(service_id=service_id)
    if is_default:
        _reset_old_default_to_false(old_default)
    else:
        _raise_when_no_default(old_default)

    new_sms_sender = ServiceSmsSender(
        service_id=service_id, sms_sender=sms_sender, is_default=is_default, inbound_number_id=inbound_number_id
    )

    db.session.add(new_sms_sender)
    return new_sms_sender


@autocommit
def dao_update_service_sms_sender(service_id, service_sms_sender_id, is_default, sms_sender=None):
    old_default = _get_existing_default(service_id)
    if is_default:
        _reset_old_default_to_false(old_default)
    else:
        if old_default.id == service_sms_sender_id:
            raise Exception("You must have at least one SMS sender as the default")

    sms_sender_to_update = ServiceSmsSender.query.get(service_sms_sender_id)
    sms_sender_to_update.is_default = is_default
    if not sms_sender_to_update.inbound_number_id and sms_sender:
        sms_sender_to_update.sms_sender = sms_sender
    db.session.add(sms_sender_to_update)
    return sms_sender_to_update


@autocommit
def update_existing_sms_sender_with_inbound_number(service_sms_sender, sms_sender, inbound_number_id):
    service_sms_sender.sms_sender = sms_sender
    service_sms_sender.inbound_number_id = inbound_number_id
    db.session.add(service_sms_sender)
    return service_sms_sender


@autocommit
def archive_sms_sender(service_id, sms_sender_id):
    sms_sender_to_archive = ServiceSmsSender.query.filter_by(id=sms_sender_id, service_id=service_id).one()

    if sms_sender_to_archive.inbound_number_id:
        raise ArchiveValidationError("You cannot delete an inbound number")
    if sms_sender_to_archive.is_default:
        raise ArchiveValidationError("You cannot delete a default sms sender")

    sms_sender_to_archive.archived = True

    db.session.add(sms_sender_to_archive)
    return sms_sender_to_archive


def _get_existing_default(service_id):
    sms_senders = dao_get_sms_senders_by_service_id(service_id=service_id)
    if sms_senders:
        old_default = [x for x in sms_senders if x.is_default]
        if len(old_default) == 1:
            return old_default[0]
        else:
            raise Exception(
                f"There should only be one default sms sender for each service. "
                f"Service {service_id} has {len(old_default)}"
            )
    return None


def _reset_old_default_to_false(old_default):
    if old_default:
        old_default.is_default = False
        db.session.add(old_default)


def _raise_when_no_default(old_default):
    # check that the update is not updating the only default to false
    if not old_default:
        raise Exception("You must have at least one SMS sender as the default.", 400)


@autocommit
def dao_remove_inbound_sms_senders(service_id: UUID):
    return (
        ServiceSmsSender.query.filter_by(service_id=service_id)
        .filter(ServiceSmsSender.inbound_number_id.isnot(None))
        .delete(synchronize_session="fetch")
    )
