#!/usr/bin/env python3
"""Verify each migrated B2 mapping without exposing object names in output."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from migrate_b2 import read_manifest, safe_path

from runpod_workspace.secrets import SecretsError, load_local_secrets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--start", type=int, default=1, help="First 1-based mapping to check")
    parser.add_argument("--end", type=int, help="Last 1-based mapping to check")
    args = parser.parse_args()
    source_bucket, target_bucket, prefix, raw_mappings, transfers, fast_list = read_manifest(
        args.manifest, parser
    )
    end = args.end or len(raw_mappings)
    if not 1 <= args.start <= end <= len(raw_mappings):
        parser.error("mapping range is outside the manifest")
    try:
        load_local_secrets()
    except SecretsError as exc:
        parser.error(str(exc))
    for key in ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY"):
        if not os.environ.get(key):
            parser.error(f"{key} is missing")
    if not shutil.which("rclone"):
        parser.error("rclone is required")
    env = os.environ.copy()
    env.update(
        {
            "RCLONE_CONFIG_RP_TYPE": "b2",
            "RCLONE_CONFIG_RP_ACCOUNT": os.environ["B2_APPLICATION_KEY_ID"],
            "RCLONE_CONFIG_RP_KEY": os.environ["B2_APPLICATION_KEY"],
        }
    )
    failed = False
    for index in range(args.start - 1, end):
        old_raw, new_raw = raw_mappings[index].split("=", 1)
        old, new = safe_path(old_raw), safe_path(new_raw)
        target_path = "/".join(part for part in (prefix, new) if part)
        command = [
            "rclone", "check", f"rp:{source_bucket}/{old}",
            f"rp:{target_bucket}/{target_path}", "--one-way", "--config", "/dev/null",
            "--checkers", str(transfers),
        ]
        if fast_list:
            command.append("--fast-list")
        result = subprocess.run(
            command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False,
        )
        label = "OK" if result.returncode == 0 else "FAILED"
        print(f"Mapping {index + 1}/{len(raw_mappings)}: {label}", flush=True)
        failed |= result.returncode != 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
