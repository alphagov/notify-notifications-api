get_inbound_sms_for_service_schema = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "description": "schema for parameters allowed when searching for to field=",
    "type": "object",
    "properties": {
        "phone_number": {"type": "string"},
    },
}

remove_capability_schema = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "description": "Schema for validating the input to remove inbound SMS capability",
    "type": "object",
    "properties": {
        "archive": {
            "type": "boolean",
            "description": "Indicates whether to archive the inbound number or release it.",
        },
    },
    "required": ["archive"],  # Ensure 'archive' is mandatory
    "additionalProperties": False,  # Disallow extra fields
}
