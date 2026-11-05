"""Human-facing rp command."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import typer

from runpod_workspace.config import (
    DEFAULT_CONFIG,
    Config,
    ConfigError,
    duration_seconds,
    load_config,
)
from runpod_workspace.lifecycle import Lifecycle, LifecycleError, proxy_urls
from runpod_workspace.runpod import RunPodClient, RunPodError
from runpod_workspace.secrets import SecretsError, load_local_secrets, verify_pod_b2_key
from runpod_workspace.ssh import (
    SSHClient,
    SSHError,
    can_sign_ssh_identity,
    generate_dedicated_key,
    matching_local_keys,
    prepare_ssh_identity,
)
from runpod_workspace.state import StateError, StateStore

app = typer.Typer(no_args_is_help=True, help="Ephemeral RunPod workspace with B2 persistence")


@app.callback()
def main(
    ctx: typer.Context,
    config: Path = typer.Option(DEFAULT_CONFIG, "--config", help="YAML configuration path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show extra progress"),
) -> None:
    try:
        load_local_secrets()
    except SecretsError as exc:
        _fail(exc)
    ctx.obj = {"config": config, "verbose": verbose}


def _config(ctx: typer.Context) -> Config:
    try:
        return load_config(ctx.obj["config"])
    except ConfigError as exc:
        _fail(exc)
        raise AssertionError("unreachable") from exc


def _service(ctx: typer.Context) -> tuple[Config, RunPodClient, Lifecycle]:
    config = _config(ctx)
    client = RunPodClient()
    try:
        keys_result = client.get_ssh_keys()
    except RunPodError:
        client.http.close()
        raise
    keys = keys_result.get("keys", []) if isinstance(keys_result, dict) else keys_result
    ctx.obj["ssh_identity_ready"] = (
        prepare_ssh_identity(keys)
        if isinstance(keys, list) and all(isinstance(key, str) for key in keys)
        else False
    )
    return config, client, Lifecycle(config, client, StateStore())


def _warn_sync(config: Config) -> None:
    for direction, mappings in (
        ("restore", config.storage.restore),
        ("backup", config.storage.backup),
    ):
        for mapping in mappings:
            if mapping.mode == "sync":
                typer.secho(
                    f"WARNING: {direction} mapping {mapping.name} uses destructive rclone sync",
                    fg=typer.colors.YELLOW,
                    err=True,
                )


def _show_login_hints(config: Config) -> None:
    typer.echo("Generation UIs: direct access (no extra login)")
    if config.application.code_server:
        typer.echo("code-server: password WORKSPACE_WEB_PASSWORD")
    if config.application.filebrowser:
        typer.echo("FileBrowser: username admin; password WORKSPACE_WEB_PASSWORD")


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check local setup and RunPod catalog access without creating resources."""
    ok = True
    python_ok = sys.version_info >= (3, 12)
    typer.echo(
        f"Python/project: {sys.version_info.major}.{sys.version_info.minor} "
        f"({'OK' if python_ok else 'requires 3.12+'})"
    )
    ok &= python_ok
    try:
        config = load_config(ctx.obj["config"])
        typer.echo(f"Config: valid ({ctx.obj['config']})")
        _warn_sync(config)
    except ConfigError as exc:
        typer.echo(f"Config: FAIL ({exc})")
        raise typer.Exit(1) from exc
    required_keys = ["RUNPOD_API_KEY", "B2_POD_APPLICATION_KEY_ID", "B2_POD_APPLICATION_KEY"]
    required_keys.append("WORKSPACE_WEB_PASSWORD")
    for key in required_keys:
        found = bool(os.environ.get(key))
        if key == "WORKSPACE_WEB_PASSWORD":
            found = bool(os.environ.get(key) and len(os.environ[key]) >= 12)
        typer.echo(f"{key}: {'set' if found else 'MISSING'}")
        ok &= found
    if os.environ.get("B2_POD_APPLICATION_KEY_ID") and os.environ.get("B2_POD_APPLICATION_KEY"):
        try:
            destructive = config.uses_destructive_sync()
            verify_pod_b2_key(config.storage.bucket, allow_delete=destructive)
            typer.echo(
                "Pod B2 scope: destination bucket only; "
                + ("delete allowed for explicit sync" if destructive else "no delete capability")
            )
        except SecretsError as exc:
            typer.echo(f"Pod B2 scope: FAIL ({exc})")
            ok = False
    civitai_status = (
        "set (InvokeAI downloads enabled)"
        if os.environ.get("CIVITAI_API_TOKEN")
        else "optional; not set"
    )
    typer.echo(f"CIVITAI_API_TOKEN: {civitai_status}")
    hf_status = (
        "set (Hugging Face downloads enabled)"
        if os.environ.get("HF_TOKEN")
        else "optional; not set"
    )
    typer.echo(f"HF_TOKEN: {hf_status}")
    migration_key = bool(
        os.environ.get("B2_APPLICATION_KEY_ID") and os.environ.get("B2_APPLICATION_KEY")
    )
    typer.echo(f"B2 migration key: {'set' if migration_key else 'optional; not set'}")
    typer.echo(f"SSH client: {'found' if shutil.which('ssh') else 'MISSING'}")
    ok &= bool(shutil.which("ssh"))
    try:
        state = StateStore().load()
    except StateError as exc:
        typer.echo(f"State: FAIL ({exc})")
        raise typer.Exit(1) from exc
    typer.echo(f"State: {state.pod_id if state else 'empty'}")
    if os.environ.get("RUNPOD_API_KEY"):
        try:
            with RunPodClient() as client:
                if config.runpod.template_id:
                    template = client.get_template(config.runpod.template_id)
                    typer.echo(f"Template: found ({template.get('name', 'unnamed')})")
                    typer.echo(f"Template image: {template.get('image', 'unknown')}")
                else:
                    typer.echo("Template: omitted; using wrapper image directly")
                any_available = False
                for gpu_id in config.runpod.gpu.all_ids():
                    try:
                        gpu = client.get_gpu(
                            gpu_id,
                            config.runpod.gpu.count,
                            config.runpod.cloud,
                            config.runpod.gpu.min_cuda_version,
                        )
                    except RunPodError as exc:
                        if "HTTP 404" not in str(exc):
                            raise
                        gpu = {"availability": "INVALID"}
                    availability = str(gpu.get("availability", "unknown"))
                    price = (gpu.get("price") or {}).get(config.runpod.cloud.lower())
                    price_text = f"; ${price}/GPU-hour" if isinstance(price, (int, float)) else ""
                    tier = "premium" if gpu_id in config.runpod.gpu.premium else "standard"
                    typer.echo(f"GPU {gpu_id} ({tier}): {availability}{price_text}")
                    any_available |= availability in ("LOW", "MEDIUM", "HIGH")
                ok &= any_available
                ssh_keys = client.get_ssh_keys()
                keys = ssh_keys.get("keys", ssh_keys) if isinstance(ssh_keys, dict) else ssh_keys
                typer.echo(
                    f"SSH keys: {len(keys) if isinstance(keys, list) else 'unknown'} registered"
                )
                ok &= bool(keys)
                if isinstance(keys, list) and all(isinstance(key, str) for key in keys):
                    matches = matching_local_keys(keys)
                    typer.echo(f"Local SSH keys: {matches} matching registered key(s)")
                    ok &= matches > 0
                if state:
                    pod = client.get_pod(state.pod_id)
                    typer.echo(
                        f"State Pod: {'exists' if pod else 'stale (run rp status to repair)'}"
                    )
        except RunPodError as exc:
            typer.echo(f"RunPod: FAIL ({exc})")
            ok = False
    typer.echo("Auto termination: Pod-side deadline (RunPod native flag unavailable)")
    raise typer.Exit(0 if ok else 1)


@app.command("setup-ssh")
def setup_ssh() -> None:
    """Create a dedicated local SSH key and register its public half with RunPod."""
    try:
        public_key = generate_dedicated_key()
        with RunPodClient() as client:
            client.add_ssh_key(public_key)
            keys_result = client.get_ssh_keys()
            keys = keys_result.get("keys", []) if isinstance(keys_result, dict) else []
            if not prepare_ssh_identity(keys) or not can_sign_ssh_identity():
                raise SSHError("dedicated SSH key registration could not be verified")
        typer.echo("Dedicated RunPod SSH key registered and ready")
    except (RunPodError, SSHError) as exc:
        _fail(exc)


@app.command()
def up(
    ctx: typer.Context,
    no_wait: bool = typer.Option(False, "--no-wait", help="Return after Pod creation"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print plan without API mutation"),
    premium: bool = typer.Option(False, "--premium", help="Choose only configured premium GPUs"),
    only_gpu: str | None = typer.Option(None, "--only-gpu", help="Use one configured exact GPU ID"),
    retry_every: int | None = typer.Option(
        None, "--retry-every", min=5, help="Poll this exact GPU every N seconds"
    ),
    retry_for: str = typer.Option("1h", "--retry-for", help="Maximum GPU polling time, e.g. 2h"),
) -> None:
    """Create or reconcile the workspace, restore B2, and wait for readiness."""
    config = _config(ctx)
    _warn_sync(config)
    if premium and only_gpu:
        _fail(LifecycleError("--premium and --only-gpu cannot be combined"))
    if only_gpu and only_gpu not in config.runpod.gpu.all_ids():
        _fail(LifecycleError("--only-gpu must match a configured standard or premium GPU ID"))
    if retry_every is not None and not only_gpu:
        _fail(LifecycleError("--retry-every requires --only-gpu"))
    try:
        retry_seconds = duration_seconds(retry_for)
    except ValueError:
        _fail(LifecycleError("--retry-for must be a positive duration such as 30m or 2h"))
        raise AssertionError("unreachable") from None
    if retry_every is not None and retry_seconds < retry_every:
        _fail(LifecycleError("--retry-for must be at least --retry-every"))
    if ctx.obj["verbose"]:
        typer.echo(f"Config fingerprint: {config.fingerprint()}")
    if dry_run:
        typer.echo(f"Pod name: {config.runpod.pod_name}")
        typer.echo(f"Template: {config.runpod.template_id or 'none (direct image)'}")
        typer.echo(f"Wrapper image: {config.runpod.wrapper_image}")
        typer.echo(f"Standard GPUs: {', '.join(config.runpod.gpu.ordered_ids())}")
        typer.echo(f"Premium GPUs: {', '.join(config.runpod.gpu.premium) or 'none'}")
        typer.echo(
            f"Selection: {only_gpu or ('premium only' if premium else 'standard then prompt')}"
        )
        if retry_every is not None:
            typer.echo(f"GPU retry: every {retry_every}s for up to {retry_for}")
        typer.echo(f"Cloud: {config.runpod.cloud}")
        typer.echo(f"Ephemeral disk: {config.runpod.disk_gb} GB")
        typer.echo(f"Restore: {', '.join(m.name for m in config.storage.restore)}")
        typer.echo(f"Backup: {', '.join(m.name for m in config.storage.backup)}")
        typer.echo("No resources created")
        return
    try:
        _, client, life = _service(ctx)
        with client:
            if not ctx.obj["ssh_identity_ready"]:
                raise LifecycleError("no matching local SSH key; run `rp setup-ssh`")
            if not can_sign_ssh_identity():
                raise LifecycleError(
                    "matching SSH key cannot sign; run `rp setup-ssh` or unlock the key manager"
                )

            def confirm_premium(gpu: dict[str, Any]) -> bool:
                if not sys.stdin.isatty():
                    raise LifecycleError(
                        "Standard GPUs unavailable; pass --premium to explicitly allow "
                        "the configured premium tier in non-interactive mode"
                    )
                price = (gpu.get("price") or {}).get(config.runpod.cloud.lower())
                cost = f"${price}/GPU-hour" if isinstance(price, (int, float)) else "price unknown"
                return typer.confirm(
                    f"Standard GPUs unavailable. Create a Pod with {gpu.get('id')} "
                    f"({gpu.get('memory', 'unknown')} GB VRAM, {cost})?",
                    default=False,
                )

            pod = life.up(
                wait=not no_wait,
                progress=typer.echo,
                only_gpu=only_gpu,
                premium=premium,
                confirm_premium=confirm_premium,
                retry_every=retry_every,
                retry_for=retry_seconds,
            )
        typer.echo(f"Pod: {pod['id']}")
        saved = life.store.load()
        gpu_id = (pod.get("gpu") or {}).get("id") or (saved.gpu_id if saved else "unknown")
        typer.echo(f"GPU: {gpu_id}")
        typer.echo(f"Bootstrap: {'completed' if not no_wait else 'pending'}")
        for url in proxy_urls(str(pod["id"]), pod.get("ports") or config.runpod.ports):
            typer.echo(f"URL: {url}")
        _show_login_hints(config)
        typer.echo(f"Auto deadline: {config.runpod.terminate_after} (after successful backup)")
    except (LifecycleError, RunPodError, SSHError, StateError) as exc:
        _fail(exc)


def _managed(ctx: typer.Context) -> tuple[Config, RunPodClient, Lifecycle, dict[str, Any]]:
    config, client, life = _service(ctx)
    with life.store.lock():
        pod = life.managed_pod()
    if not pod:
        client.http.close()
        raise LifecycleError("no managed Pod")
    return config, client, life, pod


@app.command()
def status(ctx: typer.Context) -> None:
    """Show Pod, bootstrap, sync and connection status."""
    try:
        config, client, life = _service(ctx)
        with client:
            with life.store.lock():
                pod = life.managed_pod()
            if not pod:
                typer.echo("No managed Pod (stale state repaired if needed)")
                return
            pod = client.get_pod(str(pod["id"])) or pod
            typer.echo(f"Pod: {pod['id']} ({pod.get('status', 'unknown')})")
            saved = life.store.load()
            if saved and saved.config_hash != config.fingerprint():
                typer.echo("Config: changed since Pod creation")
            typer.echo(f"GPU: {pod.get('gpu', {}).get('id', config.runpod.gpu.id)}")
            runtime = pod.get("runtime")
            uptime = runtime.get("uptime", "unknown") if runtime else "unknown"
            typer.echo(f"Runtime: {uptime} seconds")
            typer.echo(f"Auto deadline: {config.runpod.terminate_after}")
            for url in proxy_urls(str(pod["id"]), pod.get("ports") or config.runpod.ports):
                typer.echo(f"URL: {url}")
            _show_login_hints(config)
            try:
                ssh = SSHClient(pod)
                result = ssh.run(
                    "test -f /workspace/.rp-bootstrap-ready && cat /workspace/.rp-sync-status.json",
                    timeout=15,
                )
                typer.echo("Bootstrap: ready; SSH: ready")
                typer.echo(f"Sync: {result}")
            except SSHError:
                typer.echo("Bootstrap/SSH/sync: pending or unavailable")
    except (LifecycleError, RunPodError, SSHError, StateError) as exc:
        _fail(exc)


@app.command("ssh")
def ssh_command(
    ctx: typer.Context,
    command: str | None = typer.Option(None, "--command", help="Remote shell command"),
) -> None:
    """Open SSH or run one remote command."""
    try:
        _, client, _, pod = _managed(ctx)
        with client:
            ssh = SSHClient(client.get_pod(str(pod["id"])) or pod)
            code = ssh.stream(command or "bash -l", interactive=command is None)
            raise typer.Exit(code)
    except (LifecycleError, RunPodError, SSHError, StateError) as exc:
        _fail(exc)


@app.command()
def logs(
    ctx: typer.Context,
    follow: bool = typer.Option(False, "--follow", "-f"),
) -> None:
    """Read bootstrap, background restore, and periodic backup logs from the Pod."""
    try:
        _, client, _, pod = _managed(ctx)
        with client:
            current = client.get_pod(str(pod["id"])) or pod
            try:
                ssh = SSHClient(current)
                ssh.run("test -r /workspace/.rp-bootstrap.log", timeout=10)
            except SSHError:
                typer.echo("SSH unavailable; reading Pod container logs from RunPod")
                for line in client.log_lines(str(pod["id"]), follow=follow, source=None):
                    typer.echo(line)
                return
            cmd = (
                "tail "
                + ("-n 100 -F" if follow else "-n 100")
                + " /workspace/.rp-bootstrap.log /workspace/.rp-restore.log /workspace/.rp-sync.log"
            )
            raise typer.Exit(ssh.stream(cmd))
    except (LifecycleError, RunPodError, SSHError, StateError) as exc:
        _fail(exc)


@app.command()
def sync(ctx: typer.Context, name: str | None = typer.Argument(None)) -> None:
    """Back up all mappings or one named mapping immediately."""
    try:
        _, client, life = _service(ctx)
        with client:
            life.sync(name)
        typer.echo("B2 backup completed")
    except (LifecycleError, RunPodError, SSHError, StateError) as exc:
        _fail(exc)


@app.command()
def down(ctx: typer.Context, force: bool = typer.Option(False, "--force")) -> None:
    """Back up and terminate; --force permits termination after backup failure."""
    if force:
        typer.secho("FORCE: Pod may be deleted with unsaved data", fg=typer.colors.RED, err=True)
    try:
        _, client, life = _service(ctx)
        with client:
            life.down(force=force)
    except (LifecycleError, RunPodError, SSHError, StateError) as exc:
        _fail(exc)


def _fail(exc: Exception) -> None:
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    raise typer.Exit(1) from exc
