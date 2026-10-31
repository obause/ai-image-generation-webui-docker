#!/usr/bin/env python3
"""List B2 directory prefixes read-only to prepare a migration map."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess

from runpod_workspace.secrets import SecretsError, load_local_secrets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bucket", help="B2 bucket to inspect")
    parser.add_argument("--max-depth", type=int, default=3)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{5,49}", args.bucket):
        parser.error("bucket must be a B2 bucket name")
    if not 1 <= args.max_depth <= 8:
        parser.error("max depth must be between 1 and 8")
    try:
        load_local_secrets()
    except SecretsError as exc:
        parser.error(str(exc))
    for key in ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY"):
        if not os.environ.get(key):
            parser.error(f"{key} is missing")
    if not shutil.which("rclone"):
        parser.error("rclone is required to inspect B2")
    env = os.environ.copy()
    env.update(
        {
            "RCLONE_CONFIG_RP_TYPE": "b2",
            "RCLONE_CONFIG_RP_ACCOUNT": os.environ["B2_APPLICATION_KEY_ID"],
            "RCLONE_CONFIG_RP_KEY": os.environ["B2_APPLICATION_KEY"],
        }
    )
    result = subprocess.run(
        [
            "rclone",
            "lsf",
            f"rp:{args.bucket}",
            "--dirs-only",
            "--recursive",
            "--max-depth",
            str(args.max_depth),
            "--config",
            "/dev/null",
        ],
        env=env,
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
