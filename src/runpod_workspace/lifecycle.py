"""Pod lifecycle with explicit backup-before-delete invariant."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from runpod_workspace.config import Config
from runpod_workspace.runpod import RunPodClient
from runpod_workspace.secrets import SecretsError, verify_pod_b2_key
from runpod_workspace.ssh import SSHClient, SSHError
from runpod_workspace.state import State, StateStore


class LifecycleError(Exception):
    pass


class Lifecycle:
    def __init__(
        self,
        config: Config,
        client: RunPodClient,
        store: StateStore,
        ssh_factory: Callable[[dict[str, Any]], SSHClient] = SSHClient,
    ):
        self.config = config
        self.client = client
        self.store = store
        self.ssh_factory = ssh_factory

    def managed_pod(self) -> dict[str, Any] | None:
        state = self.store.load()
        if state:
            pod = self.client.get_pod(state.pod_id)
            if pod:
                if pod.get("name") != self.config.runpod.pod_name:
                    raise LifecycleError(
                        "state Pod name differs from configuration; inspect before proceeding"
                    )
                if pod.get("image") != self.config.runpod.wrapper_image:
                    raise LifecycleError(
                        "state Pod image differs from configuration; inspect manually"
                    )
                return pod
            self.store.clear()
        matches = self.client.find_managed_pods(self.config.runpod.pod_name)
        if len(matches) > 1:
            raise LifecycleError(
                "multiple Pods have the managed name; resolve manually before continuing"
            )
        if matches:
            pod = matches[0]
            if (
                pod.get("image") != self.config.runpod.wrapper_image
                or pod.get("gpu", {}).get("id") != self.config.runpod.gpu.id
            ):
                raise LifecycleError("same-name Pod has different image or GPU; refusing to adopt")
            self.store.save(
                State.new(
                    str(pod["id"]),
                    self.config.fingerprint(),
                    self.config.runpod.template_id,
                    self.config.runpod.gpu.id,
                )
            )
            return pod
        return None

    def up(self, *, wait: bool = True, progress: Callable[[str], None] = print) -> dict[str, Any]:
        with self.store.lock():
            pod = self.managed_pod()
            if pod:
                state = self.store.load()
                if state and state.config_hash != self.config.fingerprint():
                    raise LifecycleError(
                        "managed Pod uses a different config; inspect `rp status` before reuse"
                    )
                progress(f"Reusing managed Pod {pod['id']}")
            else:
                if self.config.runpod.template_id:
                    self.client.get_template(self.config.runpod.template_id)
                gpu = self.client.get_gpu(
                    self.config.runpod.gpu.id,
                    self.config.runpod.gpu.count,
                    self.config.runpod.cloud,
                    self.config.runpod.gpu.min_cuda_version,
                )
                if gpu.get("availability") == "NONE":
                    raise LifecycleError(
                        f"exact GPU {self.config.runpod.gpu.id} is unavailable in "
                        f"{self.config.runpod.cloud}; no alternate GPU selected"
                    )
                for key in ("B2_POD_APPLICATION_KEY_ID", "B2_POD_APPLICATION_KEY"):
                    if not os.environ.get(key):
                        raise LifecycleError(f"{key} is missing")
                try:
                    verify_pod_b2_key(
                        self.config.storage.bucket,
                        allow_delete=self.config.uses_destructive_sync(),
                    )
                except SecretsError as exc:
                    raise LifecycleError(str(exc)) from exc
                if (
                    not os.environ.get("WORKSPACE_WEB_PASSWORD")
                    or len(os.environ["WORKSPACE_WEB_PASSWORD"]) < 12
                ):
                    raise LifecycleError("WORKSPACE_WEB_PASSWORD must have 12+ characters")
                # Secrets travel in TLS request bodies. The create body contains only references.
                prefix = self.config.runpod.secret_prefix
                self.client.ensure_secret(
                    f"{prefix}_b2_id", os.environ["B2_POD_APPLICATION_KEY_ID"]
                )
                self.client.ensure_secret(f"{prefix}_b2_key", os.environ["B2_POD_APPLICATION_KEY"])
                if os.environ.get("CIVITAI_API_TOKEN"):
                    self.client.ensure_secret(f"{prefix}_civitai", os.environ["CIVITAI_API_TOKEN"])
                if os.environ.get("HF_TOKEN"):
                    self.client.ensure_secret(f"{prefix}_hf", os.environ["HF_TOKEN"])
                if os.environ.get("WORKSPACE_WEB_PASSWORD"):
                    self.client.ensure_secret(
                        f"{prefix}_web_password", os.environ["WORKSPACE_WEB_PASSWORD"]
                    )
                pod = self.client.create_pod(self.config)
                self.store.save(
                    State.new(
                        str(pod["id"]),
                        self.config.fingerprint(),
                        self.config.runpod.template_id,
                        self.config.runpod.gpu.id,
                    )
                )
                progress(f"Created Pod {pod['id']}; state saved")
        if wait:
            return self.wait_ready(str(pod["id"]), progress)
        return pod

    def wait_ready(self, pod_id: str, progress: Callable[[str], None] = print) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.runpod.wait_timeout_seconds
        attempt = 0
        last_report = 0.0
        while time.monotonic() < deadline:
            pod = self.client.get_pod(pod_id)
            if pod is None:
                self.store.clear()
                raise LifecycleError("Pod disappeared while starting")
            status = str(pod.get("status", "UNKNOWN"))
            if status in ("ERROR", "EXITED", "TERMINATED"):
                raise LifecycleError(f"Pod is {status}; it was kept for inspection")
            if status == "RUNNING":
                try:
                    ssh = self.ssh_factory(pod)
                    check = "test -f /workspace/.rp-bootstrap-ready"
                    ports = [18188, 19090]
                    if self.config.application.code_server:
                        ports.append(8443)
                    if self.config.application.filebrowser:
                        ports.append(8081)
                    for port in ports:
                        check += (
                            " && /opt/venv/bin/python -c 'import urllib.request; "
                            f'urllib.request.urlopen("http://127.0.0.1:{port}", timeout=5)\''
                        )
                    if self.config.application.swarmui:
                        check += (
                            " && /opt/venv/bin/python -c 'import urllib.request; "
                            'urllib.request.urlopen("http://127.0.0.1:17801", timeout=5)\''
                        )
                    ssh.run(check, timeout=20)
                    return pod
                except SSHError:
                    pass
            now = time.monotonic()
            if now - last_report >= 30 or attempt == 0:
                progress(f"Pod {pod_id}: {status}; waiting for SSH and B2 bootstrap")
                last_report = now
            time.sleep(min(30, 2 ** min(attempt, 5)))
            attempt += 1
        raise LifecycleError("startup timed out; Pod kept alive. Inspect with rp logs and rp ssh")

    def sync(self, name: str | None = None) -> None:
        with self.store.lock():
            pod = self.managed_pod()
            if not pod:
                raise LifecycleError("no managed Pod")
            if pod.get("status") != "RUNNING":
                raise LifecycleError("Pod is not running")
            if name and name not in [m.name for m in self.config.storage.backup]:
                raise LifecycleError(f"unknown backup mapping: {name}")
            command = "/opt/venv/bin/python /opt/runpod-bootstrap/worker.py backup"
            if name:
                command += f" {name}"
            command += " >> /workspace/.rp-sync.log 2>&1"
            try:
                self.ssh_factory(self.client.get_pod(str(pod["id"])) or pod).run(
                    command, timeout=7200
                )
            except SSHError as exc:
                raise LifecycleError("backup failed; Pod remains alive") from exc

    def down(self, *, force: bool = False, progress: Callable[[str], None] = print) -> None:
        with self.store.lock():
            pod = self.managed_pod()
            if not pod:
                progress("No managed Pod")
                return
            pod_id = str(pod["id"])
            if pod.get("status") == "RUNNING":
                try:
                    progress("Running final B2 backup...")
                    self.ssh_factory(self.client.get_pod(pod_id) or pod).run(
                        "/opt/venv/bin/python /opt/runpod-bootstrap/worker.py backup "
                        ">> /workspace/.rp-sync.log 2>&1",
                        timeout=7200,
                    )
                except SSHError as exc:
                    if not force:
                        raise LifecycleError(
                            "Final backup failed. Pod was NOT terminated. "
                            "Retry `rp sync` or use `rp down --force`."
                        ) from exc
                    progress("FORCE: backup failed; terminating despite unbacked data")
            elif not force:
                raise LifecycleError(
                    "Pod is not running; final backup unavailable. Use --force to delete"
                )
            self.client.delete_pod(pod_id)
            for _ in range(30):
                if self.client.get_pod(pod_id) is None:
                    self.store.clear()
                    progress(f"Terminated Pod {pod_id}")
                    return
                time.sleep(2)
            raise LifecycleError("delete requested but not confirmed; state retained")


def proxy_urls(pod_id: str, ports: list[str]) -> list[str]:
    return [
        f"https://{pod_id}-{item.split('/')[0]}.proxy.runpod.net"
        for item in ports
        if item.endswith("/http")
    ]
