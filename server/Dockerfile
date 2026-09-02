FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
python3 python3-dev python3-pip python3-venv git gcc curl \
# lxml
libxml2-dev libxslt-dev \
# xmlsec
pkg-config libxml2-dev libxmlsec1-dev libxmlsec1-openssl \
# PIL
libjpeg-dev zlib1g-dev \
# magic
libmagic-dev \
# reportlab
fonts-dejavu \
# cleanup
&& rm -rf /var/lib/apt/lists/*

# setup venv
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# install server
WORKDIR /opt/superdesk/
COPY requirements.txt .
RUN python3 -m pip install -Ur requirements.txt

ENV PYTHONUNBUFFERED=1
ENV C_FORCE_ROOT=False
ENV CELERYBEAT_SCHEDULE_FILENAME=/tmp/celerybeatschedule.db
ENV TZ=Europe/Prague

COPY . .

# Bake the bootstrap/seed entrypoints so the image is self-contained (no host
# bind mounts): the ECS RunTask one-shots and the local bootstrap compose
# services both run these baked copies. bootstrap-superdesk.sh execs
# bootstrap_superdesk.py by absolute path, so the .py lives in /usr/local/bin too.
COPY docker/bootstrap/bootstrap-superdesk.sh    /usr/local/bin/bootstrap-superdesk.sh
COPY docker/bootstrap/bootstrap_superdesk.py    /usr/local/bin/bootstrap_superdesk.py
COPY docker/bootstrap/bootstrap-ghost-ingest.sh /usr/local/bin/bootstrap-ghost-ingest.sh
RUN chmod +x /usr/local/bin/bootstrap-superdesk.sh /usr/local/bin/bootstrap-ghost-ingest.sh

ENTRYPOINT [ "/opt/superdesk/docker/start.sh" ]
CMD ["honcho", "start"]
