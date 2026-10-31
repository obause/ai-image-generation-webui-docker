#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${WORKSPACE_WEB_PASSWORD:-}" ]]; then
    echo "WORKSPACE_WEB_PASSWORD is required for browser tools" >&2
    exit 1
fi

mkdir -p /workspace/code-server/user-data /workspace/code-server/extensions \
    /workspace/filebrowser /workspace/images /workspace/projects /workspace/uploads
export PASSWORD="$WORKSPACE_WEB_PASSWORD"
export FILEBROWSER_ADMIN_PASSWORD="$WORKSPACE_WEB_PASSWORD"
unset WORKSPACE_WEB_PASSWORD

if [[ "${RP_CODE_SERVER_ENABLED:-false}" == "true" ]]; then
    /opt/code-server/bin/code-server --auth password \
        --bind-addr 0.0.0.0:8443 \
        --user-data-dir /workspace/code-server/user-data \
        --extensions-dir /workspace/code-server/extensions \
        /workspace >> /workspace/.rp-code-server.log 2>&1 &
fi
if [[ "${RP_FILEBROWSER_ENABLED:-false}" == "true" ]]; then
    /usr/local/bin/filebrowser -c /opt/runpod-bootstrap/filebrowser.yaml \
        >> /workspace/.rp-filebrowser.log 2>&1 &
fi
wait
