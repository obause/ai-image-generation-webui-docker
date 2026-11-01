from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from runpod_workspace.secrets import SecretsError, load_local_secrets, verify_pod_b2_key


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


@pytest.mark.parametrize("capabilities,valid", [
    (["listBuckets", "listFiles", "readFiles", "writeFiles"], True),
    (["listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"], False),
    (["listBuckets", "listFiles", "readFiles"], False),
])
def test_pod_b2_key_rejects_broad_or_incomplete_scope(
    monkeypatch: pytest.MonkeyPatch, capabilities: list[str], valid: bool
) -> None:
    monkeypatch.setenv("B2_POD_APPLICATION_KEY_ID", "restricted-id")
    monkeypatch.setenv("B2_POD_APPLICATION_KEY", "do-not-print-me")

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"apiInfo": {"storageApi": {"allowed": {
            "buckets": [{"name": "destination", "id": "bucket-id"}],
            "capabilities": capabilities,
        }}}})

    transport = httpx.MockTransport(respond)
    if valid:
        verify_pod_b2_key("destination", transport)
    else:
        with pytest.raises(SecretsError) as error:
            verify_pod_b2_key("destination", transport)
        assert "do-not-print-me" not in str(error.value)


def test_explicit_sync_requires_delete_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("B2_POD_APPLICATION_KEY_ID", "restricted-id")
    monkeypatch.setenv("B2_POD_APPLICATION_KEY", "do-not-print-me")

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"apiInfo": {"storageApi": {"allowed": {
            "buckets": [{"name": "destination", "id": "bucket-id"}],
            "capabilities": ["listBuckets", "listFiles", "readFiles", "writeFiles", "deleteFiles"],
        }}}})

    verify_pod_b2_key("destination", httpx.MockTransport(respond), allow_delete=True)

    def missing_delete(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"apiInfo": {"storageApi": {"allowed": {
            "buckets": [{"name": "destination", "id": "bucket-id"}],
            "capabilities": ["listBuckets", "listFiles", "readFiles", "writeFiles"],
        }}}})

    with pytest.raises(SecretsError):
        verify_pod_b2_key("destination", httpx.MockTransport(missing_delete), allow_delete=True)
