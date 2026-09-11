"""Install a persistent CA bundle before network clients are imported."""

from __future__ import annotations

import os
import platform
import ssl
import tempfile
from pathlib import Path
from typing import Optional

import certifi


_ORIGINAL_CREATE_DEFAULT_CONTEXT = ssl.create_default_context
_CA_PATH: Optional[str] = None


def _app_data_dir() -> Path:
    override = os.environ.get("DELFI_DB_PATH")
    if override:
        return Path(override).expanduser().parent

    home = Path.home()
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        return base / "com.delfi.desktop"
    if system == "Darwin":
        return home / "Library" / "Application Support" / "com.delfi.desktop"
    base = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
    return base / "com.delfi.desktop"


def persist_ca_bundle(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = source.read_bytes()
    if destination.exists() and destination.read_bytes() == source_bytes:
        return destination

    fd, temporary = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=".cacert.",
        suffix=".pem",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(source_bytes)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return destination


def install_persistent_ca_bundle() -> Path:
    global _CA_PATH
    source = Path(certifi.where())
    destination = _app_data_dir() / "data" / "cacert.pem"
    persistent = persist_ca_bundle(source, destination)
    persistent_path = str(persistent)
    _CA_PATH = persistent_path

    os.environ["SSL_CERT_FILE"] = persistent_path
    os.environ["REQUESTS_CA_BUNDLE"] = persistent_path

    def create_default_context(*args, **kwargs):
        context = _ORIGINAL_CREATE_DEFAULT_CONTEXT(*args, **kwargs)
        context.load_verify_locations(cafile=persistent_path)
        return context

    ssl.create_default_context = create_default_context
    return persistent
