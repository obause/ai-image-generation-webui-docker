from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    monkeypatch.setenv("RP_CONFIG_JSON", "{}")
    monkeypatch.setenv("B2_APPLICATION_KEY_ID", "test-id")
    monkeypatch.setenv("B2_APPLICATION_KEY", "test-secret")
    spec = importlib.util.spec_from_file_location("worker", Path("container/worker.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "READY", tmp_path / "ready")
    monkeypatch.setattr(module, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(module, "LOCK", tmp_path / "backup.lock")
    monkeypatch.setattr(module, "STATUS_LOCK", tmp_path / "status.lock")
    binary = tmp_path / "rclone"
    binary.write_text("#!/bin/sh\necho 'rclone v1.75.2 (test)'\n")
    binary.chmod(0o755)
    monkeypatch.setattr(module, "RCLONE", binary)
    monkeypatch.setattr(module, "shutil_which", lambda _: "/usr/local/bin/runpodctl")
    monkeypatch.setattr(module, "rclone_env", lambda: {})
    monkeypatch.setattr(
        module,
        "settings",
        lambda: {
            "storage": {
                "bucket": "bucket",
                "transfers": 16,
                "fast_list": True,
                "restore": [
                    {
                        "name": "models",
                        "local": str(tmp_path / "models"),
                        "remote": "models",
                        "mode": "copy",
                        "required": True,
                    }
                ],
                "backup": [
                    {
                        "name": "models",
                        "local": str(tmp_path / "models"),
                        "remote": "models",
                        "mode": "copy",
                        "interval_seconds": 300,
                    }
                ],
            }
        },
    )
    return module


def test_required_restore_failure_blocks_ready(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def rclone(args: list[str], _cfg: dict) -> bool:
        calls.append(args)
        return args[0] != "copy"

    monkeypatch.setattr(worker, "rclone", rclone)
    assert worker.bootstrap() == 1
    assert not (tmp_path / "ready").exists()
    assert calls[1][0] == "copy"


def test_backup_copy_and_failure_status(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "models").mkdir()
    calls: list[list[str]] = []

    def rclone(args: list[str], _cfg: dict) -> bool:
        calls.append(args)
        return False

    monkeypatch.setattr(worker, "rclone", rclone)
    assert worker.backup() == 1
    assert calls[0][0] == "copy"
    assert '"success": false' in (tmp_path / "status.json").read_text()


def test_empty_required_restore_blocks_ready(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker, "rclone", lambda _args, _cfg: True)
    assert worker.bootstrap() == 1
    assert not (tmp_path / "ready").exists()


def test_background_restore_does_not_delay_ready(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = worker.settings()
    config["storage"]["restore"].append(
        {
            "name": "images",
            "local": str(tmp_path / "images"),
            "remote": "images",
            "mode": "copy",
            "required": False,
            "background": True,
        }
    )
    monkeypatch.setattr(worker, "settings", lambda: config)
    calls: list[list[str]] = []

    def fake_rclone(args: list[str], _cfg: dict) -> bool:
        calls.append(args)
        if args[0] == "copy" and args[1].endswith("/models"):
            target = tmp_path / "models"
            (target / "checkpoints").mkdir(parents=True, exist_ok=True)
            (target / "checkpoints" / "model.safetensors").write_text("test")
        return True

    monkeypatch.setattr(worker, "rclone", fake_rclone)
    assert worker.bootstrap() == 0
    assert (tmp_path / "ready").exists()
    assert not any(args[1].endswith("/images") for args in calls)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["mappings"]["restore:images"]["state"] == "pending"
    assert worker.background_restore() == 0
    assert calls[-1][-1] == "--ignore-existing"
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["mappings"]["restore:images"]["state"] == "completed"


def test_invokeai_gallery_restores_after_database_and_ready(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = worker.settings()
    invoke_root = tmp_path / "invokeai"
    config["storage"]["restore"].extend(
        [
            {
                "name": "invokeai",
                "local": str(invoke_root),
                "remote": "invokeai",
                "mode": "copy",
                "required": False,
                "exclude": ["/outputs/**"],
            },
            {
                "name": "invokeai-outputs",
                "local": str(invoke_root / "outputs"),
                "remote": "invokeai/outputs",
                "mode": "copy",
                "required": False,
                "background": True,
            },
        ]
    )
    monkeypatch.setattr(worker, "settings", lambda: config)
    calls: list[list[str]] = []

    def fake_rclone(args: list[str], _cfg: dict) -> bool:
        calls.append(args)
        if args[0] != "copy":
            return True
        if args[1].endswith("/models"):
            model = tmp_path / "models/checkpoints/model.safetensors"
            model.parent.mkdir(parents=True)
            model.write_text("model")
        elif args[1].endswith("/invokeai"):
            assert args[-2:] == ["--exclude", "/outputs/**"]
            database = invoke_root / "databases/invokeai.snapshot.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as connection:
                connection.execute("create table models (name text)")
        elif args[1].endswith("/invokeai/outputs"):
            assert (tmp_path / "ready").exists()
            assert (invoke_root / "databases/invokeai.db").is_file()
            assert args[-1] == "--ignore-existing"
        return True

    monkeypatch.setattr(worker, "rclone", fake_rclone)
    assert worker.bootstrap() == 0
    assert (tmp_path / "ready").is_file()
    assert (invoke_root / "databases/invokeai.db").is_file()
    assert not any(args[1].endswith("/invokeai/outputs") for args in calls)
    assert worker.background_restore() == 0
    assert calls[-1][1].endswith("/invokeai/outputs")


def test_progress_logs_only_aggregate_statistics(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    output: list[str] = []
    monkeypatch.setattr(worker, "log", output.append)
    worker._progress(
        json.dumps(
            {
                "stats": {
                    "bytes": 50,
                    "totalBytes": 100,
                    "transfers": 1,
                    "totalTransfers": 2,
                    "speed": 1048576,
                    "errors": 0,
                },
                "message": "secret-model-name",
            }
        )
    )
    worker._progress('{"message":"B2_APPLICATION_KEY=test-secret"}')
    assert len(output) == 1
    assert "50.0%" in output[0]
    assert "1.0 MiB/s" in output[0]
    assert "secret" not in output[0]


def test_rclone_progress_stream_redacts_raw_lines(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "rclone-progress"
    binary.write_text(
        "#!/bin/sh\n"
        'echo \'{"stats":{"bytes":25,"totalBytes":100,"speed":1048576},'
        '"message":"private-name"}\' >&2\n'
        "echo 'secret-key-and-private-path' >&2\n"
    )
    binary.chmod(0o755)
    monkeypatch.setattr(worker, "RCLONE", binary)
    output: list[str] = []
    monkeypatch.setattr(worker, "log", output.append)
    assert worker.rclone(["copy", "source", "target"], {"transfers": 16, "fast_list": True})
    assert any("25.0%" in line for line in output)
    assert not any("private" in line or "secret" in line for line in output)


def test_backup_waits_for_same_mapping_background_restore(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = worker.settings()
    images = {
        "name": "images",
        "local": str(tmp_path / "images"),
        "remote": "images",
        "mode": "copy",
        "required": False,
        "background": True,
    }
    config["storage"]["restore"] = [images]
    config["storage"]["backup"] = [{**images, "interval_seconds": 300}]
    monkeypatch.setattr(worker, "settings", lambda: config)
    (tmp_path / "images").mkdir()
    restoring = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def fake_rclone(args: list[str], _cfg: dict) -> bool:
        if args[-1] == "--ignore-existing":
            calls.append("restore")
            restoring.set()
            assert release.wait(3)
        else:
            calls.append("backup")
        return True

    monkeypatch.setattr(worker, "rclone", fake_rclone)
    restore_thread = threading.Thread(target=worker.background_restore)
    backup_thread = threading.Thread(target=worker.backup, args=("images",))
    restore_thread.start()
    try:
        assert restoring.wait(2)
        backup_thread.start()
        time.sleep(0.05)
        assert calls == ["restore"]
    finally:
        release.set()
        restore_thread.join(3)
        if backup_thread.ident:
            backup_thread.join(3)
    assert calls == ["restore", "backup"]


def test_invokeai_backup_uses_sqlite_snapshot(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    invoke_root = tmp_path / "invokeai"
    database = invoke_root / "databases/invokeai.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute("create table models (name text)")
        connection.execute("insert into models values ('example')")
    original_settings = worker.settings
    config = original_settings()
    config["storage"]["backup"] = [
        {
            "name": "invokeai",
            "local": str(invoke_root),
            "remote": "invokeai",
            "mode": "copy",
            "interval_seconds": 300,
            "exclude": ["/outputs/**"],
        }
    ]
    monkeypatch.setattr(worker, "settings", lambda: config)
    calls: list[list[str]] = []

    def fake_rclone(args: list[str], _cfg: dict) -> bool:
        calls.append(args)
        if args[0] == "copyto":
            with sqlite3.connect(args[1]) as connection:
                assert connection.execute("select name from models").fetchone() == ("example",)
        return True

    monkeypatch.setattr(worker, "rclone", fake_rclone)
    assert worker.backup("invokeai") == 0
    assert calls[0][0] == "copy"
    assert "/outputs/**" in calls[0]
    assert "/databases/invokeai.db*" in calls[0]
    assert calls[1][0] == "copyto"


def test_remote_uses_workspace_prefix(worker: ModuleType) -> None:
    assert worker.remote({"bucket": "mybucket", "prefix": "workspace-v1"}, "models") == (
        "rp:mybucket/workspace-v1/models"
    )


def test_ssh_backup_reads_only_needed_init_environment(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    keys = ("RP_CONFIG_JSON", "B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY")
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    init_env = tmp_path / "init-env"
    init_env.write_bytes(
        b"RP_CONFIG_JSON={}\0B2_APPLICATION_KEY_ID=test-id\0"
        b"B2_APPLICATION_KEY=test-secret\0UNRELATED=ignored\0"
    )
    worker.restore_pod_environment(init_env)
    assert all(os.environ.get(key) for key in keys)
    assert "UNRELATED" not in os.environ


def test_self_delete_uses_current_runpodctl_command(
    worker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNPOD_POD_ID", "pod123")
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    assert worker.request_self_delete() is True
    assert calls == [["runpodctl", "pod", "delete", "pod123"]]
