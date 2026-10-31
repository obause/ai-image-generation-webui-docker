"""Atomic local state and a process lock for lifecycle commands."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

STATE_DIR = Path.home() / ".local/state/runpod-workspace"


class StateError(Exception):
    """Corrupt or unreadable state, with no file contents in the message."""


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pod_id: str
    created_at: datetime
    config_hash: str
    template_id: str | None
    gpu_id: str

    @classmethod
    def new(cls, pod_id: str, config_hash: str, template_id: str | None, gpu_id: str) -> State:
        return cls(
            pod_id=pod_id,
            created_at=datetime.now(UTC),
            config_hash=config_hash,
            template_id=template_id,
            gpu_id=gpu_id,
        )


class StateStore:
    def __init__(self, directory: Path = STATE_DIR):
        self.directory = directory
        self.path = directory / "state.json"
        self.lock_path = directory / "state.lock"

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> State | None:
        if not self.path.exists():
            return None
        try:
            return State.model_validate_json(self.path.read_text())
        except (OSError, ValidationError) as exc:
            raise StateError(f"invalid state file at {self.path}; inspect it manually") from exc

    def save(self, state: State) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(state.model_dump(mode="json"), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            dir_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
