from __future__ import annotations

import pytest
from pydantic import ValidationError

from runpod_workspace.config import Config, duration_seconds


def test_valid_defaults(config: Config) -> None:
    assert config.storage.backup[0].mode == "copy"
    assert config.storage.restore[0].mode == "copy"
    assert duration_seconds(config.runpod.terminate_after) == 28800


@pytest.mark.parametrize(
    "change",
    [
        ("runpod", "gpu", {}),
        ("runpod", "gpu", {"count": 1}),
        ("runpod", "gpu", {"id": "NVIDIA GeForce RTX 5090", "min_cuda_version": "twelve"}),
        (
            "storage",
            "backup",
            [{"name": "x", "remote": "x", "local": "/workspace/x", "mode": "mirror"}],
        ),
        (
            "storage",
            "backup",
            [{"name": "x", "remote": "x", "local": "/workspace/x", "interval_seconds": 0}],
        ),
    ],
)
def test_invalid_config(config: Config, change: tuple[str, str, object]) -> None:
    raw = config.model_dump()
    raw[change[0]][change[1]] = change[2]
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_sync_must_be_explicit(config: Config) -> None:
    raw = config.model_dump()
    raw["storage"]["backup"][0]["mode"] = "sync"
    assert Config.model_validate(raw).storage.backup[0].mode == "sync"


def test_remote_escape_rejected(config: Config) -> None:
    raw = config.model_dump()
    raw["storage"]["backup"][0]["remote"] = "../other-bucket"
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_prefix_must_be_relative(config: Config) -> None:
    raw = config.model_dump()
    raw["storage"]["prefix"] = "../old-backup"
    with pytest.raises(ValidationError):
        Config.model_validate(raw)
