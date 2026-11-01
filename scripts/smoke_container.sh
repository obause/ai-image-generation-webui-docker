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
        mkdir -p "$3/checkpoints"
        printf 'fake model\n' > "$3/checkpoints/smoke.safetensors"
        exit 0 ;;
    *) exit 0 ;;
esac
SH
chmod 755 "$temporary/rclone"

config='{"storage":{"bucket":"fake-bucket","transfers":1,"fast_list":false,"restore":[{"name":"models","remote":"models","local":"/workspace/models","mode":"copy","required":true}],"backup":[{"name":"models","remote":"models","local":"/workspace/models","mode":"copy","interval_seconds":300}]},"application":{"swarmui":false,"code_server":true,"filebrowser":true,"shared_models_path":"/workspace/models"}}'

docker run -d --rm --platform linux/amd64 --name "$container" \
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
docker exec -i "$container" python3 - <<'PY'
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8188/", timeout=5)
except urllib.error.HTTPError as error:
    assert error.code == 401, error.code
else:
    raise AssertionError("generation port is not password protected")
PY
echo "Container bootstrap and web gate smoke test passed"
