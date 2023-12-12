# Writing public APIs

_Most of the API endpoints in this repo are for internal use. These are all defined within top-level folders under `app/` and tend to have the structure `app/<feature>/rest.py`._

## Overview

Public APIs are intended for use by services and are all located under `app/v2/` to distinguish them from internal endpoints. Originally we did have a "v1" public API, where we tried to reuse / expose existing internal endpoints. The needs for public APIs are sufficiently different that we decided to separate them out. Any "v1" endpoints that remain are now purely internal and no longer exposed to services.

## New APIs

Here are some pointers for how we write public API endpoints.

### Each endpoint should be in its own file in a feature folder

Example: `app/v2/inbound_sms/get_inbound_sms.py`

This helps keep the file size manageable but does mean a bit more work to register each endpoint if we have many that are related. Note that internal endpoints are grouped differently: in large `rest.py` files.

### Each group of endpoints should have an `__init__.py` file

Example:

```
from flask import Blueprint

from app.v2.errors import register_errors

v2_notification_blueprint = Blueprint("v2_notifications", __name__, url_prefix='/v2/notifications')

register_errors(v2_notification_blueprint)
```

Note that the error handling setup by `register_errors` (defined in [`app/v2/errors.py`](../app/v2/errors.py)) for public API endpoints is different to that for internal endpoints (defined in [`app/errors.py`](../app/errors.py)).

### Each endpoint should have an adapter in each API client

Example: [Ruby Client adapter to get template by ID](https://github.com/alphagov/notifications-ruby-client/blob/d82c85452753b97e8f0d0308c2262023d75d0412/lib/notifications/client.rb#L110-L115).

All our clients should fully support all of our public APIs.

Each adapter should be documented in each client ([example](https://github.com/alphagov/notifications-ruby-client/blob/d82c85452753b97e8f0d0308c2262023d75d0412/DOCUMENTATION.md#get-a-template-by-id)). We should also document each public API endpoint in our generic API docs ([example](https://github.com/alphagov/notifications-tech-docs/blob/2700f1164f9d644c87e4c72ad7223952288e8a83/source/documentation/_api_docs.md#send-a-text-message)). Note that internal endpoints are not documented anywhere.

### Each endpoint should specify the authentication it requires

This is done as part of registering the blueprint in `app/__init__.py` e.g.

```
v2_notification_blueprint.before_request(requires_auth)
application.register_blueprint(v2_notification_blueprint)
```
