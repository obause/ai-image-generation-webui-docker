"""Direct SSH transport for Pod control commands."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from runpod_workspace.state import STATE_DIR


class SSHError(Exception):
    pass


def _agent_public_keys() -> list[str]:
    """Read public keys from the agent selected by SSH config for Pod hosts."""
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
            return []
        if agent and agent != "SSH_AUTH_SOCK":
            env["SSH_AUTH_SOCK"] = os.path.expanduser(agent)
        keys = subprocess.run(
            ["ssh-add", "-L"], env=env, capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if keys.returncode:
        return []
    return keys.stdout.splitlines()


def _blob(line: str) -> str:
    parts = line.split()
    return parts[1] if len(parts) >= 2 else ""


def matching_agent_keys(registered: list[str]) -> int:
    """Count RunPod public keys available from the selected SSH agent."""
    available = {value for line in _agent_public_keys() if (value := _blob(line))}
    expected = {value for line in registered if (value := _blob(line))}
    return len(available & expected)


def prepare_ssh_identity(registered: list[str], state_dir: Path = STATE_DIR) -> bool:
    """Pin a matching public key so SSH does not try unrelated agent identities."""
    expected = {value for line in registered if (value := _blob(line))}
    selected = next((line for line in _agent_public_keys() if _blob(line) in expected), None)
    if selected is None:
        return False
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = state_dir / "identity.pub"
    fd, temporary = tempfile.mkstemp(prefix=".identity-", dir=state_dir)
    try:
        with os.fdopen(fd, "w") as handle:
            parts = selected.split()
            handle.write(f"{parts[0]} {parts[1]}\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


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
        identity = self.state_dir / "identity.pub"
        if identity.is_file():
            result += ["-o", "IdentitiesOnly=yes", "-i", str(identity)]
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
