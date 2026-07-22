"""MemoryManager startup degradation tests."""

import builtins
import sys

import pytest

from symbio.memory.manager import MemoryManager, MemoryManagerConfig


@pytest.mark.asyncio
async def test_initialize_degrades_to_memory_mode_when_vector_backend_import_breaks(
    monkeypatch, tmp_path
):
    """Optional vector backend ABI/import failures should not break API startup."""
    real_import = builtins.__import__

    def fail_lancedb_import(name, *args, **kwargs):
        if name == "lancedb":
            raise AttributeError("_ARRAY_API not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_lancedb_import)

    manager = MemoryManager(
        MemoryManagerConfig(
            lancedb_path=str(tmp_path / "memory"),
            enable_decay=False,
        )
    )

    await manager.initialize()

    assert manager._initialized is True
    assert manager._db is None
    assert manager._table is None


@pytest.mark.asyncio
async def test_initialize_suppresses_optional_backend_stderr_noise(monkeypatch, tmp_path, capsys):
    """Noisy optional backend import failures should not flood server startup stderr."""
    real_import = builtins.__import__

    def noisy_lancedb_import(name, *args, **kwargs):
        if name == "lancedb":
            print("RAW OPTIONAL BACKEND TRACEBACK", file=sys.stderr)
            raise AttributeError("_ARRAY_API not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", noisy_lancedb_import)

    manager = MemoryManager(
        MemoryManagerConfig(
            lancedb_path=str(tmp_path / "memory"),
            enable_decay=False,
        )
    )

    await manager.initialize()

    captured = capsys.readouterr()
    assert "RAW OPTIONAL BACKEND TRACEBACK" not in captured.err
