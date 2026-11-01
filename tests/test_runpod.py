from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from runpod_workspace.config import Config
from runpod_workspace.runpod import RunPodClient, RunPodError


def test_create_pod_uses_v2_and_secret_references(config: Config) -> None:
    seen: dict = {}

    def respond(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "pod123", "status": "PROVISIONING"})

    with RunPodClient("mock-secret", httpx.MockTransport(respond)) as client:
        assert client.create_pod(config)["id"] == "pod123"
    assert seen["url"] == "https://api.runpod.io/v2/pods"
    assert seen["body"]["gpu"] == {"id": "NVIDIA GeForce RTX 4090", "count": 1}
    assert seen["body"]["templateId"] == "tpl123"
    assert seen["body"]["image"] == config.runpod.wrapper_image
    assert "mounts" not in seen["body"]
    assert "mock-secret" not in json.dumps(seen["body"])
    assert seen["body"]["env"]["B2_APPLICATION_KEY"] == "{{ RUNPOD_SECRET_rp_workspace_b2_key }}"
    assert seen["body"]["entrypoint"] == ["/opt/runpod-bootstrap/entrypoint.sh"]
    assert datetime.fromisoformat(seen["body"]["env"]["RP_DEADLINE_AT"]).tzinfo is not None


def test_create_pod_without_template(config: Config) -> None:
    config.runpod.template_id = None
    config.runpod.gpu.min_cuda_version = "12.8"
    bodies: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "pod123"})

    with RunPodClient("key", httpx.MockTransport(respond)) as client:
        client.create_pod(config)
    assert "templateId" not in bodies[0]
    assert bodies[0]["gpu"]["minCudaVersion"] == "12.8"


def test_log_stream_redacts_local_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("B2_APPLICATION_KEY", "very-private-test-value")
    monkeypatch.setenv("CIVITAI_API_TOKEN", "very-private-civitai-value")
    monkeypatch.setenv("HF_TOKEN", "very-private-hf-value")
    payload = (
        'data: {"line":"bad: very-private-test-value and '
        'very-private-civitai-value and very-private-hf-value",'
        '"source":"container"}\n\n'
    )

    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})

    with RunPodClient("key", httpx.MockTransport(respond)) as client:
        lines = client.log_lines("pod123")
    assert lines == ["bad: [REDACTED] and [REDACTED] and [REDACTED]"]


def test_system_logs_dedupe_repeated_pull_progress() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            text='data: {"line":"Extracting"}\n\ndata: {"line":"Extracting"}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    with RunPodClient("key", httpx.MockTransport(respond)) as client:
        lines = client.log_lines("pod123", source=None)
    assert "source=" not in seen[0]
    assert lines == ["Extracting (repeated 2 times)"]


def test_optional_civitai_secret_is_reference(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIVITAI_API_TOKEN", "very-private-civitai-value")
    bodies: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "pod123"})

    with RunPodClient("key", httpx.MockTransport(respond)) as client:
        client.create_pod(config)
    assert bodies[0]["env"]["CIVITAI_API_TOKEN"] == "{{ RUNPOD_SECRET_rp_workspace_civitai }}"
    assert "very-private-civitai-value" not in json.dumps(bodies[0])


def test_optional_hf_secret_is_reference(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "very-private-hf-value")
    bodies: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "pod123"})

    with RunPodClient("key", httpx.MockTransport(respond)) as client:
        client.create_pod(config)
    assert bodies[0]["env"]["HF_TOKEN"] == "{{ RUNPOD_SECRET_rp_workspace_hf }}"
    assert "very-private-hf-value" not in json.dumps(bodies[0])


def test_get_delete_and_errors_redact(config: Config) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "mock-secret"})
        return httpx.Response(403, json={"detail": "mock-secret"})

    with RunPodClient("mock-secret", httpx.MockTransport(respond)) as client:
        assert client.get_pod("missing") is None
        with pytest.raises(RunPodError) as exc:
            client.delete_pod("pod123")
    assert "mock-secret" not in str(exc.value)


def test_gpu_unavailable(config: Config) -> None:
    with RunPodClient("key", httpx.MockTransport(lambda _: httpx.Response(404))) as client:
        with pytest.raises(RunPodError, match="HTTP 404"):
            client.get_gpu(config.runpod.gpu.id, 1, "COMMUNITY")


def test_gpu_availability_query_uses_cuda_floor() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"id": "NVIDIA GeForce RTX 5090", "availability": "HIGH"})

    with RunPodClient("key", httpx.MockTransport(respond)) as client:
        client.get_gpu("NVIDIA GeForce RTX 5090", 1, "COMMUNITY", "12.8")
    assert "minCudaVersion=12.8" in seen[0]


def test_add_ssh_key_preserves_registered_keys() -> None:
    existing = "ssh-ed25519 existing first"
    new = "ssh-ed25519 new-key runpod-workspace"
    writes: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"keys": [existing]})
        body = json.loads(request.content)
        writes.append(body)
        return httpx.Response(200, json=body)

    with RunPodClient("key", httpx.MockTransport(respond)) as client:
        client.add_ssh_key(new)
        client.add_ssh_key(existing)
    assert writes == [{"keys": [existing, new]}]


def test_add_ssh_key_rejects_lost_existing_key() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"keys": ["ssh-ed25519 existing first"]})
        return httpx.Response(200, json={"keys": ["ssh-ed25519 other second"]})

    with RunPodClient("key", httpx.MockTransport(respond)) as client:
        with pytest.raises(RunPodError, match="did not confirm"):
            client.add_ssh_key("ssh-ed25519 new-key runpod-workspace")
