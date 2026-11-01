from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from runpod_workspace.ssh import (
    SSHClient,
    can_sign_ssh_identity,
    generate_dedicated_key,
    matching_agent_keys,
    matching_local_keys,
    prepare_ssh_identity,
)


def test_doctor_finds_key_in_configured_ssh_agent(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs.get("env", {}).get("SSH_AUTH_SOCK")))
        output = (
            "identityagent ~/.ssh/agent.sock\n"
            if args[:2] == ["ssh", "-G"]
            else "ssh-ed25519 matching-key local\n"
        )
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert matching_agent_keys(["ssh-ed25519 matching-key runpod"]) == 1
    assert calls[1][1] == str(Path.home() / ".ssh/agent.sock")
    assert prepare_ssh_identity(["ssh-ed25519 matching-key runpod"], tmp_path)
    assert (tmp_path / "identity.pub").read_text() == "ssh-ed25519 matching-key\n"
    assert can_sign_ssh_identity(tmp_path)
    pod = {"id": "pod-1", "ssh": {"direct": {"host": "127.0.0.1", "port": 22, "username": "root"}}}
    args = SSHClient(pod, state_dir=tmp_path).args()
    assert "IdentitiesOnly=yes" in args
    assert str(tmp_path / "identity.pub") in args


def test_doctor_rejects_unmatched_agent_key(monkeypatch: Any, tmp_path: Path) -> None:
    def run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        output = "identityagent none\n" if args[:2] == ["ssh", "-G"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert matching_agent_keys(["ssh-ed25519 registered runpod"]) == 0
    assert not prepare_ssh_identity(["ssh-ed25519 registered runpod"], tmp_path)
    assert not (tmp_path / "identity.pub").exists()
    assert not can_sign_ssh_identity(tmp_path)


def test_paid_pod_guard_rejects_locked_agent(monkeypatch: Any, tmp_path: Path) -> None:
    (tmp_path / "identity.pub").write_text("ssh-ed25519 matching-key\n")
    monkeypatch.setattr("runpod_workspace.ssh._agent_environment", lambda: {})

    def failed_sign(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="approval required")

    monkeypatch.setattr(subprocess, "run", failed_sign)
    assert not can_sign_ssh_identity(tmp_path)


def test_dedicated_key_works_without_agent(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr("runpod_workspace.ssh._agent_public_keys", lambda: [])
    monkeypatch.setattr("runpod_workspace.ssh._agent_environment", lambda: None)
    public = generate_dedicated_key(tmp_path)
    assert generate_dedicated_key(tmp_path) == public
    assert matching_local_keys([public], tmp_path) == 1
    assert prepare_ssh_identity([public], tmp_path)
    assert can_sign_ssh_identity(tmp_path)
    pod = {"id": "pod-1", "ssh": {"direct": {"host": "127.0.0.1", "port": 22, "username": "root"}}}
    args = SSHClient(pod, state_dir=tmp_path).args()
    assert "IdentityAgent=none" in args
    assert str(tmp_path / "id_ed25519") in args


def test_ssh_host_pin_changes_with_pod_image(tmp_path: Path) -> None:
    pod = {
        "id": "pod-1",
        "image": "example/image:one",
        "ssh": {"direct": {"host": "127.0.0.1", "port": 22, "username": "root"}},
    }
    first = SSHClient(pod, state_dir=tmp_path).host_key_alias
    assert first == SSHClient(pod, state_dir=tmp_path).host_key_alias
    pod["image"] = "example/image:two"
    second = SSHClient(pod, state_dir=tmp_path).host_key_alias
    assert first != second
