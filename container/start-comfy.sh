#!/usr/bin/env bash
set -euo pipefail
cd /opt/ComfyUI
exec /opt/venv/bin/python main.py --listen 127.0.0.1 --port 18188 \
    --extra-model-paths-config /opt/ComfyUI/extra_model_paths.yaml
