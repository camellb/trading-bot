from pathlib import Path

from engine.tls_bootstrap import persist_ca_bundle


def test_persisted_ca_bundle_survives_source_removal(tmp_path: Path) -> None:
    source = tmp_path / "temporary-pyinstaller" / "cacert.pem"
    source.parent.mkdir()
    source.write_bytes(b"test certificate bundle")
    destination = tmp_path / "app-data" / "data" / "cacert.pem"

    persist_ca_bundle(source, destination)
    source.unlink()

    assert destination.read_bytes() == b"test certificate bundle"
