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


def test_reconcile_pod_using_configured_alternative_gpu(config: Config, tmp_path: Path) -> None:
    config.runpod.gpu.alternatives = ["NVIDIA RTX 6000 Ada Generation"]
    client = FakeClient()
    assert client.pod is not None
    client.pod["gpu"] = {"id": "NVIDIA RTX 6000 Ada Generation"}
    store = StateStore(tmp_path)
    life = Lifecycle(config, client, store, FakeSSH)  # type: ignore[arg-type]
    assert life.managed_pod() == client.pod
    saved = store.load()
    assert saved is not None
    assert saved.gpu_id == "NVIDIA RTX 6000 Ada Generation"


def test_wait_ready_uses_image_python(config: Config, tmp_path: Path) -> None:
    life, _, _ = make_life(config, tmp_path)
    commands: list[str] = []

    class CapturingSSH(FakeSSH):
        def run(self, command: str, timeout: int = 120) -> str:
            commands.append(command)
            return ""

    life.ssh_factory = CapturingSSH  # type: ignore[assignment]
    life.wait_ready("pod123", progress=lambda _: None)
    assert commands
    assert "/opt/venv/bin/python -c" in commands[0]
    assert "python3 -c" not in commands[0]


def test_sync_uses_image_python(config: Config, tmp_path: Path) -> None:
    life, _, _ = make_life(config, tmp_path)
    commands: list[str] = []

    class CapturingSSH(FakeSSH):
        def run(self, command: str, timeout: int = 120) -> str:
            commands.append(command)
            return ""

    life.ssh_factory = CapturingSSH  # type: ignore[assignment]
    life.sync()
    assert commands[0].startswith("/opt/venv/bin/python /opt/runpod-bootstrap/worker.py backup")


def test_down_runs_backup_before_delete(config: Config, tmp_path: Path) -> None:
    life, client, store = make_life(config, tmp_path)
    commands: list[str] = []

    class CapturingSSH(FakeSSH):
        def run(self, command: str, timeout: int = 120) -> str:
            assert not client.deleted
            commands.append(command)
            return ""

    life.ssh_factory = CapturingSSH  # type: ignore[assignment]
    life.down(progress=lambda _: None)
    assert commands[0].startswith("/opt/venv/bin/python /opt/runpod-bootstrap/worker.py backup")
    assert client.deleted
    assert store.load() is None


def test_up_sends_only_dedicated_pod_b2_key(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.runpod.template_id = None
    client = FakeClient()
    client.pod = None
    client.get_gpu = lambda *_args: {"availability": "HIGH"}  # type: ignore[method-assign]
    secrets: dict[str, str] = {}
    client.ensure_secret = lambda name, value: secrets.update({name: value})  # type: ignore[attr-defined]
    client.create_pod = lambda _config, gpu_id: {"id": "new-pod", "gpu": {"id": gpu_id}}  # type: ignore[attr-defined]
    monkeypatch.setenv("B2_APPLICATION_KEY_ID", "account-wide-id")
    monkeypatch.setenv("B2_APPLICATION_KEY", "account-wide-key")
    monkeypatch.setenv("B2_POD_APPLICATION_KEY_ID", "restricted-id")
    monkeypatch.setenv("B2_POD_APPLICATION_KEY", "restricted-key")
    monkeypatch.setenv("WORKSPACE_WEB_PASSWORD", "long-test-password")
    monkeypatch.setattr(
        "runpod_workspace.lifecycle.verify_pod_b2_key",
        lambda _bucket, **_kwargs: None,
    )
    life = Lifecycle(config, client, StateStore(tmp_path), FakeSSH)  # type: ignore[arg-type]
    assert life.up(wait=False, progress=lambda _: None)["id"] == "new-pod"
    assert secrets["rp_workspace_b2_id"] == "restricted-id"
    assert secrets["rp_workspace_b2_key"] == "restricted-key"
    assert "account-wide-key" not in secrets.values()


def test_gpu_fallback_uses_explicit_order_and_saves_selected_gpu(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config.runpod.template_id = None
    config.runpod.gpu.id = "NVIDIA GeForce RTX 5090"
    config.runpod.gpu.alternatives = [
        "NVIDIA GeForce RTX 4090",
        "NVIDIA RTX 6000 Ada Generation",
    ]
    client = FakeClient()
    client.pod = None
    checked: list[str] = []
    created: list[str] = []

    def get_gpu(gpu_id: str, *_args: object) -> dict[str, Any]:
        checked.append(gpu_id)
        availability = "NONE" if gpu_id == "NVIDIA GeForce RTX 5090" else "LOW"
        return {"id": gpu_id, "availability": availability, "price": {"community": 0.5}}

    client.get_gpu = get_gpu  # type: ignore[method-assign]

    def create_pod(_config: Config, gpu_id: str) -> dict[str, Any]:
        created.append(gpu_id)
        return {"id": "new-pod", "gpu": {"id": gpu_id}}

    client.create_pod = create_pod  # type: ignore[attr-defined]
    client.ensure_secret = lambda *_args: None  # type: ignore[attr-defined]
    monkeypatch.setenv("B2_POD_APPLICATION_KEY_ID", "restricted-id")
    monkeypatch.setenv("B2_POD_APPLICATION_KEY", "restricted-key")
    monkeypatch.setenv("WORKSPACE_WEB_PASSWORD", "long-test-password")
    monkeypatch.setattr(
        "runpod_workspace.lifecycle.verify_pod_b2_key", lambda *_args, **_kwargs: None
    )
    store = StateStore(tmp_path)
    life = Lifecycle(config, client, store, FakeSSH)  # type: ignore[arg-type]
    messages: list[str] = []
    assert life.up(wait=False, progress=messages.append)["id"] == "new-pod"
    assert checked == ["NVIDIA GeForce RTX 5090", "NVIDIA GeForce RTX 4090"]
    assert created == ["NVIDIA GeForce RTX 4090"]
    assert store.load().gpu_id == "NVIDIA GeForce RTX 4090"  # type: ignore[union-attr]
    assert any("Selected GPU: NVIDIA GeForce RTX 4090" in message for message in messages)


def test_no_available_configured_gpu_does_not_create_pod(config: Config, tmp_path: Path) -> None:
    config.runpod.template_id = None
    config.runpod.gpu.alternatives = ["NVIDIA RTX 6000 Ada Generation"]
    client = FakeClient()
    client.pod = None
    client.get_gpu = lambda *_args: {"availability": "NONE"}  # type: ignore[method-assign]
    created = False

    def create_pod(_config: Config, _gpu_id: str) -> dict[str, Any]:
        nonlocal created
        created = True
        return {"id": "new-pod"}

    client.create_pod = create_pod  # type: ignore[attr-defined]
    life = Lifecycle(config, client, StateStore(tmp_path), FakeSSH)  # type: ignore[arg-type]
    with pytest.raises(LifecycleError, match="No configured GPU is available"):
        life.up(wait=False, progress=lambda _: None)
    assert not created
