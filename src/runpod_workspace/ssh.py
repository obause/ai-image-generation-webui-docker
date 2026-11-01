"""Direct SSH transport for Pod control commands."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from runpod_workspace.state import STATE_DIR


class SSHError(Exception):
    pass


def matching_agent_keys(registered: list[str]) -> int:
    """Count RunPod public keys available from the SSH agent selected by ssh config."""
    try:
        config = subprocess.run(
            ["ssh", "-G", "runpod.invalid"], capture_output=True, text=True, timeout=5, check=False
        )
        agent = next(
            (
                line.split(" ", 1)[1]
                for line in config.stdout.splitlines()
                if line.startswith("identityagent ")
            ),
            None,
        )
        env = os.environ.copy()
        if agent == "none":
            return 0
        if agent and agent != "SSH_AUTH_SOCK":
            env["SSH_AUTH_SOCK"] = os.path.expanduser(agent)
        keys = subprocess.run(
            ["ssh-add", "-L"], env=env, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if keys.returncode:
        return 0

    def blob(line: str) -> str:
        parts = line.split()
        return parts[1] if len(parts) >= 2 else ""

    available = {value for line in keys.stdout.splitlines() if (value := blob(line))}
    expected = {value for line in registered if (value := blob(line))}
    return len(available & expected)


class SSHClient:
    def __init__(self, pod: dict[str, Any], state_dir: Path = STATE_DIR):
        direct = pod.get("ssh", {}).get("direct")
        if not direct:
            raise SSHError("direct SSH endpoint is unavailable; ensure 22/tcp is exposed")
        self.pod_id = str(pod["id"])
        self.host = str(direct["host"])
        self.port = int(direct["port"])
        self.username = str(direct["username"])
        self.state_dir = state_dir

    def args(self, *, interactive: bool = False) -> list[str]:
        if not shutil.which("ssh"):
            raise SSHError("OpenSSH client is not installed")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        result = [
            "ssh",
            "-p",
            str(self.port),
            "-o",
            f"HostKeyAlias=runpod-{self.pod_id}",
            "-o",
            f"UserKnownHostsFile={self.state_dir / 'known_hosts'}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=10",
        ]
        if not interactive:
            result += ["-o", "BatchMode=yes"]
        else:
            result += ["-tt"]
        return result + [f"{self.username}@{self.host}"]

    def run(self, command: str, timeout: int = 120) -> str:
        try:
            result = subprocess.run(
                self.args() + [command],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SSHError("SSH command failed or timed out") from exc
        if result.returncode:
            # Remote output is deliberately excluded; it might include credentials.
            raise SSHError(f"SSH command exited with status {result.returncode}")
        return result.stdout.strip()

    def stream(self, command: str, *, interactive: bool = False) -> int:
        try:
            return subprocess.call(self.args(interactive=interactive) + [command])
        except OSError as exc:
            raise SSHError("SSH could not start") from exc
