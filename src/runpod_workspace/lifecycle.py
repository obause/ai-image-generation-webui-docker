"""Pod lifecycle with explicit backup-before-delete invariant."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from runpod_workspace.config import Config
from runpod_workspace.runpod import RunPodClient, RunPodError
from runpod_workspace.secrets import SecretsError, verify_pod_b2_key
from runpod_workspace.ssh import SSHClient, SSHError
from runpod_workspace.state import State, StateStore


class LifecycleError(Exception):
    pass


class GPUUnavailableError(LifecycleError):
    """No permitted GPU is currently available; safe to poll again."""


def new_log_lines(previous: list[str], current: list[str]) -> list[str]:
    """Return only lines after the largest overlap of two rolling log tails."""
    for count in range(min(len(previous), len(current)), 0, -1):
        if previous[-count:] == current[:count]:
            return current[count:]
    return current


def bootstrap_log_line(line: str) -> bool:
    timestamp, separator, message = line.partition(" ")
    if not separator:
        return False
    try:
        datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return message.startswith(
        (
            "Using rclone ",
            "Checking B2 connectivity",
            "Restore ",
            "Transfer progress:",
            "Bootstrap ready",
            "Required restore failed",
            "B2 connectivity failed",
            "rclone exited with status ",
        )
    )


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
                or pod.get("gpu", {}).get("id") not in self.config.runpod.gpu.all_ids()
            ):
                raise LifecycleError("same-name Pod has different image or GPU; refusing to adopt")
            self.store.save(
                State.new(
                    str(pod["id"]),
                    self.config.fingerprint(),
                    self.config.runpod.template_id,
                    str(pod["gpu"]["id"]),
                )
            )
            return pod
        return None

    def up(
        self,
        *,
        wait: bool = True,
        progress: Callable[[str], None] = print,
        only_gpu: str | None = None,
        premium: bool = False,
        confirm_premium: Callable[[dict[str, Any]], bool] | None = None,
        retry_every: int | None = None,
        retry_for: int = 3600,
    ) -> dict[str, Any]:
        if only_gpu and only_gpu not in self.config.runpod.gpu.all_ids():
            raise LifecycleError(
                "--only-gpu must name a GPU in the configured standard or premium list"
            )
        if premium and only_gpu:
            raise LifecycleError("--premium and --only-gpu cannot be combined")
        if retry_every is not None and (not only_gpu or retry_every < 5 or retry_for < retry_every):
            raise LifecycleError("GPU retry requires --only-gpu and a valid interval/timeout")
        deadline = time.monotonic() + retry_for
        first_check = True
        while True:
            with self.store.lock():
                pod = self.managed_pod()
                if pod:
                    state = self.store.load()
                    if state and state.config_hash != self.config.fingerprint():
                        raise LifecycleError(
                            "managed Pod uses a different config; inspect `rp status` before reuse"
                        )
                    running_gpu = (pod.get("gpu") or {}).get("id") or (
                        state.gpu_id if state else None
                    )
                    if only_gpu and running_gpu != only_gpu:
                        raise LifecycleError(
                            f"managed Pod already uses {running_gpu}; run `rp down` before "
                            f"requesting {only_gpu}"
                        )
                    if premium and running_gpu not in self.config.runpod.gpu.premium:
                        raise LifecycleError(
                            f"managed Pod already uses standard GPU {running_gpu}; "
                            "run `rp down` before requesting a premium GPU"
                        )
                    progress(f"Reusing managed Pod {pod['id']}")
                    break
                if self.config.runpod.template_id:
                    self.client.get_template(self.config.runpod.template_id)
                try:
                    selected_gpu = self.select_gpu(
                        progress,
                        only_gpu=only_gpu,
                        premium=premium,
                        confirm_premium=confirm_premium,
                        show_all=first_check,
                    )
                except GPUUnavailableError:
                    if retry_every is None or time.monotonic() >= deadline:
                        raise
                    selected_gpu = None
                if selected_gpu:
                    self._prepare_secrets()
                    pod = self.client.create_pod(self.config, gpu_id=selected_gpu)
                    self.store.save(
                        State.new(
                            str(pod["id"]),
                            self.config.fingerprint(),
                            self.config.runpod.template_id,
                            selected_gpu,
                        )
                    )
                    progress(f"Created Pod {pod['id']}; state saved")
                    break
            first_check = False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GPUUnavailableError(
                    f"{only_gpu} did not become available within {retry_for}s"
                )
            delay = min(retry_every or 0, remaining)
            progress(f"{only_gpu} unavailable; checking again in {delay:.0f}s")
            time.sleep(delay)
        if wait:
            return self.wait_ready(str(pod["id"]), progress)
        return pod

    def _prepare_secrets(self) -> None:
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
        prefix = self.config.runpod.secret_prefix
        self.client.ensure_secret(f"{prefix}_b2_id", os.environ["B2_POD_APPLICATION_KEY_ID"])
        self.client.ensure_secret(f"{prefix}_b2_key", os.environ["B2_POD_APPLICATION_KEY"])
        if os.environ.get("CIVITAI_API_TOKEN"):
            self.client.ensure_secret(f"{prefix}_civitai", os.environ["CIVITAI_API_TOKEN"])
        if os.environ.get("HF_TOKEN"):
            self.client.ensure_secret(f"{prefix}_hf", os.environ["HF_TOKEN"])
        self.client.ensure_secret(f"{prefix}_web_password", os.environ["WORKSPACE_WEB_PASSWORD"])

    def select_gpu(
        self,
        progress: Callable[[str], None] = print,
        *,
        only_gpu: str | None = None,
        premium: bool = False,
        confirm_premium: Callable[[dict[str, Any]], bool] | None = None,
        show_all: bool = True,
    ) -> str:
        """Show catalog availability, then choose within the authorized tier."""
        ids = (
            self.config.runpod.gpu.all_ids()
            if show_all
            else [only_gpu or self.config.runpod.gpu.id]
        )
        catalog: dict[str, dict[str, Any]] = {}
        if show_all:
            progress(f"GPU availability ({self.config.runpod.cloud}):")
        for gpu_id in ids:
            try:
                gpu = self.client.get_gpu(
                    gpu_id,
                    self.config.runpod.gpu.count,
                    self.config.runpod.cloud,
                    self.config.runpod.gpu.min_cuda_version,
                )
            except RunPodError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                gpu = {"id": gpu_id, "availability": "INVALID"}
            catalog[gpu_id] = gpu
            availability = str(gpu.get("availability", "UNKNOWN"))
            price = (gpu.get("price") or {}).get(self.config.runpod.cloud.lower())
            memory = gpu.get("memory")
            tier = "premium" if gpu_id in self.config.runpod.gpu.premium else "standard"
            details = f"{memory} GB VRAM" if isinstance(memory, (int, float)) else "VRAM unknown"
            if isinstance(price, (int, float)):
                details += f", ${price}/GPU-hour"
            progress(f"  {gpu_id} [{tier}]: {availability}; {details}")

        def available(gpu_id: str) -> bool:
            return catalog[gpu_id].get("availability") in ("LOW", "MEDIUM", "HIGH")

        if only_gpu:
            candidates = [only_gpu]
        elif premium:
            candidates = self.config.runpod.gpu.premium
            if not candidates:
                raise LifecycleError("no premium GPUs configured")
        else:
            candidates = self.config.runpod.gpu.ordered_ids()
        selected = next((gpu_id for gpu_id in candidates if available(gpu_id)), None)
        if selected is None and not only_gpu and not premium:
            premium_id = next(
                (gpu_id for gpu_id in self.config.runpod.gpu.premium if available(gpu_id)), None
            )
            if premium_id:
                if confirm_premium is None or not confirm_premium(catalog[premium_id]):
                    raise LifecycleError("Standard GPUs unavailable; premium GPU not approved")
                selected = premium_id
        if selected is None:
            checked = "; ".join(
                f"{gpu_id}: {catalog[gpu_id].get('availability', 'UNKNOWN')}"
                for gpu_id in candidates
            )
            raise GPUUnavailableError(
                f"No requested GPU is available in {self.config.runpod.cloud}: {checked}"
            )
        progress(f"Selected GPU: {selected}")
        return selected

    def wait_ready(self, pod_id: str, progress: Callable[[str], None] = print) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.runpod.wait_timeout_seconds
        attempt = 0
        last_report = 0.0
        previous_logs: list[str] = []
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
                try:
                    current_logs = self.client.log_lines(pod_id, source="container")
                    recent = (
                        new_log_lines(previous_logs, current_logs)
                        if previous_logs
                        else current_logs[-12:]
                    )
                    for line in recent:
                        if bootstrap_log_line(line):
                            progress(line)
                            if (
                                "Required restore failed" in line
                                or "B2 connectivity failed" in line
                            ):
                                raise LifecycleError(
                                    "B2 bootstrap failed; Pod kept alive. Inspect `rp logs` and "
                                    "retry after fixing storage access"
                                )
                    previous_logs = current_logs
                except RunPodError:
                    pass  # SSH and the readiness check remain authoritative.
            now = time.monotonic()
            if now - last_report >= 30 or attempt == 0:
                progress(f"Pod {pod_id}: {status}; waiting for SSH and B2 bootstrap")
                last_report = now
            time.sleep(min(15, 2 ** min(attempt, 4)))
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
