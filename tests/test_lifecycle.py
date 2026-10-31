from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from runpod_workspace.config import Config
from runpod_workspace.lifecycle import Lifecycle, LifecycleError
from runpod_workspace.ssh import SSHError
from runpod_workspace.state import State, StateStore


class FakeClient:
    def __init__(self) -> None:
        self.pod: dict[str, Any] | None = {
            "id": "pod123",
            "name": "managed",
            "image": "example/image@sha256:abc",
            "gpu": {"id": "NVIDIA GeForce RTX 4090"},
            "status": "RUNNING",
            "ssh": {
                "direct": {
                    "host": "example",
                    "port": 22,
                    "username": "root",
                }
            },
        }
        self.deleted = False

    def get_pod(self, _pod_id: str) -> dict[str, Any] | None:
        return self.pod

    def find_managed_pods(self, _name: str) -> list[dict[str, Any]]:
        return [self.pod] if self.pod else []

    def delete_pod(self, _pod_id: str) -> None:
        self.deleted = True
        self.pod = None


class FakeSSH:
    fail = False

    def __init__(self, _pod: dict[str, Any]):
        pass

    def run(self, _command: str, timeout: int = 120) -> str:
        if self.fail:
            raise SSHError("backup failed")
        return ""


def make_life(config: Config, tmp_path: Path) -> tuple[Lifecycle, FakeClient, StateStore]:
    client = FakeClient()
    store = StateStore(tmp_path)
    store.save(State.new("pod123", config.fingerprint(), "tpl123", config.runpod.gpu.id))
    return Lifecycle(config, client, store, FakeSSH), client, store  # type: ignore[arg-type]


def test_backup_failure_keeps_pod(config: Config, tmp_path: Path) -> None:
    life, client, store = make_life(config, tmp_path)
    FakeSSH.fail = True
    try:
        with pytest.raises(LifecycleError, match="NOT terminated"):
            life.down()
        assert not client.deleted
        assert store.load() is not None
    finally:
        FakeSSH.fail = False


def test_force_deletes_after_backup_failure(config: Config, tmp_path: Path) -> None:
    life, client, store = make_life(config, tmp_path)
    FakeSSH.fail = True
    try:
        life.down(force=True, progress=lambda _: None)
        assert client.deleted
        assert store.load() is None
    finally:
        FakeSSH.fail = False


def test_stale_state_reconciles(config: Config, tmp_path: Path) -> None:
    life, client, store = make_life(config, tmp_path)
    original = client.get_pod
    calls = 0

    def once(pod_id: str) -> dict[str, Any] | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else original(pod_id)

    client.get_pod = once  # type: ignore[method-assign]
    assert life.managed_pod()["id"] == "pod123"  # type: ignore[index]
    assert store.load() is not None
