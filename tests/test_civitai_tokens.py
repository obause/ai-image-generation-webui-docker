"""The Civitai token must apply to both supported domains and no lookalikes."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "civitai_tokens", Path("container/civitai_tokens.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_civitai_token_matches_only_expected_download_hosts() -> None:
    config = json.loads(_module().remote_api_tokens("fake-test-token"))
    assert len(config) == 1
    assert config[0]["token"] == "fake-test-token"
    pattern = config[0]["url_regex"]
    assert re.search(pattern, "https://civitai.com/api/download/models/1")
    assert re.search(pattern, "https://civitai.red/api/download/models/1?fileId=2")
    assert not re.search(pattern, "https://civitai.red.evil.example/api/download/models/1")
    assert not re.search(pattern, "http://civitai.red/api/download/models/1")
    assert not re.search(pattern, "https://other.example/?next=https://civitai.red/")


def test_hugging_face_token_matches_only_its_https_host() -> None:
    config = json.loads(_module().remote_api_tokens(None, "fake-hf-token"))
    assert len(config) == 1
    assert config[0]["token"] == "fake-hf-token"
    pattern = config[0]["url_regex"]
    assert re.search(pattern, "https://huggingface.co/org/model/resolve/main/model.safetensors")
    assert not re.search(pattern, "https://huggingface.co.evil.example/model")
    assert not re.search(pattern, "http://huggingface.co/model")
