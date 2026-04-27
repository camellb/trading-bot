"""Local SQLite engine singleton.

The DB lives under the user's per-platform application data directory:

    macOS    ~/Library/Application Support/Delfi/delfi.db
    Windows  %APPDATA%/Delfi/delfi.db
    Linux    ~/.local/share/Delfi/delfi.db

Override with the DELFI_DB_PATH environment variable (used by tests
and the Tauri sidecar bootstrap, which decides where to store the file
based on the OS-bundle data directory).

Multi-thread access is on (`check_same_thread=False`) because the
aiohttp API and the scheduler run on different threads of the same
process. SQLite WAL mode is enabled so reads don't block writes.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy import event as sa_event

_engine = None


def _default_db_path() -> Path:
    """Per-platform local-data location for `delfi.db`."""
    override = os.environ.get("DELFI_DB_PATH")
    if override:
        return Path(override).expanduser()

    home = Path.home()
    system = platform.system()

    if system == "Windows":
        base = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
        return base / "Delfi" / "delfi.db"

    if system == "Darwin":
        return home / "Library" / "Application Support" / "Delfi" / "delfi.db"

    # Linux / BSD / everything else: XDG data dir.
    base = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
    return base / "Delfi" / "delfi.db"


def app_data_dir() -> Path:
    """Per-platform app-data directory (parent of `delfi.db`).

    Used for any small local file the sidecar wants to persist alongside
    the database (e.g. `data/macro_calendar.json`). The directory is
    created if it does not exist.

    Anchoring relative paths to this directory matters when the sidecar
    is launched by Tauri from `/Applications/Delfi.app`: the GUI launch
    sets cwd=/, and a relative path like `Path("data/foo.json").mkdir()`
    would try to write `/data/foo.json`, which fails with `[Errno 30]
    Read-only file system` and crashes the sidecar before it can bind
    its HTTP port.
    """
    base = _default_db_path().parent
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_engine():
    """Return the lazily-built SQLAlchemy engine for the local SQLite DB."""
    global _engine
    if _engine is not None:
        return _engine

    db_path = _default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    url = f"sqlite:///{db_path}"
    _engine = _sa_create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False},
        future=True,
    )

    # Per-connection PRAGMAs. WAL gives us concurrent readers + one
    # writer; foreign_keys is off by default in SQLite so we turn it
    # on; busy_timeout makes contended writes wait instead of bouncing
    # with SQLITE_BUSY.
    @sa_event.listens_for(_engine, "connect")
    def _set_pragmas(dbapi_conn, _conn_record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return _engine


def reset_engine() -> None:
    """Tear down the cached engine. Used by tests that point DELFI_DB_PATH at a tmp file."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
