#!/bin/bash
set -e

cd /opt/superdesk/client/dist

# Regenerate the browser-facing runtime config from the environment supplied by
# the ECS task definition, so the image itself is environment-agnostic.
#
# The old approach sed-replaced the literal strings "http://localhost:5000/api"
# and "ws://localhost:5100" inside app*.js. That silently did nothing here: the
# build defaults the API to localhost:8080 (not 5000), and the value the browser
# actually reads lives in config.js (window.superdeskConfig), which was never
# touched. The result was a production page trying to reach the developer's own
# machine (localhost) for /api/client_config, hence the flashing/blank UI.
#
# index.html loads a *fingerprinted* config file (e.g. config.ec23ae24.js), so we
# discover the exact filename it references and overwrite that, falling back to
# config.js.
CONFIG_FILE=$(grep -oE 'config(\.[a-z0-9]+)?\.js' index.html | head -1)
CONFIG_FILE=${CONFIG_FILE:-config.js}

# Defaults keep local `docker run` usable; ECS overrides all of these.
: "${SUPERDESK_URL:=http://localhost:8080/api}"
: "${SUPERDESK_WS_URL:=ws://localhost:8080/ws}"
: "${SUPERDESK_PUBLISHER_PROTOCOL:=https}"
: "${SUPERDESK_PUBLISHER_DOMAIN:=}"
: "${SUPERDESK_PUBLISHER_TENANT:=}"
: "${SUPERDESK_PUBLISHER_WS_PROTOCOL:=wss}"
: "${SUPERDESK_PUBLISHER_WS_DOMAIN:=}"
: "${SUPERDESK_PUBLISHER_WS_PORT:=443}"
: "${IFRAMELY_KEY:=}"

cat > "$CONFIG_FILE" <<EOF
window.superdeskConfig = {
    server: {
        url: '${SUPERDESK_URL}',
        ws: '${SUPERDESK_WS_URL}'
    },
    publisher: {
        protocol: '${SUPERDESK_PUBLISHER_PROTOCOL}',
        domain: '${SUPERDESK_PUBLISHER_DOMAIN}',
        tenant: '${SUPERDESK_PUBLISHER_TENANT}',
        wsProtocol: '${SUPERDESK_PUBLISHER_WS_PROTOCOL}',
        wsDomain: '${SUPERDESK_PUBLISHER_WS_DOMAIN}',
        wsPort: '${SUPERDESK_PUBLISHER_WS_PORT}'
    },
    iframely: { key: '${IFRAMELY_KEY}' }
};
EOF

echo "Wrote runtime client config to ${CONFIG_FILE} (SUPERDESK_URL=${SUPERDESK_URL})"

exec "$@"
