"""Load local credentials without shell evaluation or diagnostic disclosure."""

from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_SECRETS = Path.home() / ".config/runpod-workspace/secrets.env"
ALLOWED_KEYS = frozenset(
    {
        "RUNPOD_API_KEY",
        "B2_APPLICATION_KEY_ID",
        "B2_APPLICATION_KEY",
        "CIVITAI_API_TOKEN",
        "WORKSPACE_WEB_PASSWORD",
    }
)


class SecretsError(Exception):
    """Safe error that never includes credential values."""


def load_local_secrets(path: Path = DEFAULT_SECRETS) -> None:
    """Load a private KEY=VALUE file; existing process environment takes precedence."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SecretsError(f"cannot access local secrets file at {path}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SecretsError("local secrets file must be a regular file owned by this user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SecretsError(f"local secrets file must be private: chmod 600 {path}")
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise SecretsError(f"cannot read local secrets file at {path}") from exc
    values: dict[str, str] = {}
    for number, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "=" not in raw:
            raise SecretsError(f"invalid local secrets file line {number}; use KEY=VALUE")
        key, value = raw.split("=", 1)
        if key not in ALLOWED_KEYS or not value or "\x00" in value:
            raise SecretsError(f"invalid local secrets file line {number}")
        if key in values:
            raise SecretsError(f"duplicate local secrets key on line {number}")
        values[key] = value
    for key, value in values.items():
        if not os.environ.get(key):
            os.environ[key] = value
