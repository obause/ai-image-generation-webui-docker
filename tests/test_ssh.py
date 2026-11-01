from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from runpod_workspace.ssh import matching_agent_keys


def test_doctor_finds_key_in_configured_ssh_agent(monkeypatch: Any) -> None:
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


def test_doctor_rejects_unmatched_agent_key(monkeypatch: Any) -> None:
    def run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        output = "identityagent none\n" if args[:2] == ["ssh", "-G"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert matching_agent_keys(["ssh-ed25519 registered runpod"]) == 0
