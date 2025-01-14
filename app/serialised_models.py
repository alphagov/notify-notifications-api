from collections import defaultdict
from datetime import datetime
from functools import partial
from threading import RLock
from typing import Any

import cachetools
from notifications_utils.clients.redis import RequestCache
from notifications_utils.serialised_model import (
    SerialisedModel,
    SerialisedModelCollection,
)
from werkzeug.utils import cached_property

from app import redis_store
from app.dao.api_key_dao import get_model_api_keys
from app.dao.services_dao import dao_fetch_service_by_id

caches = defaultdict(partial(cachetools.TTLCache, maxsize=1024, ttl=2))
locks = defaultdict(RLock)
redis_cache = RequestCache(redis_store)


def memory_cache(func):
    @cachetools.cached(
        cache=caches[func.__qualname__],
        lock=locks[func.__qualname__],
        key=ignore_first_argument_cache_key,
    )
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


def ignore_first_argument_cache_key(cls, *args, **kwargs):
    return cachetools.keys.hashkey(*args, **kwargs)


class SerialisedTemplate(SerialisedModel):
    archived: bool
    content: str
    id: Any
    postage: str
    process_type: str
    reply_to_text: str
    subject: str
    template_type: str
    version: int
    has_unsubscribe_link: bool

    @classmethod
    @memory_cache
    def from_id_and_service_id(cls, template_id, service_id, version=None):
        return cls(cls.get_dict(template_id, service_id, version)["data"])

    @staticmethod
    @redis_cache.set("service-{service_id}-template-{template_id}-version-{version}")
    def get_dict(template_id, service_id, version):
        from app.dao import templates_dao
        from app.schemas import template_schema

        fetched_template = templates_dao.dao_get_template_by_id_and_service_id(
            template_id=template_id,
            service_id=service_id,
            version=version,
        )

        template_dict = template_schema.dump(fetched_template)

        return {"data": template_dict}


class SerialisedService(SerialisedModel):
    id: Any
    name: str
    active: bool
    contact_link: str
    custom_email_sender_name: str
    email_sender_local_part: str
    email_message_limit: int
    letter_message_limit: int
    sms_message_limit: int
    permissions: Any
    rate_limit: int
    restricted: bool
    prefix_sms: bool
    email_branding: Any

    @classmethod
    @memory_cache
    def from_id(cls, service_id):
        return cls(cls.get_dict(service_id)["data"])

    @staticmethod
    @redis_cache.set("service-{service_id}")
    def get_dict(service_id):
        from app.schemas import service_schema

        service_dict = service_schema.dump(dao_fetch_service_by_id(service_id))

        return {"data": service_dict}

    @cached_property
    def api_keys(self):
        return SerialisedAPIKeyCollection.from_service_id(self.id)

    def has_permission(self, permission):
        return permission in self.permissions


class SerialisedAPIKey(SerialisedModel):
    id: Any
    secret: str
    expiry_date: datetime
    key_type: str


class SerialisedAPIKeyCollection(SerialisedModelCollection):
    model = SerialisedAPIKey

    @classmethod
    @memory_cache
    def from_service_id(cls, service_id):
        keys = [
            {k: getattr(key, k) for k in SerialisedAPIKey.__annotations__} for key in get_model_api_keys(service_id)
        ]
        return cls(keys)
