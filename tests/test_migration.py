from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_migration_is_dry_run_and_keeps_secrets_off_argv(tmp_path: Path) -> None:
    fake_rclone = tmp_path / "rclone"
    args_file = tmp_path / "args"
    fake_rclone.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" >> "$RP_TEST_ARGS_FILE"\n')
    fake_rclone.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "RP_TEST_ARGS_FILE": str(args_file),
            "B2_APPLICATION_KEY_ID": "test-id",
            "B2_APPLICATION_KEY": "do-not-print-me",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_b2.py",
            "--source-bucket",
            "oldbucket",
            "--target-bucket",
            "newbucket",
            "--mapping",
            "old-models=models/checkpoints",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "--dry-run" in args_file.read_text()
    assert "--max-size\n4Gi" in args_file.read_text()
    assert "--min-size\n3Gi" in args_file.read_text()
    assert "--disable\nCopy" in args_file.read_text()
    assert "--stats-log-level" in args_file.read_text()
    assert "rp:newbucket/models/checkpoints" in result.stdout
    assert "do-not-print-me" not in result.stdout + result.stderr + args_file.read_text()


def test_migration_rejects_overlapping_prefix(tmp_path: Path) -> None:
    fake_rclone = tmp_path / "rclone"
    fake_rclone.write_text("#!/bin/sh\nexit 0\n")
    fake_rclone.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "B2_APPLICATION_KEY_ID": "test-id",
            "B2_APPLICATION_KEY": "do-not-print-me",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_b2.py",
            "--source-bucket",
            "oldbucket",
            "--target-bucket",
            "oldbucket",
            "--mapping",
            "models=models/checkpoints",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "overlap" in result.stderr


def test_migration_optional_destination_prefix(tmp_path: Path) -> None:
    fake_rclone = tmp_path / "rclone"
    fake_rclone.write_text("#!/bin/sh\nexit 0\n")
    fake_rclone.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "B2_APPLICATION_KEY_ID": "test-id",
            "B2_APPLICATION_KEY": "do-not-print-me",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "scripts/migrate_b2.py",
            "--source-bucket",
            "oldbucket",
            "--target-bucket",
            "newbucket",
            "--target-prefix",
            "workspace-v1",
            "--mapping",
            "old-models=models/checkpoints",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "rp:newbucket/workspace-v1/models/checkpoints" in result.stdout


def test_migration_manifest_is_dry_run_and_preserves_secret_boundary(tmp_path: Path) -> None:
    fake_rclone = tmp_path / "rclone"
    args_file = tmp_path / "args"
    fake_rclone.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" >> "$RP_TEST_ARGS_FILE"\n')
    fake_rclone.chmod(0o755)
    manifest = tmp_path / "migration.yaml"
    manifest.write_text(
        "source_bucket: oldbucket\n"
        "target_bucket: newbucket\n"
        "transfers: 12\n"
        "fast_list: false\n"
        "mappings:\n"
        "  - source: ComfyUI/models\n"
        "    target: models\n"
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "RP_TEST_ARGS_FILE": str(args_file),
            "B2_APPLICATION_KEY_ID": "test-id",
            "B2_APPLICATION_KEY": "do-not-print-me",
        }
    )
    result = subprocess.run(
        [sys.executable, "scripts/migrate_b2.py", "--manifest", str(manifest)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    argv = args_file.read_text()
    assert "--dry-run" in argv
    assert "--fast-list" not in argv
    assert "12" in argv
    assert "rp:newbucket/models" in result.stdout
    assert "do-not-print-me" not in result.stdout + result.stderr + argv


def test_migration_rejects_manifest_with_extra_fields(tmp_path: Path) -> None:
    manifest = tmp_path / "migration.yaml"
    manifest.write_text(
        "source_bucket: oldbucket\n"
        "target_bucket: newbucket\n"
        "secrets: forbidden\n"
        "mappings:\n"
        "  - source: models\n"
        "    target: models\n"
    )
    result = subprocess.run(
        [sys.executable, "scripts/migrate_b2.py", "--manifest", str(manifest)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid migration manifest fields" in result.stderr
