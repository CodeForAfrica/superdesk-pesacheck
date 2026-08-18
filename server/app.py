#!/usr/bin/env python
#
# This file is part of Superdesk.
#
# Copyright 2013, 2014, 2015 Sourcefabric z.u. and contributors.
#
# For the full copyright and license information, please see the
# AUTHORS and LICENSE files distributed with this source code, or
# at https://www.sourcefabric.org/superdesk/license

import os

import settings
from superdesk.factory import get_app as superdesk_app


def get_app(config=None):
    """App factory.

    :param config: configuration that can override config from `settings.py`
    :return: a new SuperdeskEve app instance
    """
    if config is None:
        config = {}

    config["APP_ABSPATH"] = os.path.abspath(os.path.dirname(__file__))

    for key in dir(settings):
        if key.isupper():
            config.setdefault(key, getattr(settings, key))

    # Harden AmazonMediaStorage against the aioboto3 S3 get_object hang that freezes
    # ghost ingest (see pesacheck/media_patch.py). Must run before superdesk_app builds
    # the media-storage singleton.
    from pesacheck import media_patch

    media_patch.apply()

    # Fix the stale-etag 412 that orphans publish-queue items in IN_PROGRESS after a
    # successful transmit (see pesacheck/publish_patch.py).
    from pesacheck import publish_patch

    publish_patch.apply()

    app = superdesk_app(config)
    return app


# required so quart can instantiate it from commands terminal
create_app = get_app


if __name__ == "__main__":
    debug = True
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "5000"))
    app = get_app()
    app.run(host=host, port=port, debug=debug, use_reloader=debug)
