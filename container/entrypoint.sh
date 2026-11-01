#!/usr/bin/env bash
set -euo pipefail

mkdir -p /workspace
chown ubuntu:ubuntu /workspace
rm -f /workspace/.rp-bootstrap-ready
gosu ubuntu touch /workspace/.rp-bootstrap.log /workspace/.rp-sync.log \
    /workspace/.rp-comfy.log /workspace/.rp-swarm.log \
    /workspace/.rp-code-server.log /workspace/.rp-filebrowser.log

# RunPod supplies PUBLIC_KEY when startSsh is enabled. Start the control
# channel first so a failed restore can be inspected without starting apps.
if [[ -n "${PUBLIC_KEY:-}" ]]; then
    mkdir -p /run/sshd /root/.ssh
    printf '%s\n' "$PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 700 /root/.ssh
    chmod 600 /root/.ssh/authorized_keys
    ssh-keygen -A >/dev/null 2>&1
    /usr/sbin/sshd
    unset PUBLIC_KEY
fi

if ! gosu ubuntu python3 /opt/runpod-bootstrap/worker.py bootstrap 2>&1 \
    | tee -a /workspace/.rp-bootstrap.log; then
    echo "Required B2 bootstrap failed; Pod held for rp logs and rp ssh" \
        | tee -a /workspace/.rp-bootstrap.log >&2
    exec sleep infinity
fi
gosu ubuntu mkdir -p /workspace/models/invokeai /workspace/invokeai/outputs \
    /workspace/output/comfyui /workspace/comfy-user /workspace/swarmui-data \
    /workspace/images /workspace/projects /workspace/uploads /workspace/code-server \
    /workspace/filebrowser

if [[ -z "${WORKSPACE_WEB_PASSWORD:-}" || ${#WORKSPACE_WEB_PASSWORD} -lt 12 ]]; then
    echo "WORKSPACE_WEB_PASSWORD must have at least 12 characters" >&2
    exit 1
fi
export RP_CODE_SERVER_ENABLED="$(python3 -c 'import json,os; print(str(json.loads(os.environ["RP_CONFIG_JSON"])["application"].get("code_server", False)).lower())')"
export RP_FILEBROWSER_ENABLED="$(python3 -c 'import json,os; print(str(json.loads(os.environ["RP_CONFIG_JSON"])["application"].get("filebrowser", False)).lower())')"

# The public proxy ports for generation apps all require HTTP Basic auth.
# The hash is ephemeral and never enters Docker layers or command arguments.
umask 077
printf 'workspace:%s\n' "$(printf '%s\n' "$WORKSPACE_WEB_PASSWORD" | openssl passwd -6 -stdin)" > /run/rp-web.htpasswd
chgrp www-data /run/rp-web.htpasswd
chmod 640 /run/rp-web.htpasswd
nginx -c /opt/runpod-bootstrap/nginx.conf -g 'daemon off;' >> /workspace/.rp-nginx.log 2>&1 &

# InvokeAI accepts JSON for this settings field. Build it from an optional
# Pod secret in-process; the token never enters Docker layers, argv, or logs.
if [[ -n "${CIVITAI_API_TOKEN:-}" ]]; then
    export INVOKEAI_REMOTE_API_TOKENS="$(python3 - <<'PY'
import json, os
print(json.dumps([{"url_regex": "civitai\\.com", "token": os.environ["CIVITAI_API_TOKEN"]}]))
PY
)"
    unset CIVITAI_API_TOKEN
fi

gosu ubuntu python3 /opt/runpod-bootstrap/worker.py loop >> /workspace/.rp-sync.log 2>&1 &
gosu ubuntu /opt/runpod-bootstrap/start-comfy.sh >> /workspace/.rp-comfy.log 2>&1 &
if [[ -n "${RP_CODE_SERVER_ENABLED:-}${RP_FILEBROWSER_ENABLED:-}" ]]; then
    gosu ubuntu /opt/runpod-bootstrap/start-web-tools.sh &
fi
unset WORKSPACE_WEB_PASSWORD
if [[ "$(python3 -c 'import json,os; print(json.loads(os.environ["RP_CONFIG_JSON"])["application"]["swarmui"])')" == "True" ]]; then
    gosu ubuntu /opt/runpod-bootstrap/start-swarm.sh >> /workspace/.rp-swarm.log 2>&1 &
fi

exec /opt/invokeai/docker-entrypoint.sh "$@"
