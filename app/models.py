import datetime
import enum
import uuid
from dataclasses import dataclass

from flask import current_app, url_for
from notifications_utils.insensitive_dict import InsensitiveDict
from notifications_utils.letter_timings import get_letter_timings
from notifications_utils.recipient_validation.email_address import validate_email_address
from notifications_utils.recipient_validation.errors import InvalidRecipientError
from notifications_utils.recipient_validation.phone_number import (
    try_validate_and_format_phone_number,
    validate_phone_number,
)
from notifications_utils.recipient_validation.postal_address import (
    address_lines_1_to_6_and_postcode_keys,
)
from notifications_utils.safe_string import make_string_safe_for_email_local_part
from notifications_utils.template import (
    LetterPrintTemplate,
    PlainTextEmailTemplate,
    SMSMessageTemplate,
)
from sqlalchemy import (
    CheckConstraint,
    Index,
    String,
    UniqueConstraint,
    and_,
    func,
)
from sqlalchemy.dialects.postgresql import JSON, JSONB, UUID
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm.collections import attribute_mapped_collection

from app import db, redis_store, signing
from app.constants import (
    BRANDING_ORG,
    EMAIL_TYPE,
    GUEST_LIST_RECIPIENT_TYPE,
    INVITE_PENDING,
    INVITED_USER_STATUS_TYPES,
    JOIN_REQUEST_PENDING,
    LETTER_TYPE,
    MOBILE_TYPE,
    NORMAL,
    NOTIFICATION_CREATED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_FAILED,
    NOTIFICATION_PENDING_VIRUS_CHECK,
    NOTIFICATION_RETURNED_LETTER,
    NOTIFICATION_SENDING,
    NOTIFICATION_STATUS_LETTER_ACCEPTED,
    NOTIFICATION_STATUS_LETTER_RECEIVED,
    NOTIFICATION_STATUS_TYPES_COMPLETED,
    NOTIFICATION_STATUS_TYPES_FAILED,
    NOTIFICATION_STATUS_TYPES_LETTERS_NEVER_SENT,
    NOTIFICATION_TYPE,
    ORGANISATION_PERMISSION_TYPES,
    PERMISSION_LIST,
    PRECOMPILED_TEMPLATE_NAME,
    REQUEST_STATUS_VALUES,
    SMS_AUTH_TYPE,
    SMS_TYPE,
    TEMPLATE_TYPES,
    VERIFY_CODE_TYPES,
    LetterLanguageOptions,
    OrganisationUserPermissionTypes,
)
from app.hashing import check_hash, hashpw
from app.history_meta import Versioned
from app.utils import (
    DATETIME_FORMAT,
    DATETIME_FORMAT_NO_TIMEZONE,
    get_dt_string_or_none,
    get_london_midnight_in_utc,
    get_uuid_string_or_none,
    url_with_token,
    utc_string_to_bst_string,
)


def filter_null_value_fields(obj):
    return dict(filter(lambda x: x[1] is not None, obj.items()))


guest_list_recipient_types = db.Enum(*GUEST_LIST_RECIPIENT_TYPE, name="recipient_type")
notification_types = db.Enum(*NOTIFICATION_TYPE, name="notification_type")
template_types = db.Enum(*TEMPLATE_TYPES, name="template_type")


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String, nullable=False, unique=False)
    email_address = db.Column(db.String(255), nullable=False, index=True, unique=True)
    created_at = db.Column(db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, index=False, unique=False, nullable=True, onupdate=datetime.datetime.utcnow)
    _password = db.Column(db.String, index=False, unique=False, nullable=False)
    mobile_number = db.Column(db.String, index=False, unique=False, nullable=True)
    password_changed_at = db.Column(
        db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow
    )
    logged_in_at = db.Column(db.DateTime, nullable=True)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    state = db.Column(db.String, nullable=False, default="pending")
    platform_admin = db.Column(db.Boolean, nullable=False, default=False)
    current_session_id = db.Column(UUID(as_uuid=True), nullable=True)
    auth_type = db.Column(db.String, db.ForeignKey("auth_type.name"), nullable=False, default=SMS_AUTH_TYPE)
    email_access_validated_at = db.Column(
        db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow
    )
    take_part_in_research = db.Column(db.Boolean, nullable=False, default=True)
    receives_new_features_email = db.Column(db.Boolean, nullable=False, default=True)

    # either email auth or a mobile number must be provided
    __table_args__ = (CheckConstraint("auth_type in ('email_auth', 'webauthn_auth') or mobile_number is not null"),)

    services = db.relationship("Service", secondary="user_to_service", backref="users")
    organisations = db.relationship("Organisation", secondary="user_to_organisation", backref="users")

    @property
    def password(self):
        raise AttributeError("Password not readable")

    @property
    def can_use_webauthn(self):
        if self.platform_admin:
            return True

        if self.auth_type == "webauthn_auth":
            return True

        return any(str(service.id) == current_app.config["NOTIFY_SERVICE_ID"] for service in self.services)

    @password.setter
    def password(self, password):
        self._password = hashpw(password)

    def check_password(self, password):
        return check_hash(password, self._password)

    def get_permissions(self, service_id=None):
        from app.dao.permissions_dao import permission_dao

        if service_id:
            return [x.permission for x in permission_dao.get_permissions_by_user_id_and_service_id(self.id, service_id)]

        retval = {}
        for x in permission_dao.get_permissions_by_user_id(self.id):
            service_id = str(x.service_id)
            if service_id not in retval:
                retval[service_id] = []
            retval[service_id].append(x.permission)
        return retval

    def get_organisation_permissions(self) -> dict[str, list[str]]:
        from app.dao.organisation_user_permissions_dao import organisation_user_permissions_dao

        retval = {}

        # Make sure that every org the user is in, is presented in the return value.
        for org in self.organisations:
            retval[str(org.id)] = []

        for p in organisation_user_permissions_dao.get_permissions_by_user_id(self.id):
            org_id = str(p.organisation_id)
            retval[org_id].append(p.permission.value)

        return retval

    def serialize(self):
        return {
            "id": self.id,
            "name": self.name,
            "email_address": self.email_address,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "auth_type": self.auth_type,
            "current_session_id": self.current_session_id,
            "failed_login_count": self.failed_login_count,
            "email_access_validated_at": self.email_access_validated_at.strftime(DATETIME_FORMAT),
            "logged_in_at": get_dt_string_or_none(self.logged_in_at),
            "mobile_number": self.mobile_number,
            "organisations": [x.id for x in self.organisations if x.active],
            "password_changed_at": self.password_changed_at.strftime(DATETIME_FORMAT_NO_TIMEZONE),
            "permissions": self.get_permissions(),
            "organisation_permissions": self.get_organisation_permissions(),
            "platform_admin": self.platform_admin,
            "services": [x.id for x in self.services if x.active],
            "can_use_webauthn": self.can_use_webauthn,
            "state": self.state,
            "take_part_in_research": self.take_part_in_research,
            "receives_new_features_email": self.receives_new_features_email,
        }

    def serialize_for_users_list(self):
        return {
            "id": self.id,
            "name": self.name,
            "email_address": self.email_address,
            "mobile_number": self.mobile_number,
        }


class ServiceUser(db.Model):
    __tablename__ = "user_to_service"
    user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), primary_key=True)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), primary_key=True)

    __table_args__ = (UniqueConstraint("user_id", "service_id", name="uix_user_to_service"),)


user_to_organisation = db.Table(
    "user_to_organisation",
    db.Model.metadata,
    db.Column("user_id", UUID(as_uuid=True), db.ForeignKey("users.id")),
    db.Column("organisation_id", UUID(as_uuid=True), db.ForeignKey("organisation.id")),
    UniqueConstraint("user_id", "organisation_id", name="uix_user_to_organisation"),
)

user_folder_permissions = db.Table(
    "user_folder_permissions",
    db.Model.metadata,
    db.Column("user_id", UUID(as_uuid=True), primary_key=True),
    db.Column("template_folder_id", UUID(as_uuid=True), db.ForeignKey("template_folder.id"), primary_key=True),
    db.Column("service_id", UUID(as_uuid=True), primary_key=True),
    db.ForeignKeyConstraint(["user_id", "service_id"], ["user_to_service.user_id", "user_to_service.service_id"]),
    db.ForeignKeyConstraint(["template_folder_id", "service_id"], ["template_folder.id", "template_folder.service_id"]),
)


class BrandingTypes(db.Model):
    __tablename__ = "branding_type"
    name = db.Column(db.String(255), primary_key=True)


class EmailBranding(db.Model):
    __tablename__ = "email_branding"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    colour = db.Column(db.String(7), nullable=True)
    logo = db.Column(db.String(255), nullable=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    text = db.Column(db.String(255), nullable=True)
    alt_text = db.Column(db.String(255), nullable=True)
    brand_type = db.Column(
        db.String(255), db.ForeignKey("branding_type.name"), index=True, nullable=False, default=BRANDING_ORG
    )
    created_at = db.Column(db.DateTime, nullable=True, default=lambda: datetime.datetime.utcnow())
    created_by = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=lambda: datetime.datetime.utcnow())
    updated_by = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)

    active = db.Column(db.Boolean, nullable=False, default=True)

    CONSTRAINT_UNIQUE_NAME = "uq_email_branding_name"
    CONSTRAINT_CHECK_ONE_OF_ALT_TEXT_TEXT_NULL = "ck_email_branding_one_of_alt_text_or_text_is_null"
    # one of alt_text or text MUST be supplied
    __table_args__ = (
        CheckConstraint(
            "(text is not null and alt_text is null) or (text is null and alt_text is not null)",
            name="ck_email_branding_one_of_alt_text_or_text_is_null",
        ),
    )

    def serialize(self):
        serialized = {
            "id": str(self.id),
            "colour": self.colour,
            "logo": self.logo,
            "name": self.name,
            "text": self.text,
            "brand_type": self.brand_type,
            "alt_text": self.alt_text,
            "created_by": self.created_by,
            "created_at": self.created_at.strftime(DATETIME_FORMAT) if self.created_at else None,
            "updated_at": self.updated_at.strftime(DATETIME_FORMAT) if self.updated_at else None,
        }

        return serialized


service_email_branding = db.Table(
    "service_email_branding",
    db.Model.metadata,
    # service_id is a primary key as you can only have one email branding per service
    db.Column("service_id", UUID(as_uuid=True), db.ForeignKey("services.id"), primary_key=True, nullable=False),
    db.Column("email_branding_id", UUID(as_uuid=True), db.ForeignKey("email_branding.id"), nullable=False),
)


class LetterBranding(db.Model):
    __tablename__ = "letter_branding"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(255), unique=True, nullable=False)
    filename = db.Column(db.String(255), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=True, default=datetime.datetime.utcnow)
    created_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)
    updated_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)

    def serialize(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "filename": self.filename,
            "created_by": self.created_by_id,
            "created_at": self.created_at.strftime(DATETIME_FORMAT) if self.created_at else None,
            "updated_at": self.updated_at.strftime(DATETIME_FORMAT) if self.updated_at else None,
        }


service_letter_branding = db.Table(
    "service_letter_branding",
    db.Model.metadata,
    # service_id is a primary key as you can only have one letter branding per service
    db.Column("service_id", UUID(as_uuid=True), db.ForeignKey("services.id"), primary_key=True, nullable=False),
    db.Column("letter_branding_id", UUID(as_uuid=True), db.ForeignKey("letter_branding.id"), nullable=False),
)


class ServicePermissionTypes(db.Model):
    __tablename__ = "service_permission_types"

    name = db.Column(db.String(255), primary_key=True)


class Domain(db.Model):
    __tablename__ = "domain"
    domain = db.Column(db.String(255), primary_key=True)
    organisation_id = db.Column("organisation_id", UUID(as_uuid=True), db.ForeignKey("organisation.id"), nullable=False)


class OrganisationTypes(db.Model):
    __tablename__ = "organisation_types"

    name = db.Column(db.String(255), primary_key=True)
    is_crown = db.Column(db.Boolean, nullable=True)


class OrganisationPermission(db.Model):
    __tablename__ = "organisation_permissions"
    id = db.Column(UUID(as_uuid=True), primary_key=True, nullable=False, default=uuid.uuid4)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    organisation = db.relationship("Organisation", backref="permissions")
    organisation_id = db.Column(UUID(as_uuid=True), db.ForeignKey("organisation.id"), nullable=False)
    permission = db.Column(
        db.Enum(*ORGANISATION_PERMISSION_TYPES, name="organisation_permission_types"),
        index=False,
        unique=False,
        nullable=False,
    )


class OrganisationUserPermissions(db.Model):
    __tablename__ = "organisation_user_permissions"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = db.Column(db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow)
    organisation_id = db.Column(UUID(as_uuid=True), db.ForeignKey("organisation.id"), index=True)
    organisation = db.relationship("Organisation")

    user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=False)
    user = db.relationship("User")

    permission = db.Column(
        db.Enum(OrganisationUserPermissionTypes, name="organisation_user_permission_types"), index=True
    )

    __table_args__ = (
        UniqueConstraint("organisation_id", "user_id", "permission", name="uix_organisation_user_permission"),
    )


class Organisation(db.Model):
    __tablename__ = "organisation"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, unique=False)
    name = db.Column(db.String(255), nullable=False, unique=True, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)
    agreement_signed = db.Column(db.Boolean, nullable=True)
    agreement_signed_at = db.Column(db.DateTime, nullable=True)
    agreement_signed_by_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("users.id"),
        nullable=True,
    )
    agreement_signed_by = db.relationship("User")
    agreement_signed_on_behalf_of_name = db.Column(db.String(255), nullable=True)
    agreement_signed_on_behalf_of_email_address = db.Column(db.String(255), nullable=True)
    agreement_signed_version = db.Column(db.Float, nullable=True)
    crown = db.Column(db.Boolean, nullable=True)
    organisation_type = db.Column(
        db.String(255),
        db.ForeignKey("organisation_types.name"),
        unique=False,
        nullable=True,
    )
    request_to_go_live_notes = db.Column(db.Text)
    can_approve_own_go_live_requests = db.Column(db.Boolean, default=False, nullable=False)

    domains = db.relationship(
        "Domain",
    )

    # this is default email branding for organisation, not to be confused with email branding pool
    email_branding = db.relationship("EmailBranding")
    email_branding_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("email_branding.id"),
        nullable=True,
    )

    email_branding_pool = db.relationship(
        "EmailBranding", secondary="email_branding_to_organisation", backref="organisations"
    )

    # this is default letter branding for organisation
    letter_branding = db.relationship("LetterBranding")
    letter_branding_id = db.Column(
        UUID(as_uuid=True),
        db.ForeignKey("letter_branding.id"),
        nullable=True,
    )

    letter_branding_pool = db.relationship(
        "LetterBranding",
        secondary="letter_branding_to_organisation",
        backref="organisations",
    )

    notes = db.Column(db.Text, nullable=True)
    purchase_order_number = db.Column(db.String(255), nullable=True)
    billing_contact_names = db.Column(db.Text, nullable=True)
    billing_contact_email_addresses = db.Column(db.Text, nullable=True)
    billing_reference = db.Column(db.String(255), nullable=True)

    @property
    def live_services(self):
        return [service for service in self.services if service.active and not service.restricted]

    @property
    def domain_list(self):
        return [domain.domain for domain in self.domains]

    def set_permissions_list(self, permissions: list[str]):
        from app.dao.organisation_permissions_dao import set_organisation_permission

        set_organisation_permission(self, permissions)

    def serialize(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "active": self.active,
            "crown": self.crown,
            "organisation_type": self.organisation_type,
            "letter_branding_id": self.letter_branding_id,
            "email_branding_id": self.email_branding_id,
            "agreement_signed": self.agreement_signed,
            "agreement_signed_at": self.agreement_signed_at,
            "agreement_signed_by_id": self.agreement_signed_by_id,
            "agreement_signed_on_behalf_of_name": self.agreement_signed_on_behalf_of_name,
            "agreement_signed_on_behalf_of_email_address": self.agreement_signed_on_behalf_of_email_address,
            "agreement_signed_version": self.agreement_signed_version,
            "domains": self.domain_list,
            "request_to_go_live_notes": self.request_to_go_live_notes,
            "count_of_live_services": len(self.live_services),
            "notes": self.notes,
            "purchase_order_number": self.purchase_order_number,
            "billing_contact_names": self.billing_contact_names,
            "billing_contact_email_addresses": self.billing_contact_email_addresses,
            "billing_reference": self.billing_reference,
            "can_approve_own_go_live_requests": self.can_approve_own_go_live_requests,
            "permissions": [x.permission for x in self.permissions],
        }

    def serialize_for_list(self):
        return {
            "name": self.name,
            "id": str(self.id),
            "active": self.active,
            "count_of_live_services": len(self.live_services),
            "domains": self.domain_list,
            "organisation_type": self.organisation_type,
        }


class OrganisationEmailBranding(db.Model):
    __tablename__ = "email_branding_to_organisation"
    organisation_id = db.Column(UUID(as_uuid=True), db.ForeignKey("organisation.id"), primary_key=True)
    email_branding_id = db.Column(UUID(as_uuid=True), db.ForeignKey("email_branding.id"), primary_key=True)

    __table_args__ = (
        UniqueConstraint("organisation_id", "email_branding_id", name="uix_email_branding_to_organisation"),
    )


letter_branding_to_organisation = db.Table(
    "letter_branding_to_organisation",
    db.Model.metadata,
    db.Column(
        "organisation_id", UUID(as_uuid=True), db.ForeignKey("organisation.id"), primary_key=True, nullable=False
    ),
    db.Column(
        "letter_branding_id", UUID(as_uuid=True), db.ForeignKey("letter_branding.id"), primary_key=True, nullable=False
    ),
)


class Service(db.Model, Versioned):
    __tablename__ = "services"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    _name = db.Column("name", db.String(255), nullable=False, unique=True)

    # this isn't intended to be accessed, just used for checking service name uniqueness. See `email_sender_local_part`
    _normalised_service_name = db.Column("normalised_service_name", db.String, nullable=False, unique=True)
    _custom_email_sender_name = db.Column("custom_email_sender_name", db.String(255), nullable=True)
    _email_sender_local_part = db.Column("email_sender_local_part", db.String(255), nullable=False)

    created_at = db.Column(db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, index=False, unique=False, nullable=True, onupdate=datetime.datetime.utcnow)
    active = db.Column(db.Boolean, index=False, unique=False, nullable=False, default=True)
    letter_message_limit = db.Column(db.BigInteger, index=False, unique=False, nullable=False, default=999_999_999)
    sms_message_limit = db.Column(db.BigInteger, index=False, unique=False, nullable=False, default=999_999_999)
    email_message_limit = db.Column(db.BigInteger, index=False, unique=False, nullable=False, default=999_999_999)
    restricted = db.Column(db.Boolean, index=False, unique=False, nullable=False)
    created_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=False)
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    prefix_sms = db.Column(db.Boolean, nullable=False, default=True)
    organisation_type = db.Column(
        db.String(255),
        db.ForeignKey("organisation_types.name"),
        unique=False,
        nullable=True,
    )
    crown = db.Column(db.Boolean, index=False, nullable=True)
    rate_limit = db.Column(db.Integer, index=False, nullable=False, default=3000)
    contact_link = db.Column(db.String(255), nullable=True, unique=False)
    volume_sms = db.Column(db.Integer(), nullable=True, unique=False)
    volume_email = db.Column(db.Integer(), nullable=True, unique=False)
    volume_letter = db.Column(db.Integer(), nullable=True, unique=False)
    consent_to_research = db.Column(db.Boolean, nullable=True)
    count_as_live = db.Column(db.Boolean, nullable=False, default=True)
    go_live_user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    go_live_user = db.relationship("User", foreign_keys=[go_live_user_id])
    go_live_at = db.Column(db.DateTime, nullable=True)
    has_active_go_live_request = db.Column(db.Boolean, default=False, nullable=False)

    organisation_id = db.Column(UUID(as_uuid=True), db.ForeignKey("organisation.id"), index=True, nullable=True)
    organisation = db.relationship("Organisation", backref="services")

    notes = db.Column(db.Text, nullable=True)
    purchase_order_number = db.Column(db.String(255), nullable=True)
    billing_contact_names = db.Column(db.Text, nullable=True)
    billing_contact_email_addresses = db.Column(db.Text, nullable=True)
    billing_reference = db.Column(db.String(255), nullable=True)

    email_branding = db.relationship(
        "EmailBranding", secondary=service_email_branding, uselist=False, backref=db.backref("services", lazy="dynamic")
    )
    letter_branding = db.relationship(
        "LetterBranding",
        secondary=service_letter_branding,
        uselist=False,
        backref=db.backref("services", lazy="dynamic"),
    )

    @hybrid_property  # a hybrid_property enables us to still use it in queries
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value
        self._normalised_service_name = make_string_safe_for_email_local_part(value)

        # if the service hasn't set their own sender, update their sender to reflect normalised_service_name
        if not self.custom_email_sender_name:
            self._email_sender_local_part = self._normalised_service_name

    @hybrid_property  # a hybrid_property enables us to still use it in queries
    def custom_email_sender_name(self):
        return self._custom_email_sender_name

    @custom_email_sender_name.setter
    def custom_email_sender_name(self, value):
        self._custom_email_sender_name = value
        # if value is None, then we're clearing custom sender name, so set the local part based on service name
        self._email_sender_local_part = make_string_safe_for_email_local_part(value or self.name)

    @hybrid_property
    def email_sender_local_part(self):
        return self._email_sender_local_part

    @email_sender_local_part.setter
    def email_sender_local_part(self, value):
        # we can't allow this to be set manually.
        # Imagine we've updated just `custom_email_sender_name` via a serialised json blob. When that is set, it will
        # also update the value of email_sender_local_part. We don't want to then undo that good work by setting to the
        # old value (that was also passed through in the json to `service_schema.load``).
        raise NotImplementedError(
            "email_sender_local_part can only be written to via `custom_email_sender_name` or `name`"
        )

    @classmethod
    def from_json(cls, data):
        """
        Assumption: data has been validated appropriately.

        Returns a Service object based on the provided data. Deserialises created_by to created_by_id as marshmallow
        would.
        """
        # validate json with marshmallow
        fields = data.copy()

        fields["created_by_id"] = fields.pop("created_by")

        return cls(**fields)

    def get_inbound_number(self):
        if self.inbound_number and self.inbound_number.active:
            return self.inbound_number.number

    def get_default_sms_sender(self):
        default_sms_sender = [x for x in self.service_sms_senders if x.is_default]
        return default_sms_sender[0].sms_sender

    def get_default_reply_to_email_address(self):
        default_reply_to = [x for x in self.reply_to_email_addresses if x.is_default]
        return default_reply_to[0].email_address if default_reply_to else None

    def get_default_letter_contact(self):
        default_letter_contact = [x for x in self.letter_contacts if x.is_default]
        return default_letter_contact[0].contact_block if default_letter_contact else None

    def has_permission(self, permission):
        return permission in [p.permission for p in self.permissions]

    def serialize_for_org_dashboard(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "active": self.active,
            "restricted": self.restricted,
        }


class DefaultAnnualAllowance(db.Model):
    """This table represents default allowances that organisations will get for free notifications.

    Eg central government services get 40,000 free text messages for FY 2023.

    The default rates will be applied to services automatically used when a new financial year begins. They can be
    overridden on a per-service basis (eg some services may have their allowance reduced or removed).
    """

    __tablename__ = "default_annual_allowance"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    valid_from_financial_year_start = db.Column(db.Integer, index=True, nullable=False)
    organisation_type = db.Column(
        db.String(255),
        db.ForeignKey("organisation_types.name"),
        unique=False,
        nullable=True,
    )
    allowance = db.Column(db.Integer, nullable=False)
    notification_type = db.Column(notification_types, index=True, nullable=False)

    def __str__(self):
        return (
            f"AnnualAllowance({self.allowance:_d}, {self.notification_type}, "
            f"financial_year_start={self.valid_from_financial_year_start}, organisation_type{self.organisation_type})>"
        )


class AnnualBilling(db.Model):
    __tablename__ = "annual_billing"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, unique=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=False, index=True, nullable=False)
    financial_year_start = db.Column(db.Integer, nullable=False, default=True, unique=False)
    free_sms_fragment_limit = db.Column(db.Integer, nullable=False, index=False, unique=False)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    UniqueConstraint("financial_year_start", "service_id", name="ix_annual_billing_service_id")
    service = db.relationship(Service, backref=db.backref("annual_billing", uselist=True))

    __table_args__ = (
        UniqueConstraint("service_id", "financial_year_start", name="uix_service_id_financial_year_start"),
    )

    def serialize_free_sms_items(self):
        return {
            "free_sms_fragment_limit": self.free_sms_fragment_limit,
            "financial_year_start": self.financial_year_start,
        }

    def serialize(self):
        def serialize_service():
            return {"id": str(self.service_id), "name": self.service.name}

        return {
            "id": str(self.id),
            "free_sms_fragment_limit": self.free_sms_fragment_limit,
            "service_id": self.service_id,
            "financial_year_start": self.financial_year_start,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
            "service": serialize_service() if self.service else None,
        }


class InboundNumber(db.Model):
    __tablename__ = "inbound_numbers"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    number = db.Column(db.String(11), unique=True, nullable=False)
    provider = db.Column(db.String(), nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=True, index=True, nullable=True)
    service = db.relationship(Service, backref=db.backref("inbound_number", uselist=False))
    active = db.Column(db.Boolean, index=False, unique=False, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)

    def serialize(self):
        def serialize_service():
            return {"id": str(self.service_id), "name": self.service.name}

        return {
            "id": str(self.id),
            "number": self.number,
            "provider": self.provider,
            "service": serialize_service() if self.service else None,
            "active": self.active,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
        }


class ServiceSmsSender(db.Model):
    __tablename__ = "service_sms_senders"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sms_sender = db.Column(db.String(11), nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, nullable=False, unique=False)
    service = db.relationship(Service, backref=db.backref("service_sms_senders", uselist=True))
    is_default = db.Column(db.Boolean, nullable=False, default=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    inbound_number_id = db.Column(
        UUID(as_uuid=True), db.ForeignKey("inbound_numbers.id"), unique=True, index=True, nullable=True
    )
    inbound_number = db.relationship(InboundNumber, backref=db.backref("inbound_number", uselist=False))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)

    def get_reply_to_text(self):
        return try_validate_and_format_phone_number(self.sms_sender)

    def serialize(self):
        return {
            "id": str(self.id),
            "sms_sender": self.sms_sender,
            "service_id": str(self.service_id),
            "is_default": self.is_default,
            "archived": self.archived,
            "inbound_number_id": str(self.inbound_number_id) if self.inbound_number_id else None,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
        }


class ServicePermission(db.Model):
    __tablename__ = "service_permissions"

    service_id = db.Column(
        UUID(as_uuid=True), db.ForeignKey("services.id"), primary_key=True, index=True, nullable=False
    )
    permission = db.Column(
        db.String(255), db.ForeignKey("service_permission_types.name"), index=True, primary_key=True, nullable=False
    )
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)

    service_permission_types = db.relationship(Service, backref=db.backref("permissions", cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<{self.service_id} has service permission: {self.permission}>"


class ServiceGuestList(db.Model):
    __tablename__ = "service_whitelist"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, nullable=False)
    service = db.relationship("Service", backref="guest_list")
    recipient_type = db.Column(guest_list_recipient_types, nullable=False)
    recipient = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    @classmethod
    def from_string(cls, service_id, recipient_type, recipient):
        instance = cls(service_id=service_id, recipient_type=recipient_type)

        try:
            if recipient_type == MOBILE_TYPE:
                validate_phone_number(recipient, international=True)
                instance.recipient = recipient
            elif recipient_type == EMAIL_TYPE:
                validate_email_address(recipient)
                instance.recipient = recipient
            else:
                raise ValueError("Invalid recipient type")
        except InvalidRecipientError as e:
            raise ValueError(f'Invalid guest list: "{recipient}"') from e
        else:
            return instance

    def __repr__(self):
        return f"Recipient {self.recipient} of type: {self.recipient_type}"


class ServiceInboundApi(db.Model, Versioned):
    __tablename__ = "service_inbound_api"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, nullable=False, unique=True)
    service = db.relationship("Service", backref="inbound_api")
    url = db.Column(db.String(), nullable=False)
    _bearer_token = db.Column("bearer_token", db.String(), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=True)
    updated_by = db.relationship("User")
    updated_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=False)

    @property
    def bearer_token(self):
        if self._bearer_token:
            return signing.decode(self._bearer_token)
        return None

    @bearer_token.setter
    def bearer_token(self, bearer_token):
        if bearer_token:
            self._bearer_token = signing.encode(str(bearer_token))

    def serialize(self):
        return {
            "id": str(self.id),
            "service_id": str(self.service_id),
            "url": self.url,
            "updated_by_id": str(self.updated_by_id),
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
        }


class ServiceCallbackApi(db.Model, Versioned):
    __tablename__ = "service_callback_api"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, nullable=False)
    service = db.relationship("Service", backref="service_callback_api")
    url = db.Column(db.String(), nullable=False)
    callback_type = db.Column(db.String(), db.ForeignKey("service_callback_type.name"), nullable=True)
    _bearer_token = db.Column("bearer_token", db.String(), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=True)
    updated_by = db.relationship("User")
    updated_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=False)

    __table_args__ = (UniqueConstraint("service_id", "callback_type", name="uix_service_callback_type"),)

    @property
    def bearer_token(self):
        if self._bearer_token:
            return signing.decode(self._bearer_token)
        return None

    @bearer_token.setter
    def bearer_token(self, bearer_token):
        if bearer_token:
            self._bearer_token = signing.encode(str(bearer_token))

    def serialize(self):
        return {
            "id": str(self.id),
            "service_id": str(self.service_id),
            "url": self.url,
            "updated_by_id": str(self.updated_by_id),
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
        }


class ServiceCallbackType(db.Model):
    __tablename__ = "service_callback_type"

    name = db.Column(db.String, primary_key=True)


class ApiKey(db.Model, Versioned):
    __tablename__ = "api_keys"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(255), nullable=False)
    _secret = db.Column("secret", db.String(255), unique=True, nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, nullable=False)
    service = db.relationship("Service", backref="api_keys")
    key_type = db.Column(db.String(255), db.ForeignKey("key_types.name"), nullable=False)
    expiry_date = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, index=False, unique=False, nullable=True, onupdate=datetime.datetime.utcnow)
    created_by = db.relationship("User")
    created_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=False)

    __table_args__ = (
        Index("uix_service_to_key_name", "service_id", "name", unique=True, postgresql_where=expiry_date.is_(None)),
    )

    @property
    def secret(self):
        if self._secret:
            return signing.decode(self._secret)
        return None

    @secret.setter
    def secret(self, secret):
        if secret:
            self._secret = signing.encode(str(secret))


class KeyTypes(db.Model):
    __tablename__ = "key_types"

    name = db.Column(db.String(255), primary_key=True)


class TemplateProcessTypes(db.Model):
    __tablename__ = "template_process_type"
    name = db.Column(db.String(255), primary_key=True)


class TemplateFolder(db.Model):
    __tablename__ = "template_folder"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), nullable=False)
    name = db.Column(db.String, nullable=False)
    parent_id = db.Column(UUID(as_uuid=True), db.ForeignKey("template_folder.id"), nullable=True)

    service = db.relationship("Service", backref="all_template_folders")
    parent = db.relationship("TemplateFolder", remote_side=[id], backref="subfolders")
    users = db.relationship(
        "ServiceUser",
        uselist=True,
        backref=db.backref("folders", foreign_keys="user_folder_permissions.c.template_folder_id"),
        secondary="user_folder_permissions",
        primaryjoin="TemplateFolder.id == user_folder_permissions.c.template_folder_id",
    )

    __table_args__ = (UniqueConstraint("id", "service_id", name="ix_id_service_id"), {})

    def serialize(self):
        return {
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "service_id": self.service_id,
            "users_with_permission": self.get_users_with_permission(),
        }

    def is_parent_of(self, other):
        while other.parent is not None:
            if other.parent == self:
                return True
            other = other.parent
        return False

    def get_users_with_permission(self):
        service_users = self.users
        users_with_permission = [str(service_user.user_id) for service_user in service_users]

        return users_with_permission


template_folder_map = db.Table(
    "template_folder_map",
    db.Model.metadata,
    # template_id is a primary key as a template can only belong in one folder
    db.Column("template_id", UUID(as_uuid=True), db.ForeignKey("templates.id"), primary_key=True, nullable=False),
    db.Column("template_folder_id", UUID(as_uuid=True), db.ForeignKey("template_folder.id"), nullable=False),
)


def letter_languages_default(context):
    if context.get_current_parameters()["template_type"] == LETTER_TYPE:
        return LetterLanguageOptions.english
    else:
        return None


class TemplateBase(db.Model):
    __abstract__ = True

    def __init__(self, **kwargs):
        if "template_type" in kwargs:
            self.template_type = kwargs.pop("template_type")

        super().__init__(**kwargs)

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(255), nullable=False)
    template_type = db.Column(template_types, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.datetime.utcnow)
    content = db.Column(db.Text, nullable=False)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    hidden = db.Column(db.Boolean, nullable=False, default=False)
    subject = db.Column(db.Text)
    postage = db.Column(db.String, nullable=True)

    letter_welsh_content = db.Column(db.Text)
    letter_welsh_subject = db.Column(db.Text)
    letter_languages = db.Column(
        db.Enum(LetterLanguageOptions, name="letter_language_options"),
        index=False,
        unique=False,
        nullable=True,
        default=letter_languages_default,
    )

    # TODO: migrate this to be nullable=False
    has_unsubscribe_link = db.Column(db.Boolean, default=False, nullable=False)

    @declared_attr
    def service_id(cls):
        return db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, nullable=False)

    @declared_attr
    def created_by_id(cls):
        return db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=False)

    @declared_attr
    def created_by(cls):
        return db.relationship("User")

    @declared_attr
    def process_type(cls):
        return db.Column(
            db.String(255), db.ForeignKey("template_process_type.name"), index=True, nullable=False, default=NORMAL
        )

    redact_personalisation = association_proxy("template_redacted", "redact_personalisation")

    @declared_attr
    def service_letter_contact_id(cls):
        return db.Column(UUID(as_uuid=True), db.ForeignKey("service_letter_contacts.id"), nullable=True)

    @declared_attr
    def service_letter_contact(cls):
        return db.relationship("ServiceLetterContact", viewonly=True)

    @declared_attr
    def letter_attachment_id(cls):
        return db.Column(UUID(as_uuid=True), db.ForeignKey("letter_attachment.id"), nullable=True)

    @declared_attr
    def __table_args__(cls):
        if cls.__name__ not in {"Template", "TemplateHistory"}:
            raise RuntimeError("Make sure to manually add this CheckConstraint to the new migration")

        return (
            CheckConstraint(
                "template_type = 'letter' OR letter_attachment_id IS NULL",
                name=f"ck_{cls.__tablename__}_letter_attachments",
            ),
            CheckConstraint(
                "(template_type != 'letter' AND letter_languages IS NULL) OR"
                " (template_type = 'letter' AND letter_languages IS NOT NULL)"
            ),
            # if template type is not email, then has_unsubscribe_link MUST be false
            CheckConstraint(
                "template_type = 'email' OR has_unsubscribe_link IS false",
                name=f"ck_{cls.__tablename__}_non_email_has_unsubscribe_false",
            ),
        )

    @property
    def reply_to(self):
        if self.template_type == LETTER_TYPE:
            return self.service_letter_contact_id
        else:
            return None

    @reply_to.setter
    def reply_to(self, value):
        if self.template_type == LETTER_TYPE:
            self.service_letter_contact_id = value
        elif value is None:
            pass
        else:
            raise ValueError(f"Unable to set sender for {self.template_type} template")

    def get_reply_to_text(self):
        if self.template_type == LETTER_TYPE:
            return self.service_letter_contact.contact_block if self.service_letter_contact else None
        elif self.template_type == EMAIL_TYPE:
            return self.service.get_default_reply_to_email_address()
        elif self.template_type == SMS_TYPE:
            return try_validate_and_format_phone_number(self.service.get_default_sms_sender())
        else:
            return None

    @hybrid_property
    def is_precompiled_letter(self):
        return self.hidden and self.name == PRECOMPILED_TEMPLATE_NAME and self.template_type == LETTER_TYPE

    @is_precompiled_letter.setter
    def is_precompiled_letter(self, value):
        pass

    def _as_utils_template(self):
        if self.template_type == EMAIL_TYPE:
            return PlainTextEmailTemplate(self.__dict__)
        if self.template_type == SMS_TYPE:
            return SMSMessageTemplate(self.__dict__)
        if self.template_type == LETTER_TYPE:
            return LetterPrintTemplate(
                self.__dict__,
                contact_block=self.get_reply_to_text(),
            )

    def _as_utils_template_with_personalisation(self, values):
        template = self._as_utils_template()
        template.values = values
        return template

    def serialize_for_v2(self):
        serialized = {
            "id": str(self.id),
            "type": self.template_type,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
            "created_by": self.created_by.email_address,
            "version": self.version,
            "body": self.content,
            "subject": self.subject if self.template_type in {EMAIL_TYPE, LETTER_TYPE} else None,
            "name": self.name,
            "personalisation": {
                key: {
                    "required": True,
                }
                for key in self._as_utils_template().placeholders
            },
            "postage": self.postage,
            "letter_contact_block": self.service_letter_contact.contact_block if self.service_letter_contact else None,
        }

        return serialized


class Template(TemplateBase):
    __tablename__ = "templates"

    service = db.relationship("Service", backref="templates")
    version = db.Column(db.Integer, default=0, nullable=False)

    folder = db.relationship(
        "TemplateFolder",
        secondary=template_folder_map,
        uselist=False,
        # eagerly load the folder whenever the template object is fetched
        lazy="joined",
        backref=db.backref("templates"),
    )

    letter_attachment = db.relationship(
        "LetterAttachment", uselist=False, backref=db.backref("template", uselist=False)
    )

    def get_link(self):
        # TODO: use "/v2/" route once available
        return url_for(
            "template.get_template_by_id_and_service_id",
            service_id=self.service_id,
            template_id=self.id,
            _external=True,
        )

    @classmethod
    def from_json(cls, data, folder):
        """
        Assumption: data has been validated appropriately.
        Returns a Template object based on the provided data.
        """
        fields = data.copy()

        fields["created_by_id"] = fields.pop("created_by")
        fields["service_id"] = fields.pop("service")
        fields["folder"] = folder
        return cls(**fields)


class TemplateRedacted(db.Model):
    __tablename__ = "template_redacted"

    template_id = db.Column(UUID(as_uuid=True), db.ForeignKey("templates.id"), primary_key=True, nullable=False)
    redact_personalisation = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False, index=True)
    updated_by = db.relationship("User")

    # uselist=False as this is a one-to-one relationship
    template = db.relationship("Template", uselist=False, backref=db.backref("template_redacted", uselist=False))


class TemplateHistory(TemplateBase):
    __tablename__ = "templates_history"

    service = db.relationship("Service")
    version = db.Column(db.Integer, primary_key=True, nullable=False)

    # multiple template history versions can have the same attachment
    letter_attachment = db.relationship("LetterAttachment", uselist=False, backref=db.backref("template_versions"))

    @declared_attr
    def template_redacted(cls):
        return db.relationship(
            "TemplateRedacted", foreign_keys=[cls.id], primaryjoin="TemplateRedacted.template_id == TemplateHistory.id"
        )

    def get_link(self):
        return url_for("v2_template.get_template_by_id", template_id=self.id, version=self.version, _external=True)


class ProviderDetails(db.Model):
    __tablename__ = "provider_details"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name = db.Column(db.String, nullable=False)
    identifier = db.Column(db.String, nullable=False)
    priority = db.Column(db.Integer, nullable=False)
    notification_type = db.Column(notification_types, nullable=False)
    active = db.Column(db.Boolean, default=False, nullable=False)
    version = db.Column(db.Integer, default=1, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)
    created_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=True)
    created_by = db.relationship("User")
    supports_international = db.Column(db.Boolean, nullable=False, default=False)


class ProviderDetailsHistory(db.Model):
    __tablename__ = "provider_details_history"

    id = db.Column(UUID(as_uuid=True), primary_key=True, nullable=False)
    display_name = db.Column(db.String, nullable=False)
    identifier = db.Column(db.String, nullable=False)
    priority = db.Column(db.Integer, nullable=False)
    notification_type = db.Column(notification_types, nullable=False)
    active = db.Column(db.Boolean, nullable=False)
    version = db.Column(db.Integer, primary_key=True, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)
    created_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=True)
    created_by = db.relationship("User")
    supports_international = db.Column(db.Boolean, nullable=False, default=False)

    @classmethod
    def from_original(cls, original):
        history = cls()
        for c in history.__table__.columns:
            if hasattr(original, c.name):
                setattr(history, c.name, getattr(original, c.name))

        return history


class JobStatus(db.Model):
    __tablename__ = "job_status"

    name = db.Column(db.String(255), primary_key=True)


class Job(db.Model):
    __tablename__ = "jobs"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_file_name = db.Column(db.String, nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, unique=False, nullable=False)
    service = db.relationship("Service", backref=db.backref("jobs", lazy="dynamic"))
    template_id = db.Column(UUID(as_uuid=True), db.ForeignKey("templates.id"), index=True, unique=False)
    template = db.relationship("Template", backref=db.backref("jobs", lazy="dynamic"))
    template_version = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, index=False, unique=False, nullable=True, onupdate=datetime.datetime.utcnow)
    notification_count = db.Column(db.Integer, nullable=False)
    notifications_sent = db.Column(db.Integer, nullable=False, default=0)
    notifications_delivered = db.Column(db.Integer, nullable=False, default=0)
    notifications_failed = db.Column(db.Integer, nullable=False, default=0)

    processing_started = db.Column(db.DateTime, index=False, unique=False, nullable=True)
    processing_finished = db.Column(db.DateTime, index=False, unique=False, nullable=True)
    created_by = db.relationship("User")
    created_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=True)
    scheduled_for = db.Column(db.DateTime, index=True, unique=False, nullable=True)
    job_status = db.Column(
        db.String(255), db.ForeignKey("job_status.name"), index=True, nullable=False, default="pending"
    )
    archived = db.Column(db.Boolean, nullable=False, default=False)
    contact_list_id = db.Column(UUID(as_uuid=True), db.ForeignKey("service_contact_list.id"), nullable=True, index=True)


class VerifyCode(db.Model):
    __tablename__ = "verify_codes"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=False)
    user = db.relationship("User", backref=db.backref("verify_codes", lazy="dynamic"))
    _code = db.Column(db.String, nullable=False)
    code_type = db.Column(
        db.Enum(*VERIFY_CODE_TYPES, name="verify_code_types"), index=False, unique=False, nullable=False
    )
    expiry_datetime = db.Column(db.DateTime, nullable=False)
    code_used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow)

    @property
    def code(self):
        raise AttributeError("Code not readable")

    @code.setter
    def code(self, cde):
        self._code = hashpw(cde)

    def check_code(self, cde):
        return check_hash(cde, self._code)


class NotificationStatusTypes(db.Model):
    __tablename__ = "notification_status_types"

    name = db.Column(db.String(), primary_key=True)


class NotificationAllTimeView(db.Model):
    """
    WARNING: this view is a union of rows in "notifications" and
    "notification_history". Any query on this view will query both
    tables and therefore rely on *both* sets of indices.
    """

    __tablename__ = "notifications_all_time_view"

    # Tell alembic not to create this as a table. We have a migration where we manually set this up as a view.
    # This is custom logic we apply - not built-in logic. See `migrations/env.py`
    __table_args__ = {"info": {"managed_by_alembic": False}}

    id = db.Column(UUID(as_uuid=True), primary_key=True)
    job_id = db.Column(UUID(as_uuid=True))
    job_row_number = db.Column(db.Integer)
    service_id = db.Column(UUID(as_uuid=True))
    template_id = db.Column(UUID(as_uuid=True))
    template_version = db.Column(db.Integer)
    api_key_id = db.Column(UUID(as_uuid=True))
    key_type = db.Column(db.String)
    billable_units = db.Column(db.Integer)
    notification_type = db.Column(notification_types)
    created_at = db.Column(db.DateTime)
    sent_at = db.Column(db.DateTime)
    sent_by = db.Column(db.String)
    updated_at = db.Column(db.DateTime)
    status = db.Column("notification_status", db.Text)
    reference = db.Column(db.String)
    client_reference = db.Column(db.String)
    international = db.Column(db.Boolean)
    phone_prefix = db.Column(db.String)
    rate_multiplier = db.Column(db.Numeric(asdecimal=False))
    created_by_id = db.Column(UUID(as_uuid=True))
    postage = db.Column(db.String)
    document_download_count = db.Column(db.Integer)


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    to = db.Column(db.String, nullable=False)
    normalised_to = db.Column(db.String, nullable=True)
    job_id = db.Column(UUID(as_uuid=True), db.ForeignKey("jobs.id"), index=True, unique=False)
    job = db.relationship("Job", backref=db.backref("notifications", lazy="dynamic"))
    job_row_number = db.Column(db.Integer, nullable=True)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=False)
    service = db.relationship("Service")
    template_id = db.Column(UUID(as_uuid=True), index=True, unique=False)
    template_version = db.Column(db.Integer, nullable=False)
    template = db.relationship("TemplateHistory")
    api_key_id = db.Column(UUID(as_uuid=True), db.ForeignKey("api_keys.id"), unique=False)
    api_key = db.relationship("ApiKey")
    key_type = db.Column(db.String, db.ForeignKey("key_types.name"), unique=False, nullable=False)
    billable_units = db.Column(db.Integer, nullable=False, default=0)
    notification_type = db.Column(notification_types, nullable=False)
    created_at = db.Column(db.DateTime, index=True, unique=False, nullable=False)
    sent_at = db.Column(db.DateTime, index=False, unique=False, nullable=True)
    sent_by = db.Column(db.String, nullable=True)
    updated_at = db.Column(db.DateTime, index=False, unique=False, nullable=True, onupdate=datetime.datetime.utcnow)
    status = db.Column(
        "notification_status",
        db.Text,
        db.ForeignKey("notification_status_types.name"),
        nullable=True,
        default="created",
        key="status",  # http://docs.sqlalchemy.org/en/latest/core/metadata.html#sqlalchemy.schema.Column
    )
    reference = db.Column(db.String, nullable=True, index=True)
    client_reference = db.Column(db.String, index=True, nullable=True)
    _personalisation = db.Column(db.String, nullable=True)

    international = db.Column(db.Boolean, nullable=False, default=False)
    phone_prefix = db.Column(db.String, nullable=True)
    rate_multiplier = db.Column(db.Numeric(asdecimal=False), nullable=True)

    created_by = db.relationship("User")
    created_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)

    reply_to_text = db.Column(db.String, nullable=True)

    document_download_count = db.Column(db.Integer, nullable=True)

    postage = db.Column(db.String, nullable=True)

    unsubscribe_link = db.Column(db.String, nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["templates_history.id", "templates_history.version"],
        ),
        UniqueConstraint("job_id", "job_row_number", name="uq_notifications_job_row_number"),
        Index("ix_notifications_notification_type_composite", "notification_type", "status", "created_at"),
        Index("ix_notifications_service_created_at", "service_id", "created_at"),
        Index("ix_notifications_service_id_composite", "service_id", "notification_type", "status", "created_at"),
        # unsubscribe_link value should be null for non-email notifications
        CheckConstraint(
            "notification_type = 'email' OR unsubscribe_link is null",
            name="ck_unsubscribe_link_is_null_if_notification_not_an_email",
        ),
    )

    @property
    def personalisation(self):
        if self._personalisation:
            return signing.decode(self._personalisation)
        return {}

    @personalisation.setter
    def personalisation(self, personalisation):
        self._personalisation = signing.encode(personalisation or {})

    def completed_at(self):
        if self.status in NOTIFICATION_STATUS_TYPES_COMPLETED:
            return self.updated_at.strftime(DATETIME_FORMAT)

        return None

    @staticmethod
    def substitute_status(status_or_statuses: str | list[str]) -> list[str]:
        """
        static function that takes a status or list of statuses and substitutes our new failure types if it finds
        the deprecated one
        """
        if isinstance(status_or_statuses, str):
            status_or_statuses = [status_or_statuses]

        def _substitute_status(_status: str) -> list[str]:
            if _status == NOTIFICATION_FAILED:
                return NOTIFICATION_STATUS_TYPES_FAILED
            elif _status == NOTIFICATION_STATUS_LETTER_ACCEPTED:
                return [NOTIFICATION_CREATED, NOTIFICATION_SENDING]
            elif _status == NOTIFICATION_STATUS_LETTER_RECEIVED:
                return [NOTIFICATION_DELIVERED]

            return [_status]

        unique_substituted_statuses = {
            substitute for status in status_or_statuses for substitute in _substitute_status(status)
        }

        return list(unique_substituted_statuses)

    @property
    def content(self):
        return self.template._as_utils_template_with_personalisation(
            self.personalisation
        ).content_with_placeholders_filled_in

    @property
    def subject(self):
        template_object = self.template._as_utils_template_with_personalisation(self.personalisation)
        return getattr(template_object, "subject", None)

    @property
    def formatted_status(self):
        return {
            "email": {
                "failed": "Failed",
                "technical-failure": "Technical failure",
                "temporary-failure": "Inbox not accepting messages right now",
                "permanent-failure": "Email address doesn’t exist",
                "delivered": "Delivered",
                "sending": "Sending",
                "created": "Sending",
                "sent": "Delivered",
            },
            "sms": {
                "failed": "Failed",
                "technical-failure": "Technical failure",
                "temporary-failure": "Phone not accepting messages right now",
                "permanent-failure": "Phone number doesn’t exist",
                "delivered": "Delivered",
                "sending": "Sending",
                "created": "Sending",
                "sent": "Sent internationally",
            },
            "letter": {
                "technical-failure": "Technical failure",
                "permanent-failure": "Permanent failure",
                "sending": "Accepted",
                "created": "Accepted",
                "delivered": "Received",
                "returned-letter": "Returned",
            },
        }[self.template.template_type].get(self.status, self.status)

    def get_letter_status(self):
        """
        Return the notification_status, as we should present for letters. The distinction between created and sending is
        a bit more confusing for letters, not to mention that there's no concept of temporary or permanent failure yet.


        """
        # this should only ever be called for letter notifications - it makes no sense otherwise and I'd rather not
        # get the two code flows mixed up at all
        assert self.notification_type == LETTER_TYPE

        if self.status in [NOTIFICATION_CREATED, NOTIFICATION_SENDING]:
            return NOTIFICATION_STATUS_LETTER_ACCEPTED
        elif self.status in [NOTIFICATION_DELIVERED, NOTIFICATION_RETURNED_LETTER]:
            return NOTIFICATION_STATUS_LETTER_RECEIVED
        else:
            # Currently can only be technical-failure OR pending-virus-check OR validation-failed
            return self.status

    def get_created_by_name(self):
        if self.created_by:
            return self.created_by.name
        else:
            return None

    def get_created_by_email_address(self):
        if self.created_by:
            return self.created_by.email_address
        else:
            return None

    def serialize_for_csv(self):
        serialized = {
            "id": self.id,
            "row_number": "" if self.job_row_number is None else self.job_row_number + 1,
            "recipient": self.to,
            "client_reference": self.client_reference or "",
            "template_name": self.template.name,
            "template_type": self.template.template_type,
            "job_name": self.job.original_file_name if self.job else "",
            "status": self.formatted_status,
            "created_at": utc_string_to_bst_string(self.created_at),
            "created_by_name": self.get_created_by_name(),
            "created_by_email_address": self.get_created_by_email_address(),
            "api_key_name": self.api_key.name if self.api_key else None,
        }

        return serialized

    def serialize(self):
        template_dict = {"version": self.template.version, "id": self.template.id, "uri": self.template.get_link()}

        serialized = {
            "id": self.id,
            "reference": self.client_reference,
            "email_address": self.to if self.notification_type == EMAIL_TYPE else None,
            "phone_number": self.to if self.notification_type == SMS_TYPE else None,
            "line_1": None,
            "line_2": None,
            "line_3": None,
            "line_4": None,
            "line_5": None,
            "line_6": None,
            "postcode": None,
            "type": self.notification_type,
            "status": self.get_letter_status() if self.notification_type == LETTER_TYPE else self.status,
            "template": template_dict,
            "body": self.content,
            "subject": self.subject,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "created_by_name": self.get_created_by_name(),
            "sent_at": get_dt_string_or_none(self.sent_at),
            "completed_at": self.completed_at(),
            "scheduled_for": None,
            "postage": self.postage,
            "one_click_unsubscribe_url": self.get_unsubscribe_link_for_headers(
                template_has_unsubscribe_link=self.template.has_unsubscribe_link
            ),
        }

        if self.notification_type == LETTER_TYPE:
            personalisation = InsensitiveDict(self.personalisation)

            (
                serialized["line_1"],
                serialized["line_2"],
                serialized["line_3"],
                serialized["line_4"],
                serialized["line_5"],
                serialized["line_6"],
                serialized["postcode"],
            ) = (personalisation.get(line) for line in address_lines_1_to_6_and_postcode_keys)

            serialized["estimated_delivery"] = get_letter_timings(
                serialized["created_at"], postage=self.postage
            ).earliest_delivery.strftime(DATETIME_FORMAT)

        return serialized

    def serialize_with_cost_data(self):
        serialized = self.serialize()
        serialized["cost_details"] = {}
        serialized["cost_in_pounds"] = 0.00
        serialized["is_cost_data_ready"] = True

        if self.notification_type == "sms":
            return self._add_cost_data_for_sms(serialized)
        elif self.notification_type == "letter":
            return self._add_cost_data_for_letter(serialized)

        return serialized

    def _add_cost_data_for_sms(self, serialized):
        if not self._is_cost_data_ready_for_sms():
            serialized["is_cost_data_ready"] = False
            serialized["cost_details"] = {}
            serialized["cost_in_pounds"] = None
        else:
            serialized["cost_details"]["billable_sms_fragments"] = self.billable_units
            serialized["cost_details"]["international_rate_multiplier"] = self.rate_multiplier
            sms_rate = self._get_sms_rate()
            serialized["cost_details"]["sms_rate"] = sms_rate
            serialized["cost_in_pounds"] = self.billable_units * self.rate_multiplier * sms_rate

        return serialized

    def _add_cost_data_for_letter(self, serialized):
        if not self._is_cost_data_ready_for_letter():
            serialized["is_cost_data_ready"] = False
            serialized["cost_details"] = {}
            serialized["cost_in_pounds"] = None
        # we don't bill users for letters that were not sent
        elif self._letter_was_never_sent():
            serialized["cost_details"]["billable_sheets_of_paper"] = 0
            serialized["cost_details"]["postage"] = self.postage
            serialized["cost_in_pounds"] = 0.00
        else:
            serialized["cost_details"]["billable_sheets_of_paper"] = self.billable_units
            serialized["cost_details"]["postage"] = self.postage
            serialized["cost_in_pounds"] = self._get_letter_cost()

        return serialized

    def _is_cost_data_ready_for_sms(self):
        if self.status == NOTIFICATION_CREATED and not self.billable_units:
            return False
        return True

    def _is_cost_data_ready_for_letter(self):
        if self.status == NOTIFICATION_PENDING_VIRUS_CHECK or (
            self.status == NOTIFICATION_CREATED and not self.billable_units
        ):
            return False
        return True

    def _letter_was_never_sent(self):
        if self.status in NOTIFICATION_STATUS_TYPES_LETTERS_NEVER_SENT:
            return True
        return False

    def _get_sms_rate(self):

        created_at_date = self.created_at.date()

        if rate := redis_store.get(f"sms-rate-for-{created_at_date}"):
            return float(rate)

        from app.dao.sms_rate_dao import dao_get_sms_rate_for_timestamp

        rate = dao_get_sms_rate_for_timestamp(created_at_date).rate

        redis_store.set(f"sms-rate-for-{created_at_date}", rate, ex=86400)

        return rate

    def _get_letter_cost(self):
        if self.billable_units == 0:
            return 0.00

        created_at_date = self.created_at.date()

        if rate := redis_store.get(
            f"letter-rate-for-date-{created_at_date}-sheets-{self.billable_units}-postage-{self.postage}"
        ):
            return float(rate)

        from app.dao.letter_rate_dao import dao_get_letter_rates_for_timestamp

        rates = dao_get_letter_rates_for_timestamp(created_at_date)
        letter_rate = float(
            next(
                (rate for rate in rates if rate.sheet_count == self.billable_units and rate.post_class == self.postage),
                None,
            ).rate
        )
        redis_store.set(
            f"letter-rate-for-date-{created_at_date}-sheets-{self.billable_units}-postage-{self.postage}",
            letter_rate,
            ex=86400,
        )

        return letter_rate

    def _generate_unsubscribe_link(self, base_url):
        return url_with_token(
            self.to,
            url=f"/unsubscribe/{str(self.id)}/",
            base_url=base_url,
        )

    def get_unsubscribe_link_for_headers(self, *, template_has_unsubscribe_link):
        """
        Generates a URL on the API domain, which accepts a POST request from an email client
        """
        if self.unsubscribe_link:
            return self.unsubscribe_link
        if template_has_unsubscribe_link:
            return self._generate_unsubscribe_link(current_app.config["API_HOST_NAME"])

    def get_unsubscribe_link_for_body(self, *, template_has_unsubscribe_link):
        """
        Generates a URL on the admin domain, which serves a page telling the user they
        have been unsubscribed
        """
        if template_has_unsubscribe_link:
            return self._generate_unsubscribe_link(current_app.config["ADMIN_BASE_URL"])


class NotificationHistory(db.Model):
    __tablename__ = "notification_history"

    id = db.Column(UUID(as_uuid=True), primary_key=True)
    job_id = db.Column(UUID(as_uuid=True), db.ForeignKey("jobs.id"), index=True, unique=False)
    job = db.relationship("Job")
    job_row_number = db.Column(db.Integer, nullable=True)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=False)
    service = db.relationship("Service")
    template_id = db.Column(UUID(as_uuid=True), unique=False)
    template_version = db.Column(db.Integer, nullable=False)
    api_key_id = db.Column(UUID(as_uuid=True), db.ForeignKey("api_keys.id"), unique=False)
    api_key = db.relationship("ApiKey")
    key_type = db.Column(db.String, db.ForeignKey("key_types.name"), unique=False, nullable=False)
    billable_units = db.Column(db.Integer, nullable=False, default=0)
    notification_type = db.Column(notification_types, nullable=False)
    created_at = db.Column(db.DateTime, unique=False, nullable=False)
    sent_at = db.Column(db.DateTime, index=False, unique=False, nullable=True)
    sent_by = db.Column(db.String, nullable=True)
    updated_at = db.Column(db.DateTime, index=False, unique=False, nullable=True, onupdate=datetime.datetime.utcnow)
    status = db.Column(
        "notification_status",
        db.Text,
        db.ForeignKey("notification_status_types.name"),
        nullable=True,
        default="created",
        key="status",  # http://docs.sqlalchemy.org/en/latest/core/metadata.html#sqlalchemy.schema.Column
    )
    reference = db.Column(db.String, nullable=True, index=True)
    client_reference = db.Column(db.String, nullable=True)

    international = db.Column(db.Boolean, nullable=True, default=False)
    phone_prefix = db.Column(db.String, nullable=True)
    rate_multiplier = db.Column(db.Numeric(asdecimal=False), nullable=True)

    created_by_id = db.Column(UUID(as_uuid=True), nullable=True)

    postage = db.Column(db.String, nullable=True)

    document_download_count = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["templates_history.id", "templates_history.version"],
        ),
        Index(
            "ix_notification_history_service_id_composite", "service_id", "key_type", "notification_type", "created_at"
        ),
        Index("ix_notification_history_created_at", "created_at", postgresql_concurrently=True),
    )


class LetterCostThreshold(enum.Enum):
    sorted = "sorted"
    unsorted = "unsorted"


class NotificationLetterDespatch(db.Model):
    __tablename__ = "notifications_letter_despatch"

    notification_id = db.Column(UUID(as_uuid=True), primary_key=True)
    despatched_on = db.Column(db.Date, index=True)
    cost_threshold = db.Column(
        db.Enum(LetterCostThreshold, name="letter_despatch_cost_threshold"), nullable=False, index=True
    )

    # WIP:
    # Ignoring a strict foreign key relationship here for now. Notifications are archived to the NotificationHistory
    # table by a nightly job and I haven't investigated whether that might break a strict FK yet or if it would
    # work smoothly. We can still have a relationship using an explicit join condition.
    notification = db.relationship(
        "NotificationAllTimeView",
        primaryjoin="NotificationLetterDespatch.notification_id == foreign(NotificationAllTimeView.id)",
        uselist=False,
        viewonly=True,
    )


class InviteStatusType(db.Model):
    __tablename__ = "invite_status_type"

    name = db.Column(db.String, primary_key=True)


class InvitedUser(db.Model):
    __tablename__ = "invited_users"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_address = db.Column(db.String(255), nullable=False)
    user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=False)
    from_user = db.relationship("User")
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, unique=False)
    service = db.relationship("Service")
    created_at = db.Column(db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow)
    status = db.Column(
        db.Enum(*INVITED_USER_STATUS_TYPES, name="invited_users_status_types"), nullable=False, default=INVITE_PENDING
    )
    permissions = db.Column(db.String, nullable=False)
    auth_type = db.Column(db.String, db.ForeignKey("auth_type.name"), index=True, nullable=False, default=SMS_AUTH_TYPE)
    folder_permissions = db.Column(JSONB(none_as_null=True), nullable=False, default=[])

    # would like to have used properties for this but haven't found a way to make them
    # play nice with marshmallow yet
    def get_permissions(self):
        return self.permissions.split(",")


class InvitedOrganisationUser(db.Model):
    __tablename__ = "invited_organisation_users"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_address = db.Column(db.String(255), nullable=False)
    invited_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False)
    invited_by = db.relationship("User")
    organisation_id = db.Column(UUID(as_uuid=True), db.ForeignKey("organisation.id"), nullable=False)
    organisation = db.relationship("Organisation")

    permissions = db.Column(db.String, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)

    status = db.Column(db.String, db.ForeignKey("invite_status_type.name"), nullable=False, default=INVITE_PENDING)

    def serialize(self):
        return {
            "id": str(self.id),
            "email_address": self.email_address,
            "invited_by": str(self.invited_by_id),
            "organisation": str(self.organisation_id),
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "permissions": [p for p in self.permissions.split(",") if p],
            "status": self.status,
        }


class Permission(db.Model):
    __tablename__ = "permissions"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Service id is optional, if the service is omitted we will assume the permission is not service specific.
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, unique=False, nullable=True)
    service = db.relationship("Service")
    user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=False)
    user = db.relationship("User")
    permission = db.Column(
        db.Enum(*PERMISSION_LIST, name="permission_types"), index=False, unique=False, nullable=False
    )
    created_at = db.Column(db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow)

    __table_args__ = (UniqueConstraint("service_id", "user_id", "permission", name="uix_service_user_permission"),)


class Event(db.Model):
    __tablename__ = "events"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, index=False, unique=False, nullable=False, default=datetime.datetime.utcnow)
    data = db.Column(JSON, nullable=False)


class Rate(db.Model):
    __tablename__ = "rates"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    valid_from = db.Column(db.DateTime, nullable=False)
    rate = db.Column(db.Numeric(asdecimal=False), nullable=False)
    notification_type = db.Column(notification_types, index=True, nullable=False)

    def __str__(self):
        the_string = f"{self.rate}"
        the_string += f" {self.notification_type}"
        the_string += f" {self.valid_from}"
        return the_string

    def serialize(self):
        return {
            "rate": self.rate,
            "valid_from": self.valid_from.isoformat(),
        }


class InboundSms(db.Model):
    __tablename__ = "inbound_sms"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, nullable=False)
    service = db.relationship("Service", backref="inbound_sms")

    notify_number = db.Column(db.String, nullable=False)  # the service's number, that the msg was sent to
    user_number = db.Column(db.String, nullable=False, index=True)  # the end user's number, that the msg was sent from
    provider_date = db.Column(db.DateTime)
    provider_reference = db.Column(db.String)
    provider = db.Column(db.String, nullable=False)
    _content = db.Column("content", db.String, nullable=False)

    @property
    def content(self):
        return signing.decode(self._content)

    @content.setter
    def content(self, content):
        self._content = signing.encode(content)

    def serialize(self):
        return {
            "id": str(self.id),
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "service_id": str(self.service_id),
            "notify_number": self.notify_number,
            "user_number": self.user_number,
            "content": self.content,
        }


class InboundSmsHistory(db.Model):
    __tablename__ = "inbound_sms_history"
    id = db.Column(UUID(as_uuid=True), primary_key=True)
    created_at = db.Column(db.DateTime, unique=False, nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), index=True, unique=False)
    service = db.relationship("Service")
    notify_number = db.Column(db.String, nullable=False)
    provider_date = db.Column(db.DateTime)
    provider_reference = db.Column(db.String)
    provider = db.Column(db.String, nullable=False)


class LetterRate(db.Model):
    __tablename__ = "letter_rates"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    start_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime, nullable=True)
    sheet_count = db.Column(db.Integer, nullable=False)  # double sided sheet
    rate = db.Column(db.Numeric(), nullable=False)
    crown = db.Column(db.Boolean, nullable=False)
    post_class = db.Column(db.String, nullable=False)

    def serialize(self):
        return {
            "sheet_count": self.sheet_count,
            "start_date": self.start_date.isoformat(),
            "rate": self.rate,
            "post_class": self.post_class,
        }


class ServiceEmailReplyTo(db.Model):
    __tablename__ = "service_email_reply_to"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=False, index=True, nullable=False)
    service = db.relationship(Service, backref=db.backref("reply_to_email_addresses"))

    email_address = db.Column(db.Text, nullable=False, index=False, unique=False)
    is_default = db.Column(db.Boolean, nullable=False, default=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)

    def serialize(self):
        return {
            "id": str(self.id),
            "service_id": str(self.service_id),
            "email_address": self.email_address,
            "is_default": self.is_default,
            "archived": self.archived,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
        }


class ServiceLetterContact(db.Model):
    __tablename__ = "service_letter_contacts"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=False, index=True, nullable=False)
    service = db.relationship(Service, backref=db.backref("letter_contacts"))

    contact_block = db.Column(db.Text, nullable=False, index=False, unique=False)
    is_default = db.Column(db.Boolean, nullable=False, default=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)

    def serialize(self):
        return {
            "id": str(self.id),
            "service_id": str(self.service_id),
            "contact_block": self.contact_block,
            "is_default": self.is_default,
            "archived": self.archived,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
        }


class AuthType(db.Model):
    __tablename__ = "auth_type"

    name = db.Column(db.String, primary_key=True)


class DailySortedLetter(db.Model):
    __tablename__ = "daily_sorted_letter"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    billing_day = db.Column(db.Date, nullable=False, index=True)
    file_name = db.Column(db.String, nullable=True)
    unsorted_count = db.Column(db.Integer, nullable=False, default=0)
    sorted_count = db.Column(db.Integer, nullable=False, default=0)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)

    __table_args__ = (UniqueConstraint("file_name", "billing_day", name="uix_file_name_billing_day"),)


class FactBillingLetterDespatch(db.Model):
    __tablename__ = "ft_billing_letter_despatch"

    bst_date = db.Column(db.Date, nullable=False, primary_key=True)
    postage = db.Column(db.String, nullable=False, primary_key=True)
    cost_threshold = db.Column(
        # Reuse enum from NotificationLetterDespatch as the values are intrinsically linked
        db.Enum(LetterCostThreshold, name="letter_despatch_cost_threshold"),
        nullable=False,
        index=True,
        primary_key=True,
    )
    rate = db.Column(db.Numeric(), nullable=False, primary_key=True)
    billable_units = db.Column(db.Integer(), nullable=True, primary_key=True)
    notifications_sent = db.Column(db.Integer(), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)


class FactBilling(db.Model):
    __tablename__ = "ft_billing"

    bst_date = db.Column(db.Date, nullable=False, primary_key=True, index=True)
    template_id = db.Column(UUID(as_uuid=True), nullable=False, primary_key=True, index=True)
    service_id = db.Column(UUID(as_uuid=True), nullable=False, primary_key=True, index=True)
    notification_type = db.Column(db.Text, nullable=False, primary_key=True)
    provider = db.Column(db.Text, nullable=False, primary_key=True)
    rate_multiplier = db.Column(db.Integer(), nullable=False, primary_key=True)
    international = db.Column(db.Boolean, nullable=False, primary_key=True)
    rate = db.Column(db.Numeric(), nullable=False, primary_key=True)
    postage = db.Column(db.String, nullable=False, primary_key=True)
    billable_units = db.Column(db.Integer(), nullable=True)
    notifications_sent = db.Column(db.Integer(), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)


class FactNotificationStatus(db.Model):
    __tablename__ = "ft_notification_status"

    bst_date = db.Column(db.Date, index=True, primary_key=True, nullable=False)
    template_id = db.Column(UUID(as_uuid=True), primary_key=True, index=True, nullable=False)
    service_id = db.Column(
        UUID(as_uuid=True),
        primary_key=True,
        index=True,
        nullable=False,
    )
    job_id = db.Column(UUID(as_uuid=True), primary_key=True, index=True, nullable=False)
    notification_type = db.Column(db.Text, primary_key=True, nullable=False)
    key_type = db.Column(db.Text, primary_key=True, nullable=False)
    notification_status = db.Column(db.Text, primary_key=True, nullable=False)
    notification_count = db.Column(db.Integer(), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)


class FactProcessingTime(db.Model):
    __tablename__ = "ft_processing_time"

    bst_date = db.Column(db.Date, index=True, primary_key=True, nullable=False)
    messages_total = db.Column(db.Integer(), nullable=False)
    messages_within_10_secs = db.Column(db.Integer(), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)


class Complaint(db.Model):
    __tablename__ = "complaints"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    notification_id = db.Column(UUID(as_uuid=True), index=True, nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=False, index=True, nullable=False)
    service = db.relationship(Service, backref=db.backref("complaints"))
    ses_feedback_id = db.Column(db.Text, nullable=True)
    complaint_type = db.Column(db.Text, nullable=True)
    complaint_date = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)

    def serialize(self):
        return {
            "id": str(self.id),
            "notification_id": str(self.notification_id),
            "service_id": str(self.service_id),
            "service_name": self.service.name,
            "ses_feedback_id": str(self.ses_feedback_id),
            "complaint_type": self.complaint_type,
            "complaint_date": get_dt_string_or_none(self.complaint_date),
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
        }


class ServiceDataRetention(db.Model):
    __tablename__ = "service_data_retention"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=False, index=True, nullable=False)
    service = db.relationship(
        Service, backref=db.backref("data_retention", collection_class=attribute_mapped_collection("notification_type"))
    )
    notification_type = db.Column(notification_types, nullable=False)
    days_of_retention = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)

    __table_args__ = (UniqueConstraint("service_id", "notification_type", name="uix_service_data_retention"),)

    def serialize(self):
        return {
            "id": str(self.id),
            "service_id": str(self.service_id),
            "service_name": self.service.name,
            "notification_type": self.notification_type,
            "days_of_retention": self.days_of_retention,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
        }


class ReturnedLetter(db.Model):
    __tablename__ = "returned_letters"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reported_at = db.Column(db.Date, nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=False, index=True, nullable=False)
    service = db.relationship(Service, backref=db.backref("returned_letters"))
    notification_id = db.Column(UUID(as_uuid=True), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)


class ServiceContactList(db.Model):
    __tablename__ = "service_contact_list"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_file_name = db.Column(db.String, nullable=False)
    row_count = db.Column(db.Integer, nullable=False)
    template_type = db.Column(template_types, nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), unique=False, index=True, nullable=False)
    service = db.relationship(Service, backref=db.backref("contact_list"))
    created_by = db.relationship("User")
    created_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), index=True, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)
    archived = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def job_count(self):
        today = datetime.datetime.utcnow().date()
        return (
            Job.query.filter(
                Job.contact_list_id == self.id,
                func.coalesce(Job.processing_started, Job.created_at)
                >= today - func.coalesce(ServiceDataRetention.days_of_retention, 7),
            )
            .outerjoin(
                ServiceDataRetention,
                and_(
                    self.service_id == ServiceDataRetention.service_id,
                    func.cast(self.template_type, String) == func.cast(ServiceDataRetention.notification_type, String),
                ),
            )
            .count()
        )

    @property
    def has_jobs(self):
        return bool(
            Job.query.filter(
                Job.contact_list_id == self.id,
            ).first()
        )

    def serialize(self):
        contact_list = {
            "id": str(self.id),
            "original_file_name": self.original_file_name,
            "row_count": self.row_count,
            "recent_job_count": self.job_count,
            "has_jobs": self.has_jobs,
            "template_type": self.template_type,
            "service_id": str(self.service_id),
            "created_by": self.created_by.name,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
        }
        return contact_list


class WebauthnCredential(db.Model):
    """
    A table that stores data for registered webauthn credentials.
    """

    __tablename__ = "webauthn_credential"

    id = db.Column(UUID(as_uuid=True), primary_key=True, nullable=False, default=uuid.uuid4)

    user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False)
    user = db.relationship(User, backref=db.backref("webauthn_credentials"))

    name = db.Column(db.String, nullable=False)

    # base64 encoded CBOR. used for logging in. https://w3c.github.io/webauthn/#sctn-attested-credential-data
    credential_data = db.Column(db.String, nullable=False)

    # base64 encoded CBOR. used for auditing. https://www.w3.org/TR/webauthn-2/#authenticatorattestationresponse
    registration_response = db.Column(db.String, nullable=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True, onupdate=datetime.datetime.utcnow)

    logged_in_at = db.Column(db.DateTime, nullable=True)

    def serialize(self):
        return {
            "id": str(self.id),
            "user_id": str(self.user_id),
            "name": self.name,
            "credential_data": self.credential_data,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "updated_at": get_dt_string_or_none(self.updated_at),
            "logged_in_at": get_dt_string_or_none(self.logged_in_at),
        }


class LetterAttachment(db.Model):
    __tablename__ = "letter_attachment"

    id = db.Column(UUID(as_uuid=True), primary_key=True, nullable=False, default=uuid.uuid4)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    created_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)
    archived_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)

    original_filename = db.Column(db.String, nullable=False)
    page_count = db.Column(db.SmallInteger, nullable=False)

    def serialize(self):
        return {
            "id": str(self.id),
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "created_by_id": str(self.created_by_id),
            "archived_at": get_dt_string_or_none(self.archived_at),
            "archived_by_id": get_uuid_string_or_none(self.archived_by_id),
            "original_filename": self.original_filename,
            "page_count": self.page_count,
        }


class UnsubscribeRequestReport(db.Model):
    __tablename__ = "unsubscribe_request_report"
    id = db.Column(UUID(as_uuid=True), primary_key=True)

    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), nullable=False)
    service = db.relationship(Service, backref=db.backref("unsubscribe_request_reports"))

    created_at = db.Column(db.DateTime, nullable=True, default=datetime.datetime.utcnow)
    earliest_timestamp = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    latest_timestamp = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)
    processed_by_service_at = db.Column(db.DateTime, nullable=True)
    count = db.Column(db.BigInteger, nullable=False)

    @property
    def will_be_archived_at(self):
        return get_london_midnight_in_utc(self.created_at + datetime.timedelta(days=7))

    def serialize(self):
        return {
            "batch_id": str(self.id),
            "count": self.count,
            "created_at": self.created_at.strftime(DATETIME_FORMAT),
            "earliest_timestamp": self.earliest_timestamp.strftime(DATETIME_FORMAT),
            "latest_timestamp": self.latest_timestamp.strftime(DATETIME_FORMAT),
            "processed_by_service_at": (
                self.processed_by_service_at.strftime(DATETIME_FORMAT) if self.processed_by_service_at else None
            ),
            "is_a_batched_report": True,
            "will_be_archived_at": self.will_be_archived_at.strftime(DATETIME_FORMAT),
        }

    @staticmethod
    def serialize_unbatched_requests(unbatched_unsubscribe_requests):
        return {
            "batch_id": None,
            "count": len(unbatched_unsubscribe_requests),
            "created_at": None,
            "earliest_timestamp": unbatched_unsubscribe_requests[-1].created_at.strftime(DATETIME_FORMAT),
            "latest_timestamp": unbatched_unsubscribe_requests[0].created_at.strftime(DATETIME_FORMAT),
            "processed_by_service_at": None,
            "is_a_batched_report": False,
            "will_be_archived_at": get_london_midnight_in_utc(
                unbatched_unsubscribe_requests[-1].created_at + datetime.timedelta(days=90)
            ).strftime(DATETIME_FORMAT),
        }


class UnsubscribeRequest(db.Model):
    __tablename__ = "unsubscribe_request"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # WIP:
    # Ignoring a strict foreign key relationship here for now. Notifications are archived to the NotificationHistory
    # table by a nightly job and I haven't investigated whether that might break a strict FK yet or if it would
    # work smoothly. We can still have a relationship using an explicit join condition.
    notification_id = db.Column(UUID(as_uuid=True), index=True, nullable=False)
    notification = db.relationship(
        "NotificationAllTimeView",
        primaryjoin="UnsubscribeRequest.notification_id == foreign(NotificationAllTimeView.id)",
        uselist=False,
        viewonly=True,
    )

    # this is denormalised but might still be useful to have as a separate column?
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), nullable=False)
    service = db.relationship(Service, backref=db.backref("unsubscribe_requests"))

    template_id = db.Column(UUID(as_uuid=True), nullable=False)
    template_version = db.Column(db.Integer, nullable=False)

    template_history = db.relationship(TemplateHistory, backref=db.backref("unsubscribe_requests"))
    template = db.relationship(
        Template,
        foreign_keys=[template_id],
        primaryjoin="Template.id == UnsubscribeRequest.template_id",
        backref=db.backref("unsubscribe_requests"),
        viewonly=True,
    )

    email_address = db.Column(db.Text, nullable=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow)

    unsubscribe_request_report_id = db.Column(
        UUID(as_uuid=True), db.ForeignKey("unsubscribe_request_report.id"), index=True, nullable=True
    )
    unsubscribe_request_report = db.relationship(UnsubscribeRequestReport, backref=db.backref("unsubscribe_requests"))

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["template_id", "template_version"],
            ["templates_history.id", "templates_history.version"],
        ),
        Index("ix_unsubscribe_request_notification_id", "notification_id"),
        Index("ix_unsubscribe_request_unsubscribe_request_report_id", "unsubscribe_request_report_id"),
    )

    def serialize_for_history(self):
        return {
            column.key: getattr(self, column.key) for column in self.__table__.columns if column.key != "email_address"
        }


class UnsubscribeRequestHistory(db.Model):
    __tablename__ = "unsubscribe_request_history"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Ignoring a strict foreign key relationship here for now. Notifications are archived to the NotificationHistory
    # table by a nightly job and I haven't investigated whether that might break a strict FK yet or if it would
    # work smoothly. We can still have a relationship using an explicit join condition.
    notification_id = db.Column(UUID(as_uuid=True), index=True, nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), nullable=False)
    template_id = db.Column(UUID(as_uuid=True), nullable=False)
    template_version = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    unsubscribe_request_report_id = db.Column(UUID(as_uuid=True), index=True, nullable=True)


class ProtectedSenderId(db.Model):
    __tablename__ = "protected_sender_ids"

    sender_id = db.Column(db.String, primary_key=True, nullable=False)


@dataclass
class SerializedServiceJoinRequest:
    service_join_request_id: str
    requester_id: str
    service_id: str
    created_at: str
    status: str
    status_changed_at: str | None
    status_changed_by_id: str | None
    reason: str | None
    contacted_service_users: list[str]


contacted_users = db.Table(
    "contacted_users",
    db.Model.metadata,
    db.Column(
        "service_join_request_id", UUID(as_uuid=True), db.ForeignKey("service_join_requests.id"), primary_key=True
    ),
    db.Column("user_id", UUID(as_uuid=True), db.ForeignKey("users.id"), primary_key=True),
)


class ServiceJoinRequest(db.Model):
    __tablename__ = "service_join_requests"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requester_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=False)
    service_id = db.Column(UUID(as_uuid=True), db.ForeignKey("services.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.datetime.utcnow())
    status = db.Column(
        db.Enum(*REQUEST_STATUS_VALUES, name="request_status"), nullable=False, default=JOIN_REQUEST_PENDING
    )
    status_changed_at = db.Column(db.DateTime, nullable=True)
    status_changed_by_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    reason = db.Column(db.Text, nullable=True)

    requester = db.relationship("User", foreign_keys=[requester_id])
    status_changed_by = db.relationship("User", foreign_keys=[status_changed_by_id])

    # Use lazy="joined" to load the contacted_service_users relationship with a SQL JOIN
    # This is a nice option as we expect to load this relationship frequently when querying ServiceJoinRequest
    contacted_service_users = db.relationship(
        "User", secondary=contacted_users, backref="service_join_requests", lazy="joined"
    )

    def serialize(self) -> SerializedServiceJoinRequest:
        return SerializedServiceJoinRequest(
            service_join_request_id=get_uuid_string_or_none(self.id),
            requester_id=get_uuid_string_or_none(self.requester_id),
            service_id=get_uuid_string_or_none(self.service_id),
            created_at=get_dt_string_or_none(self.created_at),
            status=self.status,
            status_changed_at=get_dt_string_or_none(self.status_changed_at),
            status_changed_by_id=get_uuid_string_or_none(self.status_changed_by_id),
            reason=self.reason,
            contacted_service_users=[get_uuid_string_or_none(user.id) for user in self.contacted_service_users],
        )
