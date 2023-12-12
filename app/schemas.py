from datetime import datetime, timedelta
from uuid import UUID

from dateutil.parser import parse
from flask_marshmallow.fields import fields
from marshmallow import (
    EXCLUDE,
    ValidationError,
    post_dump,
    post_load,
    pre_dump,
    pre_load,
    validates,
    validates_schema,
)
from marshmallow_sqlalchemy import field_for
from notifications_utils.recipient_validation.email_address import validate_email_address
from notifications_utils.recipient_validation.errors import InvalidEmailError, InvalidPhoneError
from notifications_utils.recipient_validation.phone_number import (
    validate_and_format_phone_number,
    validate_phone_number,
)

import app.constants
from app import db, ma, models
from app.dao.permissions_dao import permission_dao
from app.models import ServicePermission
from app.utils import DATETIME_FORMAT_NO_TIMEZONE


def _validate_positive_number(value, msg="Not a positive integer"):
    try:
        page_int = int(value)
    except ValueError as e:
        raise ValidationError(msg) from e
    if page_int < 1:
        raise ValidationError(msg)


class FlexibleDateTime(fields.DateTime):
    """
    Allows input data to not contain tz info.
    Outputs data using the output format that marshmallow version 2 used to use, OLD_MARSHMALLOW_FORMAT
    """

    DEFAULT_FORMAT = "flexible"
    OLD_MARSHMALLOW_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"

    def __init__(self, *args, allow_none=True, **kwargs):
        super().__init__(*args, allow_none=allow_none, **kwargs)
        self.DESERIALIZATION_FUNCS["flexible"] = parse
        self.SERIALIZATION_FUNCS["flexible"] = lambda x: x.strftime(self.OLD_MARSHMALLOW_FORMAT)


class UUIDsAsStringsMixin:
    @post_dump()
    def __post_dump(self, data, **kwargs):
        for key, value in data.items():
            if isinstance(value, UUID):
                data[key] = str(value)

            if isinstance(value, list):
                data[key] = [(str(item) if isinstance(item, UUID) else item) for item in value]
        return data


class BaseSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        load_instance = True
        include_relationships = True
        unknown = EXCLUDE
        sqla_session = db.session

    def __init__(self, load_json=False, *args, **kwargs):
        self.load_json = load_json
        super().__init__(*args, **kwargs)

    @post_load
    def make_instance(self, data, **kwargs):
        """Deserialize data to an instance of the model. Update an existing row
        if specified in `self.instance` or loaded by primary key(s) in the data;
        else create a new row.
        :param data: Data to deserialize.
        """
        if self.load_json:
            return data
        return super().make_instance(data)


class UserSchema(BaseSchema):
    permissions = fields.Method("user_permissions", dump_only=True)
    password_changed_at = field_for(models.User, "password_changed_at", format=DATETIME_FORMAT_NO_TIMEZONE)
    created_at = field_for(models.User, "created_at", format=DATETIME_FORMAT_NO_TIMEZONE)
    updated_at = FlexibleDateTime()
    logged_in_at = FlexibleDateTime()
    auth_type = field_for(models.User, "auth_type")
    password = fields.String(required=True, load_only=True)

    def user_permissions(self, usr):
        retval = {}
        for x in permission_dao.get_permissions_by_user_id(usr.id):
            service_id = str(x.service_id)
            if service_id not in retval:
                retval[service_id] = []
            retval[service_id].append(x.permission)
        return retval

    class Meta(BaseSchema.Meta):
        model = models.User
        exclude = (
            "_password",
            "created_at",
            "email_access_validated_at",
            "updated_at",
            "verify_codes",
        )

    @validates("name")
    def validate_name(self, value):
        if not value:
            raise ValidationError("Invalid name")

    @validates("email_address")
    def validate_email_address(self, value):
        try:
            validate_email_address(value)
        except InvalidEmailError as e:
            raise ValidationError(str(e)) from e

    @validates("mobile_number")
    def validate_mobile_number(self, value):
        try:
            if value is not None:
                validate_phone_number(value, international=True)
        except InvalidPhoneError as error:
            raise ValidationError(f"Invalid phone number: {error.get_legacy_v2_api_error_message()}") from error


class UserUpdateAttributeSchema(BaseSchema):
    auth_type = field_for(models.User, "auth_type")
    email_access_validated_at = FlexibleDateTime()

    class Meta(BaseSchema.Meta):
        model = models.User
        exclude = (
            "_password",
            "created_at",
            "failed_login_count",
            "id",
            "logged_in_at",
            "password_changed_at",
            "platform_admin",
            "state",
            "updated_at",
            "verify_codes",
        )

    @validates("name")
    def validate_name(self, value):
        if not value:
            raise ValidationError("Invalid name")

    @validates("email_address")
    def validate_email_address(self, value):
        try:
            validate_email_address(value)
        except InvalidEmailError as e:
            raise ValidationError(str(e)) from e

    @validates("mobile_number")
    def validate_mobile_number(self, value):
        try:
            if value is not None:
                validate_phone_number(value, international=True)
        except InvalidPhoneError as error:
            raise ValidationError(f"Invalid phone number: {error.get_legacy_v2_api_error_message()}") from error

    @validates_schema(pass_original=True)
    def check_unknown_fields(self, data, original_data, **kwargs):
        for key in original_data:
            if key not in self.fields:
                raise ValidationError(f"Unknown field name {key}")


class UserUpdatePasswordSchema(BaseSchema):
    class Meta(BaseSchema.Meta):
        model = models.User

    @validates_schema(pass_original=True)
    def check_unknown_fields(self, data, original_data, **kwargs):
        for key in original_data:
            if key not in self.fields:
                raise ValidationError(f"Unknown field name {key}")


class ProviderDetailsSchema(BaseSchema):
    created_by = fields.Nested(UserSchema, only=["id", "name", "email_address"], dump_only=True)
    updated_at = FlexibleDateTime()

    class Meta(BaseSchema.Meta):
        model = models.ProviderDetails


class ProviderDetailsHistorySchema(BaseSchema):
    created_by = fields.Nested(UserSchema, only=["id", "name", "email_address"], dump_only=True)
    updated_at = FlexibleDateTime()

    class Meta(BaseSchema.Meta):
        model = models.ProviderDetailsHistory


class ServiceSchema(BaseSchema, UUIDsAsStringsMixin):
    created_by = field_for(models.Service, "created_by", required=True)
    organisation_type = field_for(models.Service, "organisation_type")
    letter_logo_filename = fields.Method(dump_only=True, serialize="get_letter_logo_filename")
    permissions = fields.Method("serialize_service_permissions", "deserialize_service_permissions")
    email_branding = field_for(models.Service, "email_branding")
    organisation = field_for(models.Service, "organisation")
    email_message_limit = field_for(models.Service, "email_message_limit", required=True)
    sms_message_limit = field_for(models.Service, "sms_message_limit", required=True)
    letter_message_limit = field_for(models.Service, "letter_message_limit", required=True)
    go_live_at = field_for(models.Service, "go_live_at", format=DATETIME_FORMAT_NO_TIMEZONE)
    name = fields.String(required=True)
    custom_email_sender_name = fields.String(allow_none=True)
    # this can only be set via custom_email_sender_name or name
    email_sender_local_part = fields.String(dump_only=True)
    service_callback_api = fields.Method("service_delivery_status_callback_api")

    def service_delivery_status_callback_api(self, service):
        return [
            callback.id
            for callback in service.service_callback_api
            if callback.callback_type == app.constants.DELIVERY_STATUS_CALLBACK_TYPE
        ]

    def get_letter_logo_filename(self, service):
        return service.letter_branding and service.letter_branding.filename

    def serialize_service_permissions(self, service):
        return [p.permission for p in service.permissions]

    def deserialize_service_permissions(self, in_data):
        if isinstance(in_data, dict) and "permissions" in in_data:
            str_permissions = in_data["permissions"]
            permissions = []
            for p in str_permissions:
                permission = ServicePermission(service_id=in_data["id"], permission=p)
                permissions.append(permission)

            in_data["permissions"] = permissions

        return in_data

    def get_letter_contact(self, service):
        return service.get_default_letter_contact()

    class Meta(BaseSchema.Meta):
        model = models.Service
        exclude = (
            "all_template_folders",
            "annual_billing",
            "api_keys",
            "complaints",
            "contact_list",
            "created_at",
            "crown",
            "data_retention",
            "guest_list",
            "inbound_number",
            "inbound_sms",
            "jobs",
            "letter_contacts",
            "letter_logo_filename",
            "reply_to_email_addresses",
            "returned_letters",
            "service_sms_senders",
            "templates",
            "updated_at",
            "users",
            "unsubscribe_request_reports",
            "unsubscribe_requests",
            "version",
            "_name",
            "_normalised_service_name",
            "_custom_email_sender_name",
            "_email_sender_local_part",
        )

    @validates("permissions")
    def validate_permissions(self, value):
        permissions = [v.permission for v in value]
        for p in permissions:
            if p not in app.constants.SERVICE_PERMISSION_TYPES:
                raise ValidationError(f"Invalid Service Permission: '{p}'")

        if len(set(permissions)) != len(permissions):
            duplicates = list({x for x in permissions if permissions.count(x) > 1})
            raise ValidationError(f"Duplicate Service Permission: {duplicates}")

    @pre_load()
    def format_for_data_model(self, in_data, **kwargs):
        if isinstance(in_data, dict) and "permissions" in in_data:
            str_permissions = in_data["permissions"]
            permissions = []
            for p in str_permissions:
                permission = ServicePermission(service_id=in_data["id"], permission=p)
                permissions.append(permission)

            in_data["permissions"] = permissions

        return in_data


class DetailedServiceSchema(BaseSchema):
    statistics = fields.Dict()
    organisation_type = field_for(models.Service, "organisation_type")
    go_live_at = FlexibleDateTime()
    created_at = FlexibleDateTime()
    updated_at = FlexibleDateTime()
    name = fields.String()
    custom_email_sender_name = fields.String(required=False)
    email_sender_local_part = fields.String()
    service_callback_api = fields.Method("service_delivery_status_callback_api")

    def service_delivery_status_callback_api(self, service):
        return [
            callback.id
            for callback in service.service_callback_api
            if callback.callback_type == app.constants.DELIVERY_STATUS_CALLBACK_TYPE
        ]

    class Meta(BaseSchema.Meta):
        model = models.Service
        exclude = (
            "all_template_folders",
            "annual_billing",
            "api_keys",
            "contact_list",
            "created_by",
            "crown",
            "_name",
            "_normalised_service_name",
            "_custom_email_sender_name",
            "_email_sender_local_part",
            "email_branding",
            "email_message_limit",
            "guest_list",
            "inbound_api",
            "inbound_number",
            "inbound_sms",
            "jobs",
            "letter_message_limit",
            "permissions",
            "rate_limit",
            "reply_to_email_addresses",
            "returned_letters",
            "service_sms_senders",
            "sms_message_limit",
            "templates",
            "users",
            "version",
        )


class NotificationModelSchema(BaseSchema):
    class Meta(BaseSchema.Meta):
        model = models.Notification
        exclude = (
            "_personalisation",
            "job",
            "service",
            "template",
            "api_key",
        )

    status = fields.String(required=False)
    created_at = FlexibleDateTime()
    sent_at = FlexibleDateTime()
    updated_at = FlexibleDateTime()


class BaseTemplateSchema(BaseSchema):
    reply_to = fields.Method("get_reply_to", allow_none=True)
    reply_to_text = fields.Method("get_reply_to_text", allow_none=True)
    letter_attachment = fields.Method("get_letter_attachment", allow_none=True)
    letter_languages = fields.Method("get_letter_languages", "load_letter_languages", allow_none=True)

    def get_reply_to(self, template):
        return template.reply_to

    def get_reply_to_text(self, template):
        return template.get_reply_to_text()

    def get_letter_attachment(self, template):
        return template.letter_attachment.serialize() if template.letter_attachment_id else None

    def get_letter_languages(self, template):
        return template.letter_languages

    def load_letter_languages(self, value):
        return app.constants.LetterLanguageOptions(value) if value else None

    class Meta(BaseSchema.Meta):
        model = models.Template
        exclude = ("service_id", "jobs", "service_letter_contact_id", "unsubscribe_requests")


class TemplateSchema(BaseTemplateSchema, UUIDsAsStringsMixin):
    created_by = field_for(models.Template, "created_by", required=True)
    process_type = field_for(models.Template, "process_type")
    redact_personalisation = fields.Method("redact")
    is_precompiled_letter = fields.Method("get_is_precompiled_letter")
    created_at = FlexibleDateTime()
    updated_at = FlexibleDateTime()

    def redact(self, template):
        return template.redact_personalisation

    def get_is_precompiled_letter(self, template):
        return template.is_precompiled_letter

    @validates_schema
    def validate_type(self, data, **kwargs):
        if data.get("template_type") in {app.constants.EMAIL_TYPE, app.constants.LETTER_TYPE}:
            subject = data.get("subject")
            if not subject or subject.strip() == "":
                raise ValidationError("Invalid template subject", "subject")


class TemplateSchemaNoDetail(TemplateSchema):
    class Meta(TemplateSchema.Meta):
        fields = [
            "folder",
            "id",
            "is_precompiled_letter",
            "name",
            "template_type",
        ]
        exclude = []


class TemplateHistorySchema(BaseSchema):
    reply_to = fields.Method("get_reply_to", allow_none=True)
    reply_to_text = fields.Method("get_reply_to_text", allow_none=True)
    process_type = field_for(models.Template, "process_type")
    is_precompiled_letter = fields.Method("get_is_precompiled_letter")
    letter_attachment = fields.Method("get_letter_attachment", allow_none=True)

    created_by = fields.Nested(UserSchema, only=["id", "name", "email_address"], dump_only=True)
    created_at = field_for(models.Template, "created_at", format=DATETIME_FORMAT_NO_TIMEZONE)
    updated_at = FlexibleDateTime()

    def get_reply_to(self, template):
        return template.reply_to

    def get_reply_to_text(self, template):
        return template.get_reply_to_text()

    def get_letter_attachment(self, template):
        return template.letter_attachment.serialize() if template.letter_attachment_id else None

    def get_is_precompiled_letter(self, template):
        return template.is_precompiled_letter

    class Meta(BaseSchema.Meta):
        model = models.TemplateHistory


class ApiKeySchema(BaseSchema):
    created_by = field_for(models.ApiKey, "created_by", required=True)
    key_type = field_for(models.ApiKey, "key_type", required=True)
    expiry_date = FlexibleDateTime()
    created_at = FlexibleDateTime()
    updated_at = FlexibleDateTime()

    class Meta(BaseSchema.Meta):
        model = models.ApiKey
        exclude = ("service", "_secret")


class JobSchema(BaseSchema):
    created_by_user = fields.Nested(
        UserSchema, attribute="created_by", data_key="created_by", only=["id", "name"], dump_only=True
    )
    created_by = field_for(models.Job, "created_by", required=True, load_only=True)
    created_at = FlexibleDateTime()
    updated_at = FlexibleDateTime()
    processing_started = FlexibleDateTime()
    processing_finished = FlexibleDateTime()

    job_status = field_for(models.JobStatus, "name", required=False)

    scheduled_for = FlexibleDateTime()
    service_name = fields.Nested(
        ServiceSchema, attribute="service", data_key="service_name", only=["name"], dump_only=True
    )

    template_name = fields.Method("get_template_name", dump_only=True)
    template_type = fields.Method("get_template_type", dump_only=True)
    contact_list_id = field_for(models.Job, "contact_list_id")

    def get_template_name(self, job):
        return job.template.name

    def get_template_type(self, job):
        return job.template.template_type

    @validates("scheduled_for")
    def validate_scheduled_for(self, value):
        if value < datetime.utcnow():
            raise ValidationError("Date cannot be in the past")

        if value > datetime.utcnow() + timedelta(days=7):
            raise ValidationError("Date cannot be more than 7 days in the future")

    class Meta(BaseSchema.Meta):
        model = models.Job
        exclude = (
            "notifications",
            "notifications_delivered",
            "notifications_failed",
            "notifications_sent",
        )


class NotificationSchema(ma.Schema):
    class Meta:
        unknown = EXCLUDE

    status = fields.String(required=False)
    personalisation = fields.Dict(required=False)


class SmsNotificationSchema(NotificationSchema):
    to = fields.Str(required=True)

    @validates("to")
    def validate_to(self, value):
        try:
            validate_phone_number(value, international=True)
        except InvalidPhoneError as error:
            raise ValidationError(f"Invalid phone number: {error.get_legacy_v2_api_error_message()}") from error

    @post_load
    def format_phone_number(self, item, **kwargs):
        item["to"] = validate_and_format_phone_number(item["to"], international=True)
        return item


class NotificationWithTemplateSchema(BaseSchema):
    class Meta(BaseSchema.Meta):
        model = models.Notification
        exclude = ("_personalisation",)

    template = fields.Nested(
        TemplateSchema,
        only=[
            "id",
            "service",
            "version",
            "name",
            "template_type",
            "content",
            "subject",
            "redact_personalisation",
            "is_precompiled_letter",
            "letter_attachment",
            "reply_to_text",
            "letter_languages",
            "letter_welsh_subject",
            "letter_welsh_content",
        ],
        dump_only=True,
    )
    job = fields.Nested(JobSchema, only=["id", "original_file_name"], dump_only=True)
    created_by = fields.Nested(UserSchema, only=["id", "name", "email_address"], dump_only=True)
    status = fields.String(required=False)
    personalisation = fields.Dict(required=False)
    key_type = field_for(models.Notification, "key_type", required=True)
    key_name = fields.String()
    created_at = FlexibleDateTime()
    updated_at = FlexibleDateTime()
    sent_at = FlexibleDateTime()

    @pre_dump
    def add_api_key_name(self, in_data, **kwargs):
        if in_data.api_key:
            in_data.key_name = in_data.api_key.name
        else:
            in_data.key_name = None
        return in_data


class InvitedUserSchema(BaseSchema):
    auth_type = field_for(models.InvitedUser, "auth_type")
    created_at = FlexibleDateTime()

    class Meta(BaseSchema.Meta):
        model = models.InvitedUser

    @validates("email_address")
    def validate_to(self, value):
        try:
            validate_email_address(value)
        except InvalidEmailError as e:
            raise ValidationError(str(e)) from e


class EmailDataSchema(ma.Schema):
    class Meta:
        unknown = EXCLUDE

    email = fields.Str(required=True)
    next = fields.Str(required=False)
    admin_base_url = fields.Str(required=False)

    def __init__(self, partial_email=False):
        super().__init__()
        self.partial_email = partial_email

    @validates("email")
    def validate_email(self, value):
        if self.partial_email:
            return
        try:
            validate_email_address(value)
        except InvalidEmailError as e:
            raise ValidationError(str(e)) from e


class NotificationsFilterSchema(ma.Schema):
    class Meta:
        unknown = EXCLUDE

    template_type = fields.Nested(BaseTemplateSchema, only=["template_type"], many=True)
    status = fields.Nested(NotificationModelSchema, only=["status"], many=True)
    page = fields.Int(required=False)
    page_size = fields.Int(required=False)
    limit_days = fields.Int(required=False)
    include_jobs = fields.Boolean(required=False)
    include_from_test_key = fields.Boolean(required=False)
    paginate_by_older_than = fields.Boolean(required=False)
    older_than = fields.UUID(required=False)
    format_for_csv = fields.String()
    to = fields.String()
    include_one_off = fields.Boolean(required=False)
    count_pages = fields.Boolean(required=False)

    @pre_load
    def handle_multidict(self, in_data, **kwargs):
        if isinstance(in_data, dict) and hasattr(in_data, "getlist"):
            out_data = {k: in_data.get(k) for k in in_data.keys()}
            if "template_type" in in_data:
                out_data["template_type"] = [{"template_type": x} for x in in_data.getlist("template_type")]
            if "status" in in_data:
                out_data["status"] = [{"status": x} for x in in_data.getlist("status")]

        return out_data

    @post_load
    def convert_schema_object_to_field(self, in_data, **kwargs):
        if "template_type" in in_data:
            in_data["template_type"] = [x.template_type for x in in_data["template_type"]]
        if "status" in in_data:
            in_data["status"] = [x.status for x in in_data["status"]]
        return in_data

    @validates("page")
    def validate_page(self, value):
        _validate_positive_number(value)

    @validates("page_size")
    def validate_page_size(self, value):
        _validate_positive_number(value)


class ServiceHistorySchema(ma.Schema):
    class Meta:
        unknown = EXCLUDE

    id = fields.UUID()
    name = fields.String()
    created_at = FlexibleDateTime()
    updated_at = FlexibleDateTime()
    active = fields.Boolean()
    email_message_limit = fields.Integer()
    sms_message_limit = fields.Integer()
    letter_message_limit = fields.Integer()
    restricted = fields.Boolean()
    custom_email_sender_name = fields.String()
    email_sender_local_part = fields.String()
    created_by_id = fields.UUID()
    version = fields.Integer()


class ApiKeyHistorySchema(ma.Schema):
    class Meta:
        unknown = EXCLUDE

    id = fields.UUID()
    name = fields.String()
    service_id = fields.UUID()
    expiry_date = FlexibleDateTime()
    created_at = FlexibleDateTime()
    updated_at = FlexibleDateTime()
    created_by_id = fields.UUID()


class EventSchema(BaseSchema):
    created_at = FlexibleDateTime()

    class Meta(BaseSchema.Meta):
        model = models.Event


class UnarchivedTemplateSchema(BaseSchema):
    archived = fields.Boolean(required=True)

    @validates_schema
    def validate_archived(self, data, **kwargs):
        if data["archived"]:
            raise ValidationError("Template has been deleted", "template")


# should not be used on its own for dumping - only for loading
create_user_schema = UserSchema()
user_update_schema_load_json = UserUpdateAttributeSchema(load_json=True, partial=True)
user_update_password_schema_load_json = UserUpdatePasswordSchema(only=("_password",), load_json=True, partial=True)
service_schema = ServiceSchema()
detailed_service_schema = DetailedServiceSchema()
template_schema = TemplateSchema()
template_schema_no_detail = TemplateSchemaNoDetail()
api_key_schema = ApiKeySchema()
job_schema = JobSchema()
notification_schema = NotificationModelSchema()
notification_with_template_schema = NotificationWithTemplateSchema()
invited_user_schema = InvitedUserSchema()
email_data_request_schema = EmailDataSchema()
partial_email_data_request_schema = EmailDataSchema(partial_email=True)
notifications_filter_schema = NotificationsFilterSchema()
service_history_schema = ServiceHistorySchema()
api_key_history_schema = ApiKeyHistorySchema()
template_history_schema = TemplateHistorySchema()
event_schema = EventSchema()
provider_details_schema = ProviderDetailsSchema()
provider_details_history_schema = ProviderDetailsHistorySchema()
unarchived_template_schema = UnarchivedTemplateSchema()
