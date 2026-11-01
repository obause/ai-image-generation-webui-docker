"""Small REST v2 adapter; no shell credentials or arbitrary CLI output parsing."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import httpx

from runpod_workspace.config import Config, duration_seconds

API_URL = "https://api.runpod.io/v2"


class RunPodError(Exception):
    pass


class RunPodClient:
    def __init__(self, api_key: str | None = None, transport: httpx.BaseTransport | None = None):
        key = api_key or os.environ.get("RUNPOD_API_KEY")
        if not key:
            raise RunPodError("RUNPOD_API_KEY is missing")
        self.http = httpx.Client(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
            transport=transport,
        )

    def __enter__(self) -> RunPodClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RunPodError("RunPod API connection failed") from exc
        if response.status_code >= 400:
            # Never echo a response body: it can include environment values or tokens.
            raise RunPodError(f"RunPod API {method} {path} returned HTTP {response.status_code}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise RunPodError("RunPod API returned invalid JSON") from exc

    def get_pod(self, pod_id: str) -> dict[str, Any] | None:
        try:
            return cast("dict[str, Any]", self._request("GET", f"/pods/{quote(pod_id, safe='')}"))
        except RunPodError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def list_pods(self) -> list[dict[str, Any]]:
        pods: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = self._request("GET", "/pods", params={"cursor": cursor} if cursor else {})
            pods.extend(data["pods"])
            cursor = data.get("pagination", {}).get("nextCursor")
            if not cursor:
                return pods

    def find_managed_pods(self, name: str) -> list[dict[str, Any]]:
        return [p for p in self.list_pods() if p.get("name") == name]

    def get_template(self, template_id: str) -> dict[str, Any]:
        return cast(
            "dict[str, Any]", self._request("GET", f"/templates/{quote(template_id, safe='')}")
        )

    def get_gpu(
        self, gpu_id: str, count: int, cloud: str, min_cuda_version: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "include": "AVAILABILITY",
            "product": "POD",
            "count": count,
            "cloud": cloud,
        }
        if min_cuda_version:
            params["minCudaVersion"] = min_cuda_version
        return cast(
            "dict[str, Any]",
            self._request(
                "GET",
                f"/catalog/gpus/{quote(gpu_id, safe='')}",
                params=params,
            ),
        )

    def get_ssh_keys(self) -> Any:
        return self._request("GET", "/account/ssh-keys")

    def add_ssh_key(self, public_key: str) -> None:
        """Register one public key while retaining every existing account key."""
        current = self.get_ssh_keys()
        keys = current.get("keys") if isinstance(current, dict) else None
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise RunPodError("RunPod SSH key response has an unexpected shape")
        new_blob = public_key.split()[1] if len(public_key.split()) >= 2 else ""
        if not new_blob:
            raise RunPodError("invalid SSH public key")
        if any(len(key.split()) >= 2 and key.split()[1] == new_blob for key in keys):
            return
        updated = self._request("PUT", "/account/ssh-keys", json={"keys": [*keys, public_key]})
        returned = updated.get("keys") if isinstance(updated, dict) else None
        if not isinstance(returned, list) or len(returned) != len(keys) + 1:
            raise RunPodError("RunPod did not confirm the updated SSH key set")

    def list_secrets(self) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self._request("GET", "/account/secrets")["secrets"])

    def ensure_secret(self, name: str, value: str) -> None:
        found = next((s for s in self.list_secrets() if s["name"] == name), None)
        if found:
            self._request(
                "PATCH", f"/account/secrets/{quote(found['id'], safe='')}", json={"value": value}
            )
        else:
            self._request("POST", "/account/secrets", json={"name": name, "value": value})

    def create_pod(self, config: Config) -> dict[str, Any]:
        rp = config.runpod
        prefix = rp.secret_prefix
        env = {
            "B2_APPLICATION_KEY_ID": f"{{{{ RUNPOD_SECRET_{prefix}_b2_id }}}}",
            "B2_APPLICATION_KEY": f"{{{{ RUNPOD_SECRET_{prefix}_b2_key }}}}",
            "RP_CONFIG_JSON": json.dumps(config.container_settings(), separators=(",", ":")),
            "RP_DEADLINE_AT": (
                datetime.now(UTC) + timedelta(seconds=duration_seconds(rp.terminate_after))
            ).isoformat(),
        }
        if os.environ.get("CIVITAI_API_TOKEN"):
            env["CIVITAI_API_TOKEN"] = f"{{{{ RUNPOD_SECRET_{prefix}_civitai }}}}"
        if os.environ.get("WORKSPACE_WEB_PASSWORD"):
            env["WORKSPACE_WEB_PASSWORD"] = f"{{{{ RUNPOD_SECRET_{prefix}_web_password }}}}"
        gpu_request: dict[str, Any] = {"id": rp.gpu.id, "count": rp.gpu.count}
        if rp.gpu.min_cuda_version:
            gpu_request["minCudaVersion"] = rp.gpu.min_cuda_version
        body = {
            "name": rp.pod_name,
            "image": rp.wrapper_image,
            "gpu": gpu_request,
            "cloud": rp.cloud,
            "disk": rp.disk_gb,
            "ports": rp.ports,
            "dataCenterIds": rp.data_center_ids,
            "startSsh": True,
            "entrypoint": ["/opt/runpod-bootstrap/entrypoint.sh"],
            "cmd": config.application.original_command,
            "env": env,
        }
        if rp.template_id:
            body["templateId"] = rp.template_id
        # Intentionally omit mounts: B2 is the only persistent store.
        return cast("dict[str, Any]", self._request("POST", "/pods", json=body))

    def delete_pod(self, pod_id: str) -> None:
        self._request("DELETE", f"/pods/{quote(pod_id, safe='')}")

    def log_lines(self, pod_id: str, *, follow: bool = False) -> list[str]:
        """Read v2 SSE logs, used when SSH is unavailable during bootstrap."""
        lines: list[str] = []
        try:
            with self.http.stream(
                "GET",
                f"/pods/{quote(pod_id, safe='')}/logs",
                params={"source": "container", "tail": 100},
                timeout=None if follow else 5,
            ) as response:
                if response.status_code >= 400:
                    raise RunPodError(f"RunPod logs returned HTTP {response.status_code}")
                for item in response.iter_lines():
                    if not item.startswith("data: "):
                        continue
                    try:
                        event = json.loads(item[6:])
                    except ValueError:
                        continue
                    line = str(event.get("line", ""))
                    for key in (
                        "RUNPOD_API_KEY",
                        "B2_APPLICATION_KEY_ID",
                        "B2_APPLICATION_KEY",
                        "B2_POD_APPLICATION_KEY_ID",
                        "B2_POD_APPLICATION_KEY",
                        "CIVITAI_API_TOKEN",
                        "WORKSPACE_WEB_PASSWORD",
                    ):
                        secret = os.environ.get(key)
                        if secret:
                            line = line.replace(secret, "[REDACTED]")
                    if follow:
                        print(line, flush=True)
                    else:
                        lines.append(line)
        except httpx.ReadTimeout:
            pass  # Historical backfill finished; SSE remained open without new events.
        except httpx.HTTPError as exc:
            raise RunPodError("RunPod log stream failed") from exc
        return lines
