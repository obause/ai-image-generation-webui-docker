#!/usr/bin/env python3
"""Pod-side B2 restore, serialized backups, status and deadline."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path("/workspace")
READY = ROOT / ".rp-bootstrap-ready"
STATUS = ROOT / ".rp-sync-status.json"
LOCK = ROOT / ".rp-backup.lock"
RCLONE = Path("/opt/runpod-bootstrap/bin/rclone")


def log(message: str) -> None:
    print(f"{datetime.now(UTC).isoformat()} {message}", flush=True)


def settings() -> dict:
    return json.loads(os.environ["RP_CONFIG_JSON"])


def rclone_env() -> dict[str, str]:
    for key in ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY"):
        if not os.environ.get(key):
            raise RuntimeError(f"{key} is missing")
    env = os.environ.copy()
    env["RCLONE_CONFIG_RP_TYPE"] = "b2"
    env["RCLONE_CONFIG_RP_ACCOUNT"] = os.environ["B2_APPLICATION_KEY_ID"]
    env["RCLONE_CONFIG_RP_KEY"] = os.environ["B2_APPLICATION_KEY"]
    return env


def rclone(args: list[str], cfg: dict) -> bool:
    options = ["--config", "/dev/null", "--transfers", str(cfg["transfers"])]
    if cfg["fast_list"]:
        options.append("--fast-list")
    result = subprocess.run(
        [str(RCLONE), *args, *options],
        env=rclone_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def remote(cfg: dict, name: str) -> str:
    parts = [cfg["bucket"], cfg.get("prefix", "").strip("/"), name.strip("/")]
    return "rp:" + "/".join(part for part in parts if part)


def write_status(name: str, success: bool) -> None:
    try:
        content = json.loads(STATUS.read_text())
    except (OSError, ValueError):
        content = {"mappings": {}}
    content["mappings"][name] = {"success": success, "time": datetime.now(UTC).isoformat()}
    fd, path = tempfile.mkstemp(prefix=".rp-status-", dir=ROOT)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(content, handle)
        os.replace(path, STATUS)
    finally:
        if os.path.exists(path):
            os.unlink(path)


@contextmanager
def backup_lock():
    with LOCK.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def bootstrap() -> int:
    cfg = settings()["storage"]
    if not RCLONE.is_file():
        log("pinned rclone is missing")
        write_status("bootstrap", False)
        return 1
    version = subprocess.run([str(RCLONE), "version"], capture_output=True, text=True)
    if version.returncode or not version.stdout.splitlines():
        log("pinned rclone cannot start")
        write_status("bootstrap", False)
        return 1
    log(f"Using {version.stdout.splitlines()[0]}")
    try:
        rclone_env()
    except RuntimeError as exc:
        log(str(exc))
        write_status("bootstrap", False)
        return 1
    log("Checking B2 connectivity")
    if not rclone(["lsf", f"rp:{cfg['bucket']}", "--max-depth", "1"], cfg):
        log("B2 connectivity failed")
        write_status("bootstrap", False)
        return 1
    failed_required = False
    for mapping in cfg["restore"]:
        Path(mapping["local"]).mkdir(parents=True, exist_ok=True)
        log(f"Restore {mapping['name']} started ({mapping['mode']})")
        success = rclone([mapping["mode"], remote(cfg, mapping["remote"]), mapping["local"]], cfg)
        if success and mapping["name"] == "invokeai":
            db_dir = Path(mapping["local"]) / "databases"
            snapshot = db_dir / "invokeai.snapshot.db"
            if snapshot.is_file():
                shutil.copyfile(snapshot, db_dir / "invokeai.db")
                for suffix in ("-wal", "-shm"):
                    (db_dir / f"invokeai.db{suffix}").unlink(missing_ok=True)
                log("InvokeAI database restored from consistent snapshot")
        if (
            success
            and mapping["required"]
            and not any(path.is_file() for path in Path(mapping["local"]).rglob("*"))
        ):
            log(f"Restore {mapping['name']} found no files in required mapping")
            success = False
        log(f"Restore {mapping['name']} {'completed' if success else 'FAILED'}")
        failed_required |= mapping["required"] and not success
    if failed_required:
        log("Required restore failed; application startup blocked")
        write_status("bootstrap", False)
        return 1
    READY.write_text(datetime.now(UTC).isoformat())
    write_status("bootstrap", True)
    log("Bootstrap ready")
    return 0


def backup(name: str | None = None) -> int:
    cfg = settings()["storage"]
    mappings = [m for m in cfg["backup"] if name is None or m["name"] == name]
    if not mappings:
        log("Unknown backup mapping")
        return 2
    failed = False
    with backup_lock():
        for mapping in mappings:
            source = Path(mapping["local"])
            if not source.is_dir():
                log(f"Backup {mapping['name']} FAILED: local directory missing")
                write_status(mapping["name"], False)
                failed = True
                continue
            log(f"Backup {mapping['name']} started ({mapping['mode']})")
            if mapping["name"] == "invokeai":
                # Copying a live SQLite DB plus WAL is not a consistent backup.
                success = rclone(
                    [
                        mapping["mode"],
                        str(source),
                        remote(cfg, mapping["remote"]),
                        "--exclude",
                        "/databases/invokeai.db*",
                        "--exclude",
                        "/databases/invokeai.snapshot.db",
                    ],
                    cfg,
                )
                database = source / "databases/invokeai.db"
                if success and database.is_file():
                    try:
                        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                            snapshot = Path(temporary) / "invokeai.snapshot.db"
                            with closing(
                                sqlite3.connect(
                                    f"{database.as_uri()}?mode=ro", uri=True, timeout=30
                                )
                            ) as origin:
                                with closing(sqlite3.connect(snapshot)) as target:
                                    origin.backup(target)
                            success = rclone(
                                [
                                    "copyto",
                                    str(snapshot),
                                    remote(
                                        cfg, mapping["remote"] + "/databases/invokeai.snapshot.db"
                                    ),
                                ],
                                cfg,
                            )
                    except (OSError, sqlite3.Error):
                        success = False
            else:
                success = rclone(
                    [mapping["mode"], str(source), remote(cfg, mapping["remote"])], cfg
                )
            write_status(mapping["name"], success)
            log(f"Backup {mapping['name']} {'completed' if success else 'FAILED'}")
            failed |= not success
    return 1 if failed else 0


def request_self_delete() -> bool:
    pod_id = os.environ.get("RUNPOD_POD_ID")
    if not pod_id or not shutil_which("runpodctl"):
        return False
    try:
        result = subprocess.run(
            ["runpodctl", "pod", "delete", pod_id],
            env={**os.environ, "RUNPOD_NO_UPDATE_CHECK": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def loop() -> int:
    cfg = settings()
    deadline_at = datetime.fromisoformat(os.environ["RP_DEADLINE_AT"])
    next_run = {
        m["name"]: time.monotonic() + m["interval_seconds"] for m in cfg["storage"]["backup"]
    }
    while True:
        now = time.monotonic()
        for mapping in cfg["storage"]["backup"]:
            if now >= next_run[mapping["name"]]:
                for attempt in range(3):
                    if backup(mapping["name"]) == 0:
                        break
                    log(f"Backup {mapping['name']} retry {attempt + 1}/3")
                    time.sleep(min(60, 2 ** (attempt + 2)))
                next_run[mapping["name"]] = time.monotonic() + mapping["interval_seconds"]
        if datetime.now(UTC) >= deadline_at:
            log("Auto deadline reached; attempting final backup")
            if backup() == 0:
                if request_self_delete():
                    log("Pod self-termination requested")
                    return 0
                log("Auto deletion failed; Pod remains alive for manual rp down")
            else:
                log("Auto deadline backup failed; Pod remains alive to protect data")
            deadline_at = datetime.now(UTC) + timedelta(seconds=300)
        time.sleep(5)


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "bootstrap":
        sys.exit(bootstrap())
    if action == "backup":
        sys.exit(backup(sys.argv[2] if len(sys.argv) > 2 else None))
    if action == "loop":
        sys.exit(loop())
    log("Unknown action")
    sys.exit(2)
