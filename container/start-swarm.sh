#!/usr/bin/env bash
set -euo pipefail

# ComfyUI and SwarmUI use /workspace/models as their common model root.
mkdir -p /workspace/models/checkpoints /workspace/models/loras /workspace/models/vae
mkdir -p /workspace/output /workspace/swarmui-data
if [[ ! -e /workspace/swarmui-data/Settings.fds ]]; then
    cp /opt/runpod-bootstrap/swarm-settings.fds /workspace/swarmui-data/Settings.fds
fi
if [[ ! -e /workspace/swarmui-data/Backends.fds ]]; then
    cp /opt/runpod-bootstrap/swarm-backends.fds /workspace/swarmui-data/Backends.fds
fi
cd /workspace/models
ln -sfn checkpoints Stable-Diffusion
ln -sfn loras Lora
ln -sfn vae VAE
cd /opt/SwarmUI
# The seeded backend should initialize only after its ComfyUI API is ready.
for _ in {1..300}; do
    if python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:18188/system_stats", timeout=2)' \
        >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
if ! python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:18188/system_stats", timeout=2)' \
    >/dev/null 2>&1; then
    echo "ComfyUI API did not become ready; SwarmUI startup blocked" >&2
    exit 1
fi
exec dotnet ./src/bin/live_release/SwarmUI.dll --host 127.0.0.1 --port 17801 --launch_mode none \
    --data_dir /workspace/swarmui-data
