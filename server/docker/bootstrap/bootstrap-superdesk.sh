#!/usr/bin/env bash
#
# Entrypoint for the superdesk-bootstrap compose service.
#
# The work itself lives in bootstrap_superdesk.py - it is a Python program, so
# it is kept as one rather than as a stack of shell heredocs. This wrapper only
# exists to keep the entrypoint contract stable. It deliberately does not cd:
# the `manage.py` calls resolve against the server image's working directory.
set -euo pipefail

exec python3 /usr/local/bin/bootstrap_superdesk.py "$@"
