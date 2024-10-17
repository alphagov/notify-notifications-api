# Notification status values
import enum

NOTIFICATION_CANCELLED = "cancelled"
NOTIFICATION_CREATED = "created"
NOTIFICATION_SENDING = "sending"
NOTIFICATION_SENT = "sent"
NOTIFICATION_DELIVERED = "delivered"
NOTIFICATION_PENDING = "pending"
NOTIFICATION_FAILED = "failed"
NOTIFICATION_TECHNICAL_FAILURE = "technical-failure"
NOTIFICATION_TEMPORARY_FAILURE = "temporary-failure"
NOTIFICATION_PERMANENT_FAILURE = "permanent-failure"
NOTIFICATION_PENDING_VIRUS_CHECK = "pending-virus-check"
NOTIFICATION_VALIDATION_FAILED = "validation-failed"
NOTIFICATION_VIRUS_SCAN_FAILED = "virus-scan-failed"
NOTIFICATION_RETURNED_LETTER = "returned-letter"

# Raw notification status values grouped into types
NOTIFICATION_STATUS_TYPES_FAILED = [
    NOTIFICATION_TECHNICAL_FAILURE,
    NOTIFICATION_TEMPORARY_FAILURE,
    NOTIFICATION_PERMANENT_FAILURE,
    NOTIFICATION_VALIDATION_FAILED,
    NOTIFICATION_VIRUS_SCAN_FAILED,
    NOTIFICATION_RETURNED_LETTER,
]
NOTIFICATION_STATUS_TYPES_COMPLETED = [
    NOTIFICATION_SENT,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_FAILED,
    NOTIFICATION_TECHNICAL_FAILURE,
    NOTIFICATION_TEMPORARY_FAILURE,
    NOTIFICATION_PERMANENT_FAILURE,
    NOTIFICATION_RETURNED_LETTER,
    NOTIFICATION_CANCELLED,
]
NOTIFICATION_STATUS_SUCCESS = [NOTIFICATION_SENT, NOTIFICATION_DELIVERED]
NOTIFICATION_STATUS_TYPES_BILLABLE = [
    NOTIFICATION_SENDING,
    NOTIFICATION_SENT,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_PENDING,
    NOTIFICATION_FAILED,
    NOTIFICATION_TEMPORARY_FAILURE,
    NOTIFICATION_PERMANENT_FAILURE,
    NOTIFICATION_RETURNED_LETTER,
]
NOTIFICATION_STATUS_TYPES_BILLABLE_SMS = [
    NOTIFICATION_SENDING,
    NOTIFICATION_SENT,  # internationally
    NOTIFICATION_DELIVERED,
    NOTIFICATION_PENDING,
    NOTIFICATION_TEMPORARY_FAILURE,
    NOTIFICATION_PERMANENT_FAILURE,
]
NOTIFICATION_STATUS_TYPES_BILLABLE_FOR_LETTERS = [
    NOTIFICATION_SENDING,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_RETURNED_LETTER,
]

NOTIFICATION_STATUS_TYPES_LETTERS_NEVER_SENT = [
    NOTIFICATION_CANCELLED,
    NOTIFICATION_TECHNICAL_FAILURE,
    NOTIFICATION_VALIDATION_FAILED,
    NOTIFICATION_VIRUS_SCAN_FAILED,
]

# we don't really have a concept of billable emails - however the ft billing table only includes emails that we have
# actually sent.
NOTIFICATION_STATUS_TYPES_SENT_EMAILS = [
    NOTIFICATION_SENDING,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_TEMPORARY_FAILURE,
    NOTIFICATION_PERMANENT_FAILURE,
]
NOTIFICATION_STATUS_TYPES = [
    NOTIFICATION_CANCELLED,
    NOTIFICATION_CREATED,
    NOTIFICATION_SENDING,
    NOTIFICATION_SENT,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_PENDING,
    NOTIFICATION_FAILED,
    NOTIFICATION_TECHNICAL_FAILURE,
    NOTIFICATION_TEMPORARY_FAILURE,
    NOTIFICATION_PERMANENT_FAILURE,
    NOTIFICATION_PENDING_VIRUS_CHECK,
    NOTIFICATION_VALIDATION_FAILED,
    NOTIFICATION_VIRUS_SCAN_FAILED,
    NOTIFICATION_RETURNED_LETTER,
]
NOTIFICATION_STATUS_TYPES_NON_BILLABLE = list(set(NOTIFICATION_STATUS_TYPES) - set(NOTIFICATION_STATUS_TYPES_BILLABLE))

# Letter statuses according to our print provider
NOTIFICATION_STATUS_LETTER_ACCEPTED = "accepted"
NOTIFICATION_STATUS_LETTER_RECEIVED = "received"
DVLA_RESPONSE_STATUS_SENT = "Sent"

# DVLA letter callback status
DVLA_NOTIFICATION_DISPATCHED = "Despatched"
DVLA_NOTIFICATION_REJECTED = "Rejected"

# Mapping from DVLA status to internal notification status
DVLA_TO_NOTIFICATION_STATUS_MAP = {
    DVLA_NOTIFICATION_DISPATCHED: NOTIFICATION_DELIVERED,
    DVLA_NOTIFICATION_REJECTED: NOTIFICATION_TECHNICAL_FAILURE,
}

# Letter postage zones
FIRST_CLASS = "first"
SECOND_CLASS = "second"
EUROPE = "europe"
REST_OF_WORLD = "rest-of-world"

# Aggregated letter postage types
POSTAGE_TYPES = [FIRST_CLASS, SECOND_CLASS, EUROPE, REST_OF_WORLD]
UK_POSTAGE_TYPES = [FIRST_CLASS, SECOND_CLASS]
INTERNATIONAL_POSTAGE_TYPES = [EUROPE, REST_OF_WORLD]

# Map postage zones to abbreviations for our print provider
RESOLVE_POSTAGE_FOR_FILE_NAME = {
    FIRST_CLASS: 1,
    SECOND_CLASS: 2,
    EUROPE: "E",
    REST_OF_WORLD: "N",
}

# User permissions
INVITE_PENDING = "pending"
INVITE_ACCEPTED = "accepted"
INVITE_CANCELLED = "cancelled"
INVITED_USER_STATUS_TYPES = [INVITE_PENDING, INVITE_ACCEPTED, INVITE_CANCELLED]

# Template constants
PRECOMPILED_TEMPLATE_NAME = "Pre-compiled PDF"

# Types of templates, notifications, permissions
SMS_TYPE = "sms"
EMAIL_TYPE = "email"
LETTER_TYPE = "letter"
BROADCAST_TYPE = "broadcast"
TEMPLATE_TYPES = [SMS_TYPE, EMAIL_TYPE, LETTER_TYPE, BROADCAST_TYPE]
NOTIFICATION_TYPES = [SMS_TYPE, EMAIL_TYPE, LETTER_TYPE]  # not broadcast
NOTIFICATION_TYPE = [EMAIL_TYPE, SMS_TYPE, LETTER_TYPE]  # duplicate that can probably be cleaned up


# Language options supported for bilingual letter templates
class LetterLanguageOptions(str, enum.Enum):
    english = "english"
    welsh_then_english = "welsh_then_english"


# Organisations
ORG_TYPE_CENTRAL = "central"
ORG_TYPE_LOCAL = "local"
ORG_TYPE_NHS_CENTRAL = "nhs_central"
ORG_TYPE_NHS_LOCAL = "nhs_local"
ORG_TYPE_NHS_GP = "nhs_gp"
ORG_TYPE_EMERGENCY_SERVICE = "emergency_service"
ORG_TYPE_SCHOOL_OR_COLLEGE = "school_or_college"
ORG_TYPE_OTHER = "other"
ORGANISATION_TYPES = [
    ORG_TYPE_CENTRAL,
    ORG_TYPE_LOCAL,
    ORG_TYPE_NHS_CENTRAL,
    ORG_TYPE_NHS_LOCAL,
    ORG_TYPE_NHS_GP,
    ORG_TYPE_EMERGENCY_SERVICE,
    ORG_TYPE_SCHOOL_OR_COLLEGE,
    ORG_TYPE_OTHER,
]
CROWN_ORGANISATION_TYPES = ["nhs_central"]
NON_CROWN_ORGANISATION_TYPES = ["local", "nhs_local", "nhs_gp", "emergency_service", "school_or_college"]
NHS_ORGANISATION_TYPES = ["nhs_central", "nhs_local", "nhs_gp"]


# Service permissions
MANAGE_USERS = "manage_users"
MANAGE_TEMPLATES = "manage_templates"
MANAGE_SETTINGS = "manage_settings"
SEND_TEXTS = "send_texts"
SEND_EMAILS = "send_emails"
SEND_LETTERS = "send_letters"
MANAGE_API_KEYS = "manage_api_keys"
PLATFORM_ADMIN = "platform_admin"
VIEW_ACTIVITY = "view_activity"
CREATE_BROADCASTS = "create_broadcasts"
APPROVE_BROADCASTS = "approve_broadcasts"
CANCEL_BROADCASTS = "cancel_broadcasts"
REJECT_BROADCASTS = "reject_broadcasts"
INTERNATIONAL_SMS_TYPE = "international_sms"
INBOUND_SMS_TYPE = "inbound_sms"
SCHEDULE_NOTIFICATIONS = "schedule_notifications"
EMAIL_AUTH = "email_auth"
LETTERS_AS_PDF = "letters_as_pdf"
PRECOMPILED_LETTER = "precompiled_letter"
UPLOAD_DOCUMENT = "upload_document"
EDIT_FOLDER_PERMISSIONS = "edit_folder_permissions"
UPLOAD_LETTERS = "upload_letters"
INTERNATIONAL_LETTERS = "international_letters"
EXTRA_EMAIL_FORMATTING = "extra_email_formatting"
EXTRA_LETTER_FORMATTING = "extra_letter_formatting"
SMS_TO_UK_LANDLINES = "sms_to_uk_landlines"
SERVICE_PERMISSION_TYPES = [
    EMAIL_TYPE,
    SMS_TYPE,
    LETTER_TYPE,
    BROADCAST_TYPE,
    INTERNATIONAL_SMS_TYPE,
    INBOUND_SMS_TYPE,
    SCHEDULE_NOTIFICATIONS,
    EMAIL_AUTH,
    LETTERS_AS_PDF,
    UPLOAD_DOCUMENT,
    EDIT_FOLDER_PERMISSIONS,
    UPLOAD_LETTERS,
    INTERNATIONAL_LETTERS,
    EXTRA_EMAIL_FORMATTING,
    EXTRA_LETTER_FORMATTING,
    SMS_TO_UK_LANDLINES,
]

# List of available permissions
PERMISSION_LIST = [
    MANAGE_USERS,
    MANAGE_TEMPLATES,
    MANAGE_SETTINGS,
    SEND_TEXTS,
    SEND_EMAILS,
    SEND_LETTERS,
    MANAGE_API_KEYS,
    PLATFORM_ADMIN,
    VIEW_ACTIVITY,
    CREATE_BROADCASTS,
    APPROVE_BROADCASTS,
    CANCEL_BROADCASTS,
    REJECT_BROADCASTS,
]


CAN_ASK_TO_JOIN_SERVICE = "can_ask_to_join_a_service"
ORGANISATION_PERMISSION_TYPES = [
    CAN_ASK_TO_JOIN_SERVICE,
]


# Organisation user permissions
class OrganisationUserPermissionTypes(enum.Enum):
    can_make_services_live = "can_make_services_live"


# Prioritisation for template processing
# PRIORITY queue is now archived and should be ripe for cleanup.
NORMAL = "normal"
PRIORITY = "priority"
TEMPLATE_PROCESS_TYPE = [NORMAL, PRIORITY]

# User authetication
SMS_AUTH_TYPE = "sms_auth"
EMAIL_AUTH_TYPE = "email_auth"
WEBAUTHN_AUTH_TYPE = "webauthn_auth"
USER_AUTH_TYPES = [SMS_AUTH_TYPE, EMAIL_AUTH_TYPE, WEBAUTHN_AUTH_TYPE]
VERIFY_CODE_TYPES = [EMAIL_TYPE, SMS_TYPE]

# Service callbacks
DELIVERY_STATUS_CALLBACK_TYPE = "delivery_status"
COMPLAINT_CALLBACK_TYPE = "complaint"
SERVICE_CALLBACK_TYPES = [DELIVERY_STATUS_CALLBACK_TYPE, COMPLAINT_CALLBACK_TYPE]

# Branding values
BRANDING_GOVUK = "govuk"  # Deprecated outside migrations
BRANDING_ORG = "org"
BRANDING_BOTH = "both"
BRANDING_ORG_BANNER = "org_banner"
BRANDING_TYPES = [BRANDING_ORG, BRANDING_BOTH, BRANDING_ORG_BANNER]

# API Keys
KEY_TYPE_NORMAL = "normal"
KEY_TYPE_TEAM = "team"
KEY_TYPE_TEST = "test"

# Providers
MMG_PROVIDER = "mmg"
FIRETEXT_PROVIDER = "firetext"
SES_PROVIDER = "ses"
SMS_PROVIDERS = [MMG_PROVIDER, FIRETEXT_PROVIDER]
EMAIL_PROVIDERS = [SES_PROVIDER]
PROVIDERS = SMS_PROVIDERS + EMAIL_PROVIDERS
ALL_BROADCAST_PROVIDERS = "all"

# Jobs
JOB_STATUS_PENDING = "pending"
JOB_STATUS_IN_PROGRESS = "in progress"
JOB_STATUS_FINISHED = "finished"
JOB_STATUS_SENDING_LIMITS_EXCEEDED = "sending limits exceeded"
JOB_STATUS_SCHEDULED = "scheduled"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUS_READY_TO_SEND = "ready to send"
JOB_STATUS_SENT_TO_DVLA = "sent to dvla"
JOB_STATUS_ERROR = "error"
JOB_STATUS_TYPES = [
    JOB_STATUS_PENDING,
    JOB_STATUS_IN_PROGRESS,
    JOB_STATUS_FINISHED,
    JOB_STATUS_SENDING_LIMITS_EXCEEDED,
    JOB_STATUS_SCHEDULED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_READY_TO_SEND,
    JOB_STATUS_SENT_TO_DVLA,
    JOB_STATUS_ERROR,
]
# all jobs for letters created via the api must have this filename
LETTER_API_FILENAME = "letter submitted via api"
LETTER_TEST_API_FILENAME = "test letter submitted via api"

# Miscellaneous
MOBILE_TYPE = "mobile"

# Guest lists
GUEST_LIST_RECIPIENT_TYPE = [MOBILE_TYPE, EMAIL_TYPE]

# Document download constants
DEFAULT_DOCUMENT_DOWNLOAD_RETENTION_PERIOD = "26 weeks"


# S3 tags
class Retention:
    KEY = "retention"

    ONE_WEEK = "ONE_WEEK"


# Redis cache keys
class CacheKeys:
    FT_BILLING_FOR_TODAY_UPDATED_AT_UTC_ISOFORMAT = "update_ft_billing_for_today:updated-at-utc-isoformat"
    NUMBER_OF_TIMES_OVER_SLOW_SMS_DELIVERY_THRESHOLD = "slow-sms-delivery:number-of-times-over-threshold"


# Admin API error codes
QR_CODE_TOO_LONG = "qr-code-too-long"

# Service Join Request statuses
JOIN_REQUEST_PENDING = "pending"
JOIN_REQUEST_APPROVED = "approved"
JOIN_REQUEST_REJECTED = "rejected"

REQUEST_STATUS_VALUES = [
    JOIN_REQUEST_APPROVED,
    JOIN_REQUEST_REJECTED,
    JOIN_REQUEST_PENDING,
]
