from __future__ import annotations

import os
from pathlib import Path

import pytest

from runpod_workspace.secrets import SecretsError, load_local_secrets


def test_private_file_loads_without_revealing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("RUNPOD_API_KEY=hidden-test-token\nB2_APPLICATION_KEY=hidden-test-key\n")
    path.chmod(0o600)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.delenv("B2_APPLICATION_KEY", raising=False)
    load_local_secrets(path)
    assert os.environ["RUNPOD_API_KEY"] == "hidden-test-token"
    assert os.environ["B2_APPLICATION_KEY"] == "hidden-test-key"


def test_environment_overrides_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("RUNPOD_API_KEY=local-value\n")
    path.chmod(0o600)
    monkeypatch.setenv("RUNPOD_API_KEY", "environment-value")
    load_local_secrets(path)
    assert os.environ["RUNPOD_API_KEY"] == "environment-value"


def test_insecure_permissions_rejected_without_value(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("RUNPOD_API_KEY=hidden-test-token\n")
    path.chmod(0o644)
    with pytest.raises(SecretsError) as error:
        load_local_secrets(path)
    assert "hidden-test-token" not in str(error.value)


def test_unknown_key_rejected_without_value(tmp_path: Path) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("UNEXPECTED=hidden-test-token\n")
    path.chmod(0o600)
    with pytest.raises(SecretsError) as error:
        load_local_secrets(path)
    assert "hidden-test-token" not in str(error.value)
