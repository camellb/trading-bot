import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from engine import device_id


def _fresh(monkeypatch, raw: str) -> str:
    monkeypatch.setattr(device_id, "_raw_machine_identifier", lambda: raw)
    device_id.get_device_id.cache_clear()
    try:
        return device_id.get_device_id()
    finally:
        device_id.get_device_id.cache_clear()


def test_a_slow_platform_lookup_does_not_change_the_device(tmp_path, monkeypatch) -> None:
    saved = tmp_path / "data" / "device_id"
    monkeypatch.setattr(device_id, "_device_id_file", lambda: saved)

    real = _fresh(monkeypatch, "B1F3E2A8-1234-5678-9ABC-DEF012345678")
    assert saved.read_text(encoding="utf-8") == real

    # The subprocess timed out on this start: same machine, same id.
    assert _fresh(monkeypatch, "delfi-fallback:host|arm64") == real


def test_the_fallback_id_is_never_remembered(tmp_path, monkeypatch) -> None:
    saved = tmp_path / "data" / "device_id"
    monkeypatch.setattr(device_id, "_device_id_file", lambda: saved)
    fallback = _fresh(monkeypatch, "delfi-fallback:host|arm64")
    assert len(fallback) == 32
    assert not saved.exists()


def test_a_real_read_beats_a_copied_file(tmp_path, monkeypatch) -> None:
    saved = tmp_path / "data" / "device_id"
    saved.parent.mkdir(parents=True)
    saved.write_text("a" * 32, encoding="utf-8")  # copied from another machine
    monkeypatch.setattr(device_id, "_device_id_file", lambda: saved)
    real = _fresh(monkeypatch, "OTHER-MACHINE-UUID")
    assert real != "a" * 32
    assert saved.read_text(encoding="utf-8") == real


def test_garbage_in_the_saved_file_is_ignored(tmp_path, monkeypatch) -> None:
    saved = tmp_path / "data" / "device_id"
    saved.parent.mkdir(parents=True)
    saved.write_text("not-a-device-id", encoding="utf-8")
    monkeypatch.setattr(device_id, "_device_id_file", lambda: saved)
    fallback = _fresh(monkeypatch, "delfi-fallback:host|arm64")
    assert fallback != "not-a-device-id" and len(fallback) == 32
