"""Validated, nonsecret workspace configuration."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

DEFAULT_CONFIG = Path.home() / ".config/runpod-workspace/config.yaml"
_DURATION = re.compile(r"^([1-9][0-9]*)([mhd])$")
_PORT = re.compile(r"^[1-9][0-9]{0,4}/(http|tcp)$")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class ConfigError(Exception):
    """Safe configuration error without YAML input values."""


def duration_seconds(value: str) -> int:
    match = _DURATION.fullmatch(value)
    if not match:
        raise ValueError("duration must be a positive number followed by m, h, or d")
    return int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GPU(StrictModel):
    id: str = Field(min_length=1)
    alternatives: list[str] = Field(default_factory=list)
    premium: list[str] = Field(default_factory=list)
    count: int = Field(default=1, ge=1)
    min_cuda_version: str | None = None

    @field_validator("id")
    @classmethod
    def real_id(cls, value: str) -> str:
        if not value.strip() or value != value.strip() or value.startswith("<"):
            raise ValueError("replace the GPU ID placeholder")
        return value

    @field_validator("alternatives", "premium")
    @classmethod
    def valid_alternatives(cls, values: list[str]) -> list[str]:
        if any(
            not value.strip() or value != value.strip() or value.startswith("<") for value in values
        ):
            raise ValueError("GPU alternatives must be exact catalog IDs")
        if len(values) != len(set(values)):
            raise ValueError("GPU alternatives must be unique")
        return values

    @model_validator(mode="after")
    def no_duplicate_preferred(self) -> GPU:
        all_ids = [self.id, *self.alternatives, *self.premium]
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("GPU IDs must be unique across standard and premium tiers")
        return self

    def ordered_ids(self) -> list[str]:
        return [self.id, *self.alternatives]

    def all_ids(self) -> list[str]:
        return [*self.ordered_ids(), *self.premium]

    @field_validator("min_cuda_version")
    @classmethod
    def valid_cuda(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9]+\.[0-9]+", value):
            raise ValueError("CUDA version must be major.minor")
        return value


class RunPodConfig(StrictModel):
    template_id: str | None = None
    wrapper_image: str = Field(min_length=1)
    pod_name: str = Field(min_length=1)
    gpu: GPU
    cloud: Literal["SECURE", "COMMUNITY"] = "COMMUNITY"
    disk_gb: int = Field(ge=20)
    ports: list[str]
    data_center_ids: list[str] = Field(default_factory=list)
    terminate_after: str = "8h"
    wait_timeout_seconds: int = Field(default=7200, ge=60)
    secret_prefix: str = "rp_workspace"

    @field_validator("wrapper_image")
    @classmethod
    def no_placeholder(cls, value: str) -> str:
        if value.startswith("<"):
            raise ValueError("replace this placeholder")
        return value

    @field_validator("template_id")
    @classmethod
    def valid_template(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value.startswith("<")):
            raise ValueError("set a real template ID or omit it")
        return value

    @field_validator("terminate_after")
    @classmethod
    def valid_duration(cls, value: str) -> str:
        duration_seconds(value)
        return value

    @field_validator("ports")
    @classmethod
    def valid_ports(cls, values: list[str]) -> list[str]:
        if not values or any(
            not _PORT.fullmatch(v) or int(v.split("/")[0]) > 65535 for v in values
        ):
            raise ValueError("ports must be port/http or port/tcp")
        if "22/tcp" not in values:
            raise ValueError("22/tcp is required for direct SSH backup control")
        if len(set(values)) != len(values):
            raise ValueError("duplicate ports")
        return values

    @field_validator("secret_prefix")
    @classmethod
    def valid_prefix(cls, value: str) -> str:
        if not _NAME.fullmatch(value) or value.upper().startswith("RUNPOD"):
            raise ValueError("invalid RunPod secret prefix")
        return value


class RestoreMapping(StrictModel):
    name: str
    remote: str
    local: str
    mode: Literal["copy", "sync"] = "copy"
    required: bool = True
    background: bool = False

    @model_validator(mode="after")
    def background_is_optional_copy(self) -> RestoreMapping:
        if self.background and (self.required or self.mode != "copy"):
            raise ValueError("background restore requires required: false and mode: copy")
        return self


class BackupMapping(StrictModel):
    name: str
    local: str
    remote: str
    mode: Literal["copy", "sync"] = "copy"
    interval_seconds: int = Field(default=300, ge=60)


def _check_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or not path.is_relative_to("/workspace") or ".." in path.parts:
        raise ValueError("local path must be absolute and under /workspace")
    return value


def _check_remote(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or value in ("", ".") or ":" in value:
        raise ValueError("remote path must be a relative B2 bucket path")
    return value


class StorageConfig(StrictModel):
    bucket: str = Field(min_length=1)
    prefix: str = ""
    transfers: int = Field(default=16, ge=1, le=128)
    fast_list: bool = True
    restore: list[RestoreMapping] = Field(min_length=1)
    backup: list[BackupMapping] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_mappings(self) -> StorageConfig:
        if self.bucket.startswith("<"):
            raise ValueError("replace the bucket placeholder")
        if self.prefix:
            _check_remote(self.prefix)
        for mappings in (self.restore, self.backup):
            names = [m.name for m in mappings]
            if len(names) != len(set(names)) or any(not _NAME.fullmatch(n) for n in names):
                raise ValueError("mapping names must be unique simple identifiers")
            for mapping in mappings:
                _check_path(mapping.local)
                _check_remote(mapping.remote)
        return self


class ApplicationConfig(StrictModel):
    original_command: list[str] = Field(default_factory=lambda: ["invokeai-web"], min_length=1)
    swarmui: bool = True
    code_server: bool = True
    filebrowser: bool = True
    shared_models_path: str = "/workspace/models"

    @field_validator("shared_models_path")
    @classmethod
    def valid_model_path(cls, value: str) -> str:
        return _check_path(value)


class Config(StrictModel):
    runpod: RunPodConfig
    storage: StorageConfig
    application: ApplicationConfig = Field(default_factory=ApplicationConfig)

    @model_validator(mode="after")
    def model_restore(self) -> Config:
        if self.application.shared_models_path not in [m.local for m in self.storage.restore]:
            raise ValueError("shared_models_path must have a restore mapping")
        return self

    def fingerprint(self) -> str:
        content = self.model_dump()
        # Preserve fingerprints written before the optional premium tier existed.
        if not self.runpod.gpu.premium:
            content["runpod"]["gpu"].pop("premium")
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]

    def uses_destructive_sync(self) -> bool:
        return any(mapping.mode == "sync" for mapping in self.storage.restore) or any(
            mapping.mode == "sync" for mapping in self.storage.backup
        )

    def container_settings(self) -> dict[str, object]:
        return {
            "storage": self.storage.model_dump(),
            "application": self.application.model_dump(),
        }


def load_config(path: Path | None = None) -> Config:
    actual = path or DEFAULT_CONFIG
    try:
        raw = yaml.safe_load(actual.read_text())
        if not isinstance(raw, dict):
            raise ConfigError("configuration must be a YAML mapping")
        return Config.model_validate(raw)
    except ValidationError as exc:
        fields = [".".join(str(part) for part in e["loc"]) for e in exc.errors()]
        raise ConfigError("invalid configuration fields: " + ", ".join(fields)) from None
    except yaml.YAMLError:
        raise ConfigError("invalid YAML syntax") from None
    except OSError:
        raise ConfigError(f"cannot read configuration at {actual}") from None
