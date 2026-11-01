#!/usr/bin/env bash
# Local no-GPU startup test. Uses a fake rclone and never contacts B2 or RunPod.
set -euo pipefail

image="${1:-runpod-workspace:local}"
temporary="$(mktemp -d)"
container="rp-smoke-$$"
cleanup() {
    docker rm -f "$container" >/dev/null 2>&1 || true
    rm -rf "$temporary"
}
trap cleanup EXIT

cat > "$temporary/rclone" <<'SH'
#!/bin/sh
case "$1" in
    version) echo 'rclone v1.75.2 (smoke fake)' ;;
    lsf) exit 0 ;;
    copy)
        case "$2" in
            */images)
                while [ ! -f /workspace/.rp-smoke-release-images ]; do sleep 1; done
                mkdir -p "$3"
                printf 'fake history\n' > "$3/smoke-history.txt"
                exit 0 ;;
        esac
        mkdir -p "$3/checkpoints"
        printf 'fake model\n' > "$3/checkpoints/smoke.safetensors"
        exit 0 ;;
    *) exit 0 ;;
esac
SH
chmod 755 "$temporary/rclone"

config='{"storage":{"bucket":"fake-bucket","transfers":1,"fast_list":false,"restore":[{"name":"models","remote":"models","local":"/workspace/models","mode":"copy","required":true},{"name":"images","remote":"images","local":"/workspace/images","mode":"copy","required":false,"background":true}],"backup":[{"name":"models","remote":"models","local":"/workspace/models","mode":"copy","interval_seconds":300},{"name":"images","remote":"images","local":"/workspace/images","mode":"copy","interval_seconds":300}]},"application":{"swarmui":false,"code_server":true,"filebrowser":true,"shared_models_path":"/workspace/models"}}'

docker run -d --rm --init --platform linux/amd64 --name "$container" \
    -v "$temporary/rclone:/opt/runpod-bootstrap/bin/rclone:ro" \
    -e B2_APPLICATION_KEY_ID=fake-id -e B2_APPLICATION_KEY=fake-key \
    -e WORKSPACE_WEB_PASSWORD=dummy-smoke-password-123 \
    -e RP_DEADLINE_AT=2099-01-01T00:00:00+00:00 \
    -e "RP_CONFIG_JSON=$config" \
    "$image" sleep 120 >/dev/null

for _ in {1..60}; do
    if docker exec "$container" test -f /workspace/.rp-bootstrap-ready 2>/dev/null; then
        break
    fi
    sleep 1
done
docker exec "$container" test -f /workspace/.rp-bootstrap-ready
docker exec "$container" test -f /workspace/models/checkpoints/smoke.safetensors
if docker exec "$container" test -f /workspace/images/smoke-history.txt 2>/dev/null; then
    echo "Background image restore finished before readiness check" >&2
    exit 1
fi
docker exec "$container" touch /workspace/.rp-smoke-release-images
docker exec -e CIVITAI_API_TOKEN=fake-smoke-token "$container" \
    python3 /opt/runpod-bootstrap/civitai_tokens.py | python3 -c '
import json, re, sys
entry = json.load(sys.stdin)[0]
assert entry["token"] == "fake-smoke-token"
assert re.search(entry["url_regex"], "https://civitai.red/api/download/models/1")
'
docker exec -e HF_TOKEN=fake-hf-token "$container" \
    python3 /opt/runpod-bootstrap/civitai_tokens.py | python3 -c '
import json, re, sys
entry = json.load(sys.stdin)[0]
assert entry["token"] == "fake-hf-token"
assert re.search(entry["url_regex"], "https://huggingface.co/org/model")
'
docker exec -i "$container" python3 - <<'PY'
import urllib.error
import urllib.request
import time
from pathlib import Path

assert "auth_basic" not in Path("/opt/runpod-bootstrap/nginx.conf").read_text()
try:
    response = urllib.request.urlopen("http://127.0.0.1:8188/", timeout=5)
    assert response.status == 200, response.status
except urllib.error.HTTPError as error:
    assert error.code == 502, error.code  # ComfyUI may still be starting without a GPU.

for _ in range(30):
    try:
        response = urllib.request.urlopen("http://127.0.0.1:8081/", timeout=5)
        assert response.status == 200, response.status
        break
    except urllib.error.URLError:
        time.sleep(1)
else:
    raise AssertionError("FileBrowser did not start")
PY
docker exec -e RP_CONFIG_JSON= -e B2_APPLICATION_KEY_ID= -e B2_APPLICATION_KEY= \
    "$container" /opt/venv/bin/python /opt/runpod-bootstrap/worker.py backup
docker exec "$container" test -f /workspace/images/smoke-history.txt
echo "Container bootstrap, open generation proxy, FileBrowser, and remote backup smoke test passed"
