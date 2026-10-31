from __future__ import annotations

import pytest

from runpod_workspace.config import Config


@pytest.fixture
def config() -> Config:
    return Config.model_validate(
        {
            "runpod": {
                "template_id": "tpl123",
                "wrapper_image": "example/image@sha256:abc",
                "pod_name": "managed",
                "gpu": {"id": "NVIDIA GeForce RTX 4090", "count": 1},
                "disk_gb": 280,
                "ports": ["7801/http", "22/tcp"],
            },
            "storage": {
                "bucket": "example-bucket",
                "restore": [
                    {
                        "name": "models",
                        "remote": "models",
                        "local": "/workspace/models",
                    }
                ],
                "backup": [
                    {
                        "name": "models",
                        "remote": "models",
                        "local": "/workspace/models",
                    }
                ],
            },
            "application": {"shared_models_path": "/workspace/models"},
        }
    )
