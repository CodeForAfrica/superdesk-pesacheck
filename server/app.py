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

    # Stop apply_async blocking on subtasks it just queued, which deadlocked
    # auto-publish against its own enqueue_published cascade
    # (see pesacheck/celery_eager_patch.py). First, so it is in place before
    # anything dispatches a task.
    from pesacheck import celery_eager_patch

    celery_eager_patch.apply()

    # Give image downloads HTTP keep-alive; core builds a fresh session, and so a
    # fresh TLS handshake, for every image (see pesacheck/image_fetch_patch.py).
    from pesacheck import image_fetch_patch

    image_fetch_patch.apply()

    # Silence superdesk-core's per-EXIF-tag stdout prints, which Celery re-emits
    # as WARNING logs and which dominate ingest log volume
    # (see pesacheck/exif_log_patch.py).
    from pesacheck import exif_log_patch

    exif_log_patch.apply()

    # Harden AmazonMediaStorage against the aioboto3 S3 get_object hang that freezes
    # ghost ingest (see pesacheck/media_patch.py). Must run before superdesk_app builds
    # the media-storage singleton.
    from pesacheck import media_patch

    media_patch.apply()

    # Fix the stale-etag 412 that orphans publish-queue items in IN_PROGRESS after a
    # successful transmit (see pesacheck/publish_patch.py).
    from pesacheck import publish_patch

    publish_patch.apply()

    # On publish, promote feature-media renditions out of the self-deleting temp/
    # folder to their permanent S3 location (see pesacheck/promote_media_patch.py).
    from pesacheck import promote_media_patch

    promote_media_patch.apply()

    # Load the vocabularies collection from the tracked per-vocabulary tree under
    # data/vocabularies/ (deny-listing keywords), instead of core's single-file
    # fallback (see pesacheck/content_config_patch.py).
    from pesacheck import content_config_patch

    content_config_patch.apply()

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
