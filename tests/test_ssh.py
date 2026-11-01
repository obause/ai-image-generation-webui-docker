from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from runpod_workspace.ssh import SSHClient, matching_agent_keys, prepare_ssh_identity


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
