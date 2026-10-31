#!/usr/bin/env bash
set -euo pipefail

# ComfyUI and SwarmUI use /workspace/models as their common model root.
mkdir -p /workspace/models/checkpoints /workspace/models/loras /workspace/models/vae
mkdir -p /workspace/output /workspace/swarmui-data
cd /workspace/models
ln -sfn checkpoints Stable-Diffusion
ln -sfn loras Lora
ln -sfn vae VAE
cd /opt/SwarmUI
exec dotnet ./src/bin/live_release/SwarmUI.dll --host 127.0.0.1 --port 17801 --launch_mode none \
    --data_dir /workspace/swarmui-data
