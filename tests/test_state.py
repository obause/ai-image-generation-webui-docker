from __future__ import annotations

from pathlib import Path

import pytest

from runpod_workspace.state import State, StateError, StateStore


def test_atomic_save_load_clear(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = State.new("pod123", "hash", "tpl", "gpu")
    with store.lock():
        store.save(state)
        assert store.load() == state
        assert store.path.stat().st_mode & 0o077 == 0
        assert not list(tmp_path.glob(".state-*"))
        store.clear()
        assert store.load() is None


def test_corrupt_state_has_safe_error(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.path.write_text("not-json private-test-value")
    with pytest.raises(StateError) as exc:
        store.load()
    assert "private-test-value" not in str(exc.value)
