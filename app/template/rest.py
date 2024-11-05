import base64
from io import BytesIO

import botocore
from flask import Blueprint, current_app, jsonify, request
from flask.ctx import has_request_context
from notifications_utils import SMS_CHAR_COUNT_LIMIT
from notifications_utils.pdf import extract_page_from_pdf
from notifications_utils.template import (
    LetterPreviewTemplate,
    SMSMessageTemplate,
)
from pypdf.errors import PdfReadError
from requests import post as requests_post
from sqlalchemy.orm.exc import NoResultFound

from app.constants import LETTER_TYPE, QR_CODE_TOO_LONG, SECOND_CLASS, SMS_TYPE
from app.dao.notifications_dao import get_notification_by_id
from app.dao.services_dao import dao_fetch_service_by_id
from app.dao.template_folder_dao import (
    dao_get_template_folder_by_id_and_service_id,
)
from app.dao.templates_dao import (
    dao_create_template,
    dao_get_all_templates_for_service,
    dao_get_template_by_id,
    dao_get_template_by_id_and_service_id,
    dao_get_template_versions,
    dao_redact_template,
    dao_update_template,
    get_precompiled_letter_template,
)
from app.errors import InvalidRequest, register_errors
from app.letters.utils import get_letter_pdf_and_metadata
from app.models import Template
from app.notifications.validators import check_service_letter_contact_id
from app.schema_validation import validate
from app.schemas import (
    template_history_schema,
    template_schema,
    template_schema_no_detail,
)
from app.template.template_schemas import (
    post_create_template_schema,
    post_update_template_schema,
)
from app.utils import get_public_notify_type_text

template_blueprint = Blueprint("template", __name__, url_prefix="/service/<uuid:service_id>/template")

register_errors(template_blueprint)


def _content_count_greater_than_limit(content, template_type):
    if template_type == SMS_TYPE:
        template = SMSMessageTemplate({"content": content, "template_type": SMS_TYPE})
        return template.is_message_too_long()
    return False


def _qr_code_too_long(subject: str, content: str):
    template = LetterPreviewTemplate({"subject": subject, "content": content, "template_type": "letter"})
    return template.has_qr_code_with_too_much_data()


def validate_parent_folder(template_json):
    if template_json.get("parent_folder_id"):
        try:
            return dao_get_template_folder_by_id_and_service_id(
                template_folder_id=template_json.pop("parent_folder_id"), service_id=template_json["service"]
            )
        except NoResultFound as e:
            raise InvalidRequest("parent_folder_id not found", status_code=400) from e
    else:
        return None


@template_blueprint.route("", methods=["POST"])
def create_template(service_id):
    fetched_service = dao_fetch_service_by_id(service_id=service_id)
    template_json = validate(request.get_json(), post_create_template_schema)
    folder = validate_parent_folder(template_json=template_json)
    new_template = Template.from_json(template_json, folder)

    if not fetched_service.has_permission(new_template.template_type):
        message = f"Creating {get_public_notify_type_text(new_template.template_type)} templates is not allowed"
        errors = {"template_type": [message]}
        raise InvalidRequest(errors, 403)

    if not new_template.postage and new_template.template_type == LETTER_TYPE:
        new_template.postage = SECOND_CLASS

    new_template.service = fetched_service

    over_limit = _content_count_greater_than_limit(new_template.content, new_template.template_type)
    if over_limit:
        message = f"Content has a character count greater than the limit of {SMS_CHAR_COUNT_LIMIT}"
        errors = {"content": [message]}
        raise InvalidRequest(errors, status_code=400)

    if new_template.template_type == LETTER_TYPE and _qr_code_too_long(
        subject=new_template.subject, content=new_template.content
    ):
        raise InvalidRequest({"content": [QR_CODE_TOO_LONG]}, status_code=400)

    check_service_letter_contact_id(service_id, new_template.reply_to, new_template.template_type)

    dao_create_template(new_template)

    return jsonify(data=template_schema.dump(new_template)), 201


@template_blueprint.route("/<uuid:template_id>", methods=["POST"])
def update_template(service_id, template_id):
    fetched_template = dao_get_template_by_id_and_service_id(template_id=template_id, service_id=service_id)

    if not fetched_template.service.has_permission(fetched_template.template_type):
        message = f"Updating {get_public_notify_type_text(fetched_template.template_type)} templates is not allowed"
        errors = {"template_type": [message]}

        raise InvalidRequest(errors, 403)

    data = request.get_json()
    validate(data, post_update_template_schema)

    # if redacting, don't update anything else
    if data.get("redact_personalisation") is True:
        return redact_template(fetched_template, data)

    if "reply_to" in data:
        check_service_letter_contact_id(service_id, data.get("reply_to"), fetched_template.template_type)
        fetched_template.service_letter_contact_id = data.get("reply_to")
        dao_update_template(fetched_template)
        return jsonify(data=template_schema.dump(fetched_template)), 200

    current_data = template_schema.dump(fetched_template)
    updated_template = current_data | data

    # Check if there is a change to make.
    if current_data == updated_template:
        return jsonify(data=updated_template), 200

    over_limit = _content_count_greater_than_limit(updated_template["content"], fetched_template.template_type)
    if over_limit:
        message = f"Content has a character count greater than the limit of {SMS_CHAR_COUNT_LIMIT}"
        errors = {"content": [message]}
        raise InvalidRequest(errors, status_code=400)

    if fetched_template.template_type == LETTER_TYPE and _qr_code_too_long(
        subject=updated_template["subject"], content=updated_template["content"]
    ):
        raise InvalidRequest({"content": [QR_CODE_TOO_LONG]}, status_code=400)

    update_dict = template_schema.load(updated_template)
    if update_dict.archived:
        update_dict.folder = None
    dao_update_template(update_dict)
    return jsonify(data=template_schema.dump(update_dict)), 200


@template_blueprint.route("/precompiled", methods=["GET"])
def get_precompiled_template_for_service(service_id):
    template = get_precompiled_letter_template(service_id)
    template_dict = template_schema.dump(template)

    return jsonify(template_dict), 200


@template_blueprint.route("", methods=["GET"])
def get_all_templates_for_service(service_id):
    templates = dao_get_all_templates_for_service(service_id=service_id)
    if str(request.args.get("detailed", True)) == "True":
        data = template_schema.dump(templates, many=True)
    else:
        data = template_schema_no_detail.dump(templates, many=True)
    return jsonify(data=data)


@template_blueprint.route("/<uuid:template_id>", methods=["GET"])
def get_template_by_id_and_service_id(service_id, template_id):
    fetched_template = dao_get_template_by_id_and_service_id(template_id=template_id, service_id=service_id)
    data = template_schema.dump(fetched_template)
    return jsonify(data=data)


@template_blueprint.route("/<uuid:template_id>/preview", methods=["GET"])
def preview_template_by_id_and_service_id(service_id, template_id):
    fetched_template = dao_get_template_by_id_and_service_id(template_id=template_id, service_id=service_id)
    data = template_schema.dump(fetched_template)
    template_object = fetched_template._as_utils_template_with_personalisation(request.args.to_dict())

    if template_object.missing_data:
        raise InvalidRequest(
            {"template": ["Missing personalisation: {}".format(", ".join(template_object.missing_data))]},
            status_code=400,
        )

    data["subject"] = template_object.subject
    data["content"] = template_object.content_with_placeholders_filled_in

    return jsonify(data)


@template_blueprint.route("/<uuid:template_id>/version/<int:version>")
def get_template_version(service_id, template_id, version):
    data = template_history_schema.dump(
        dao_get_template_by_id_and_service_id(template_id=template_id, service_id=service_id, version=version)
    )
    return jsonify(data=data)


@template_blueprint.route("/<uuid:template_id>/versions")
def get_template_versions(service_id, template_id):
    data = template_history_schema.dump(
        dao_get_template_versions(service_id=service_id, template_id=template_id), many=True
    )
    return jsonify(data=data)


def redact_template(template, data):
    # we also don't need to check what was passed in redact_personalisation - its presence in the dict is enough.
    if "created_by" not in data:
        message = "Field is required"
        errors = {"created_by": [message]}
        raise InvalidRequest(errors, status_code=400)

    # if it's already redacted, then just return 200 straight away.
    if not template.redact_personalisation:
        dao_redact_template(template, data["created_by"])
    return "null", 200


@template_blueprint.route("/preview/<uuid:notification_id>/<file_type>", methods=["GET"])
def preview_letter_template_by_notification_id(service_id, notification_id, file_type):
    if file_type not in ("pdf", "png"):
        raise InvalidRequest({"content": ["file_type must be pdf or png"]}, status_code=400)

    page = request.args.get("page")

    notification = get_notification_by_id(notification_id)
    template = dao_get_template_by_id(notification.template_id, notification.template_version)
    metadata = {}

    if template.is_precompiled_letter:
        try:
            pdf_file, metadata = get_letter_pdf_and_metadata(notification)

        except botocore.exceptions.ClientError as e:
            raise InvalidRequest(
                (
                    f"Error extracting requested page from PDF file for notification_id {notification_id} "
                    f"type {type(e)} {e}"
                ),
                status_code=500,
            ) from e

        page_number = page if page else "1"
        content = base64.b64encode(pdf_file).decode("utf-8")
        content_outside_printable_area = metadata.get("message") == "content-outside-printable-area"
        page_is_in_invalid_pages = page_number in metadata.get("invalid_pages", "[]")

        if content_outside_printable_area and (file_type == "pdf" or page_is_in_invalid_pages):
            path = f"/precompiled/overlay.{file_type}"
            query_string = f"?page_number={page_number}" if file_type == "png" else ""
            content = pdf_file
        elif file_type == "png":
            query_string = "?hide_notify=true" if page_number == "1" else ""
            path = "/precompiled-preview.png"
        else:
            path = None

        if file_type == "png":
            try:
                pdf_page = extract_page_from_pdf(BytesIO(pdf_file), int(page_number) - 1)
                if content_outside_printable_area and page_is_in_invalid_pages:
                    content = pdf_page
                else:
                    content = base64.b64encode(pdf_page).decode("utf-8")
            except PdfReadError as e:
                raise InvalidRequest(
                    (
                        f"Error extracting requested page from PDF file for notification_id {notification_id} "
                        f"type {type(e)} {e}"
                    ),
                    status_code=500,
                ) from e

        if path:
            url = current_app.config["TEMPLATE_PREVIEW_API_HOST"] + path + query_string
            response_content = _get_png_preview_or_overlaid_pdf(url, content, notification.id, json=False)
        else:
            response_content = content
    else:
        template_for_letter_print = {
            "id": str(notification.template_id),
            "subject": template.subject,
            "content": template.content,
            "version": str(template.version),
            "template_type": template.template_type,
        }

        service = dao_fetch_service_by_id(service_id)
        letter_logo_filename = service.letter_branding and service.letter_branding.filename
        data = {
            "letter_contact_block": notification.reply_to_text,
            "template": template_for_letter_print,
            "values": notification.personalisation,
            "date": notification.created_at.isoformat(),
            "filename": letter_logo_filename,
        }

        url = "{}/preview.{}{}".format(
            current_app.config["TEMPLATE_PREVIEW_API_HOST"], file_type, f"?page={page}" if page else ""
        )
        response_content = _get_png_preview_or_overlaid_pdf(url, data, notification.id, json=True)

    return jsonify({"content": response_content, "metadata": metadata})


def _get_png_preview_or_overlaid_pdf(url, data, notification_id, json=True):
    headers = {"Authorization": "Token {}".format(current_app.config["TEMPLATE_PREVIEW_API_KEY"])}
    if has_request_context() and hasattr(request, "get_onwards_request_headers"):
        headers.update(request.get_onwards_request_headers())

    if json:
        resp = requests_post(url, json=data, headers=headers)
    else:
        resp = requests_post(url, data=data, headers=headers)

    if resp.status_code != 200:
        raise InvalidRequest(
            f"Error generating preview letter for {notification_id} Status code: {resp.status_code} {resp.content}",
            status_code=500,
        )

    return base64.b64encode(resp.content).decode("utf-8")
