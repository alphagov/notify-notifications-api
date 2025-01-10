from datetime import datetime

from flask import Blueprint, abort, current_app, jsonify, request

from app.dao.jobs_dao import get_possibly_cached_notification_outcomes_for_job
from app.dao.uploads_dao import (
    dao_get_uploaded_letters_by_print_date,
    dao_get_uploads_by_service_id,
)
from app.errors import register_errors
from app.schemas import notification_with_template_schema
from app.utils import pagination_links

upload_blueprint = Blueprint("upload", __name__, url_prefix="/service/<uuid:service_id>/upload")

register_errors(upload_blueprint)


@upload_blueprint.route("", methods=["GET"])
def get_uploads_by_service(service_id):
    return jsonify(
        **get_paginated_uploads(
            service_id, request.args.get("limit_days", type=int), request.args.get("page", type=int)
        )
    )


def get_paginated_uploads(service_id, limit_days, page):
    pagination = dao_get_uploads_by_service_id(
        service_id, limit_days=limit_days, page=page, page_size=current_app.config["PAGE_SIZE"]
    )
    uploads = pagination.items
    data = []
    for upload in uploads:
        upload_dict = {
            "id": upload.id,
            "original_file_name": upload.original_file_name,
            "notification_count": upload.notification_count,
            "created_at": (
                upload.scheduled_for.strftime("%Y-%m-%d %H:%M:%S")
                if upload.scheduled_for
                else upload.created_at.strftime("%Y-%m-%d %H:%M:%S")
            ),
            "upload_type": upload.upload_type,
            "template_type": upload.template_type,
            "recipient": upload.recipient,
        }
        if upload.upload_type == "job":
            upload_dict["statistics"] = get_possibly_cached_notification_outcomes_for_job(
                upload.id, upload.notification_count, upload.processing_started
            )
        else:
            upload_dict["statistics"] = []
        data.append(upload_dict)

    return {
        "data": data,
        "page_size": pagination.per_page,
        "total": pagination.total,
        "links": pagination_links(pagination, ".get_uploads_by_service", service_id=service_id),
    }


@upload_blueprint.route("/uploaded-letters/<letter_print_date>", methods=["GET"])
def get_uploaded_letter_by_service_and_print_day(service_id, letter_print_date):
    try:
        letter_print_datetime = datetime.strptime(letter_print_date, "%Y-%m-%d")
    except ValueError:
        abort(400)
    pagination = dao_get_uploaded_letters_by_print_date(
        service_id,
        letter_print_date=letter_print_datetime,
        page=request.args.get("page", type=int),
        page_size=current_app.config["PAGE_SIZE"],
    )
    return jsonify(
        {
            "notifications": notification_with_template_schema.dump(
                pagination.items,
                many=True,
            ),
            "page_size": pagination.per_page,
            "total": pagination.total,
            "links": pagination_links(
                pagination,
                ".get_uploaded_letter_by_service_and_print_day",
                service_id=service_id,
                letter_print_date=letter_print_date,
            ),
        }
    )
