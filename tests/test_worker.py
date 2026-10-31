from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("worker", Path("container/worker.py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "READY", tmp_path / "ready")
    monkeypatch.setattr(module, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(module, "LOCK", tmp_path / "backup.lock")
    monkeypatch.setattr(module, "shutil_which", lambda _: "/usr/bin/rclone")
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
    assert "/databases/invokeai.db*" in calls[0]
    assert calls[1][0] == "copyto"


def test_remote_uses_workspace_prefix(worker: ModuleType) -> None:
    assert worker.remote({"bucket": "mybucket", "prefix": "workspace-v1"}, "models") == (
        "rp:mybucket/workspace-v1/models"
    )


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
