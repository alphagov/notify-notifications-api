import json

from flask import current_app, jsonify, request
from jsonschema import ValidationError as JsonSchemaValidationError
from notifications_utils.recipient_validation.errors import InvalidRecipientError
from sqlalchemy.exc import DataError
from sqlalchemy.orm.exc import NoResultFound

from app.authentication.auth import AuthError
from app.errors import InvalidRequest


class TooManyRequestsError(InvalidRequest):
    status_code = 429

    def __init__(self, limit_name, sending_limit):
        self.limit_name = limit_name
        self.sending_limit = sending_limit
        self.message = f"Exceeded send limits ({limit_name}: {sending_limit}) for today"


class RateLimitError(InvalidRequest):
    status_code = 429
    message_template = "Exceeded rate limit for key type {} of {} requests per {} seconds"

    def __init__(self, sending_limit, interval, key_type):
        # normal keys are spoken of as "live" in the documentation
        # so using this in the error messaging
        if key_type == "normal":
            key_type = "live"

        self.message = self.message_template.format(key_type.upper(), sending_limit, interval)


class BadRequestError(InvalidRequest):
    message = "An error occurred"

    def __init__(self, fields=None, message=None, status_code=400):
        self.status_code = status_code
        self.fields = fields or []
        self.message = message if message else self.message


class ValidationError(InvalidRequest):
    message = "Your notification has failed validation"

    def __init__(self, fields=None, message=None, status_code=400):
        self.status_code = status_code
        self.fields = fields or []
        self.message = message if message else self.message


class PDFNotReadyError(BadRequestError):
    def __init__(self):
        super().__init__(message="PDF not available yet, try again later", status_code=400)


class QrCodeTooLongError(ValidationError):
    message = "Cannot create a usable QR code - the link is too long"

    def __init__(self, fields=None, message=None, status_code=400, *, num_bytes, max_bytes, data):
        super().__init__(fields=fields, message=message, status_code=status_code)
        self.num_bytes = num_bytes
        self.max_bytes = max_bytes
        self.data = data

    def to_dict_v2(self):
        """
        Version 2 of the public api error response.
        """
        return {
            "status_code": self.status_code,
            "errors": [
                {
                    "error": "ValidationError",
                    "message": self.message,
                    "data": self.data,
                    "num_bytes": self.num_bytes,
                    "max_bytes": self.max_bytes,
                }
            ],
        }


def register_errors(blueprint):
    @blueprint.errorhandler(InvalidRecipientError)
    def invalid_format(error):
        current_app.logger.info(error)

        return (
            jsonify(
                status_code=400,
                errors=[{"error": error.__class__.__name__, "message": error.get_legacy_v2_api_error_message()}],
            ),
            400,
        )

    @blueprint.errorhandler(InvalidRequest)
    def invalid_data(error):
        current_app.logger.info(error)
        response = jsonify(error.to_dict_v2()), error.status_code
        return response

    @blueprint.errorhandler(JsonSchemaValidationError)
    def validation_error(error):
        current_app.logger.info(error)
        return jsonify(json.loads(error.message)), 400

    @blueprint.errorhandler(NoResultFound)
    @blueprint.errorhandler(DataError)
    def no_result_found(e):
        current_app.logger.info(e)
        return jsonify(status_code=404, errors=[{"error": e.__class__.__name__, "message": "No result found"}]), 404

    @blueprint.errorhandler(AuthError)
    def auth_error(error):
        current_app.logger.info("API AuthError, client: %s error: %s", request.headers.get("User-Agent"), error)
        return jsonify(error.to_dict_v2()), error.code

    @blueprint.errorhandler(Exception)
    def internal_server_error(error):
        current_app.logger.exception(error)
        return (
            jsonify(status_code=500, errors=[{"error": error.__class__.__name__, "message": "Internal server error"}]),
            500,
        )
