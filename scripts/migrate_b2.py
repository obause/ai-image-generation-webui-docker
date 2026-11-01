#!/usr/bin/env python3
"""Copy selected legacy B2 paths to the new workspace layout; dry-run by default."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import yaml

from runpod_workspace.secrets import SecretsError, load_local_secrets


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or value in (".", "/") or path.is_absolute() or ".." in path.parts or ":" in value:
        raise argparse.ArgumentTypeError("paths must be relative B2 prefixes")
    return value.strip("/")


def optional_prefix(value: str) -> str:
    return "" if value == "" else safe_path(value)


def safe_bucket(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{5,49}", value):
        raise argparse.ArgumentTypeError("bucket must be a B2 bucket name")
    return value


def read_manifest(
    path: Path, parser: argparse.ArgumentParser
) -> tuple[str, str, str, list[str], int, bool]:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        parser.error("cannot read migration manifest")
    if not isinstance(raw, dict) or set(raw) - {
        "source_bucket", "target_bucket", "target_prefix", "transfers", "fast_list", "mappings"
    }:
        parser.error("invalid migration manifest fields")
    try:
        source = safe_bucket(raw["source_bucket"])
        target = safe_bucket(raw["target_bucket"])
        prefix = optional_prefix(raw.get("target_prefix", ""))
        transfers = raw.get("transfers", 16)
        fast_list = raw.get("fast_list", True)
        mapping_items = raw["mappings"]
    except (KeyError, TypeError, argparse.ArgumentTypeError):
        parser.error("invalid migration manifest values")
    if not isinstance(transfers, int) or isinstance(transfers, bool) or not 1 <= transfers <= 128:
        parser.error("transfers must be between 1 and 128")
    if not isinstance(fast_list, bool):
        parser.error("fast_list must be true or false")
    if not isinstance(mapping_items, list) or not mapping_items:
        parser.error("mappings must be a nonempty list")
    mappings: list[str] = []
    for item in mapping_items:
        if not isinstance(item, dict) or set(item) != {"source", "target"}:
            parser.error("each mapping needs source and target")
        if not isinstance(item["source"], str) or not isinstance(item["target"], str):
            parser.error("mapping paths must be strings")
        mappings.append(f"{item['source']}={item['target']}")
    return source, target, prefix, mappings, transfers, fast_list


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, help="YAML migration plan; exclusive with bucket/mapping flags"
    )
    parser.add_argument("--source-bucket", type=safe_bucket)
    parser.add_argument("--target-bucket", type=safe_bucket)
    parser.add_argument(
        "--target-prefix",
        type=optional_prefix,
        default=None,
        help="Optional prefix in the destination bucket (default: bucket root)",
    )
    parser.add_argument(
        "--mapping",
        action="append",
        metavar="OLD_PATH=NEW_PATH",
        help="Copy one source prefix to a destination prefix (repeatable)",
    )
    parser.add_argument("--execute", action="store_true", help="Actually copy; default is dry-run")
    args = parser.parse_args()
    if args.manifest:
        if (
            args.source_bucket
            or args.target_bucket
            or args.target_prefix is not None
            or args.mapping
        ):
            parser.error("--manifest cannot be combined with bucket or mapping flags")
        source_bucket, target_bucket, target_prefix, raw_mappings, transfers, fast_list = (
            read_manifest(args.manifest, parser)
        )
    else:
        if not args.source_bucket or not args.target_bucket or not args.mapping:
            parser.error("provide --manifest or both buckets and at least one --mapping")
        source_bucket, target_bucket = args.source_bucket, args.target_bucket
        target_prefix, raw_mappings = args.target_prefix or "", args.mapping
        transfers, fast_list = 16, True
    try:
        load_local_secrets()
    except SecretsError as exc:
        parser.error(str(exc))
    for key in ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY"):
        if not os.environ.get(key):
            parser.error(f"{key} is missing")
    if not shutil.which("rclone"):
        parser.error("rclone is required on this machine for migration")
    env = os.environ.copy()
    env.update(
        {
            "RCLONE_CONFIG_RP_TYPE": "b2",
            "RCLONE_CONFIG_RP_ACCOUNT": os.environ["B2_APPLICATION_KEY_ID"],
            "RCLONE_CONFIG_RP_KEY": os.environ["B2_APPLICATION_KEY"],
        }
    )
    mappings: list[tuple[str, str]] = []
    for raw in raw_mappings:
        if "=" not in raw:
            parser.error("each mapping must be OLD_PATH=NEW_PATH")
        old_raw, new_raw = raw.split("=", 1)
        try:
            old, new = safe_path(old_raw), safe_path(new_raw)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
        source = f"rp:{source_bucket}/{old}"
        target_path = "/".join(part for part in (target_prefix, new) if part)
        target = f"rp:{target_bucket}/{target_path}"
        if source == target:
            parser.error("source and destination are identical")
        if source_bucket == target_bucket and (
            target_path.startswith(old + "/") or old.startswith(target_path + "/")
        ):
            parser.error("source and destination prefixes overlap")
        mappings.append((source, target))
    print(f"{'COPY' if args.execute else 'DRY RUN'}: {len(mappings)} mapping(s)", flush=True)
    for source, target in mappings:
        print(f"{source} -> {target}", flush=True)
        command = [
            "rclone",
            "copy",
            source,
            target,
            "--config",
            "/dev/null",
            "--transfers",
            str(transfers),
            "--stats",
            "30s",
            "--stats-log-level",
            "NOTICE",
            "--stats-one-line-date",
            # B2's default 4 GiB server-side copy parts can remain pending for
            # hours. Smaller parts expose progress and bound each API call.
            "--b2-copy-cutoff",
            "512Mi",
            "--retries",
            "2",
            "--low-level-retries",
            "3",
        ]
        if fast_list:
            command.append("--fast-list")
        if not args.execute:
            command.extend(("--dry-run", "--quiet"))
        result = subprocess.run(command, env=env, check=False)
        if result.returncode:
            print(
                "Migration stopped after a failed copy; original B2 data was not deleted",
                file=sys.stderr,
            )
            return result.returncode
    print(
        "Migration completed" if args.execute else "Dry run completed; rerun with --execute to copy"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
