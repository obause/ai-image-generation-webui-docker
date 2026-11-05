from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from runpod_workspace.config import Config, duration_seconds


def test_valid_defaults(config: Config) -> None:
    assert config.storage.backup[0].mode == "copy"
    assert config.storage.restore[0].mode == "copy"
    assert duration_seconds(config.runpod.terminate_after) == 28800
    assert config.runpod.gpu.ordered_ids() == ["NVIDIA GeForce RTX 4090"]


def test_ordered_gpu_alternatives(config: Config) -> None:
    raw = config.model_dump()
    raw["runpod"]["gpu"]["alternatives"] = [
        "NVIDIA GeForce RTX 5090",
        "NVIDIA RTX 6000 Ada Generation",
    ]
    changed = Config.model_validate(raw)
    assert changed.runpod.gpu.ordered_ids() == [
        "NVIDIA GeForce RTX 4090",
        "NVIDIA GeForce RTX 5090",
        "NVIDIA RTX 6000 Ada Generation",
    ]


def test_premium_gpu_ids_must_be_distinct(config: Config) -> None:
    raw = config.model_dump()
    raw["runpod"]["gpu"]["premium"] = ["NVIDIA H100 80GB HBM3"]
    assert Config.model_validate(raw).runpod.gpu.all_ids() == [
        "NVIDIA GeForce RTX 4090",
        "NVIDIA H100 80GB HBM3",
    ]
    raw["runpod"]["gpu"]["premium"].append("NVIDIA GeForce RTX 4090")
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


def test_empty_premium_preserves_previous_config_fingerprint(config: Config) -> None:
    old_shape = config.model_dump()
    old_shape["runpod"]["gpu"].pop("premium")
    expected = hashlib.sha256(json.dumps(old_shape, sort_keys=True).encode()).hexdigest()[:16]
    assert config.fingerprint() == expected


@pytest.mark.parametrize(
    "alternatives",
    [
        ["NVIDIA GeForce RTX 4090"],
        ["NVIDIA GeForce RTX 5090", "NVIDIA GeForce RTX 5090"],
        [""],
        ["<GPU_ID>"],
        [" NVIDIA GeForce RTX 5090"],
    ],
)
def test_invalid_gpu_alternatives(config: Config, alternatives: list[str]) -> None:
    raw = config.model_dump()
    raw["runpod"]["gpu"]["alternatives"] = alternatives
    with pytest.raises(ValidationError):
        Config.model_validate(raw)


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
    assert not config.uses_destructive_sync()
    raw = config.model_dump()
    raw["storage"]["backup"][0]["mode"] = "sync"
    changed = Config.model_validate(raw)
    assert changed.storage.backup[0].mode == "sync"
    assert changed.uses_destructive_sync()


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


def test_background_restore_must_be_optional_copy(config: Config) -> None:
    raw = config.model_dump()
    raw["storage"]["restore"][0]["background"] = True
    with pytest.raises(ValidationError):
        Config.model_validate(raw)
    raw["storage"]["restore"][0]["required"] = False
    raw["storage"]["restore"][0]["mode"] = "sync"
    with pytest.raises(ValidationError):
        Config.model_validate(raw)
    raw["storage"]["restore"][0]["mode"] = "copy"
    assert Config.model_validate(raw).storage.restore[0].background
