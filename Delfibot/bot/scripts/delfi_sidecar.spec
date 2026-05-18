# PyInstaller spec for the Delfi Python sidecar.
#
# Builds a single-file executable that the Tauri shell launches as
# `delfi-sidecar`. The Tauri side passes `DELFI_PORT` and `DELFI_DB_PATH`
# via the environment; the sidecar prints `DELFI_LOCAL_API_READY <port>`
# to stdout once it is bound and reading.
#
# Run this from `Delfibot/bot/`:
#
#     ../../.venv/bin/pyinstaller scripts/delfi_sidecar.spec --noconfirm
#
# The build script `scripts/build_sidecar.sh` wraps that and renames the
# output to the platform-specific suffix Tauri expects in
# `src-tauri/binaries/`.

# ruff: noqa
# This file is read by PyInstaller's exec(), not imported, so the usual
# linters do not understand the implicit globals (Analysis, PYZ, EXE, ...).

from PyInstaller.utils.hooks import collect_all, collect_submodules

# Packages where PyInstaller's static analysis is unreliable. collect_all
# drags in submodules + data files + binaries. Cheap insurance for
# libraries that load plugins by name at runtime.
bundled_pkgs = [
    # OS keychain. Loads platform backends (macOS keyring, Windows
    # Credential Locker, SecretService on Linux) via importlib at
    # runtime, so static analysis misses them.
    "keyring",

    # SQLAlchemy loads dialect modules by name when create_engine is
    # called with a URL like sqlite:///foo.db.
    "sqlalchemy",

    # APScheduler resolves triggers, executors, and jobstores via entry
    # points (apscheduler.triggers.interval, apscheduler.executors.pool,
    # ...).
    "apscheduler",

    # web3 + eth_account pull in pycryptodome and a tower of ABI / RLP
    # modules that PyInstaller frequently misses.
    "web3",
    "eth_account",
    "eth_keys",
    "eth_utils",
    "eth_abi",
    "eth_typing",

    # py-clob-client-v2 is the Polymarket V2 SDK (post-2026-04-28
    # cutover). Imports order/sign helpers by string in places, so
    # explicit collection avoids "module not found" at runtime.
    "py_clob_client_v2",

    # Google GenAI is a namespace package; the loader walks google.*.
    "google",
    "google.genai",

    # Anthropic SDK has lazy submodule imports.
    "anthropic",

    # HTTP stack. aiohttp ships C extensions; certifi ships the CA
    # bundle as a data file.
    "aiohttp",
    "certifi",

    # yfinance imports pandas, numpy, and a swarm of optional deps. yf
    # also reads packaged JSON config.
    "yfinance",
    "pandas",
    "numpy",

    # Article extraction. lxml has compiled extensions and trafilatura
    # ships trained models as data files.
    "trafilatura",
    "lxml",

    # Feedparser uses sgmllib via dynamic import on some platforms.
    "feedparser",

    # DuckDuckGo search.
    "ddgs",

    # ccxt ships per-exchange JSON market metadata as package data
    # (e.g. ccxt/async_support/okx.py loads markets from
    # ccxt/abi/*.json and similar). Without collect_all, the bundle
    # has the Python modules but NOT those JSON files, and every
    # live_crypto OKX fetch fails inside PyInstaller with
    # `FileNotFoundError(2, 'No such file or directory')`. That
    # fail-silent kills the Bitcoin / Ethereum / Solana price
    # injection into the forecaster's prompt and re-triggers the
    # "research lacks current Bitcoin price" skip storms the
    # 2026-05-16 doctrine rule explicitly bans. ccxt MUST be in
    # this list.
    "ccxt",

    # Native secp256k1 ECDSA. eth_keys uses CoinCurveECCBackend for
    # fast signing if importable; without it, signing falls back to
    # a slower pure-Python backend. The fallback logs noisy "ECC
    # backend not available" warnings on every Polymarket order. We
    # actually want coincurve in the bundle for performance + log
    # cleanliness.
    "coincurve",

    # Polymarket relayer SDKs - used by execution.pm_redeemer for
    # gasless on-chain redemption of winning CTF positions. When the
    # user has pasted Builder API Keys in Settings, the redeemer
    # submits the redeem through Polymarket's relayer instead of
    # broadcasting directly to a Polygon RPC. Polymarket pays the
    # gas, so the user's wallet never needs MATIC. Both SDKs are
    # pure-Python (no native deps).
    "py_builder_relayer_client",
    "py_builder_signing_sdk",
]

datas = []
binaries = []
hiddenimports = []

for pkg in bundled_pkgs:
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Project-internal packages. PyInstaller picks these up via the entry
# point's import graph, but submodules that are imported lazily (e.g.
# the per-archetype evaluators in engine/) are easier to declare
# explicitly than to debug at runtime.
hiddenimports += collect_submodules("db")
hiddenimports += collect_submodules("engine")
hiddenimports += collect_submodules("execution")
hiddenimports += collect_submodules("feeds")
hiddenimports += collect_submodules("research")

# A few extras PyInstaller misses on macOS, surfaced from past builds.
hiddenimports += [
    "pkg_resources.py2_warn",
    "pkg_resources.markers",
    "_cffi_backend",
    "ssl",
]

a = Analysis(
    ["../main.py"],
    pathex=["../"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Tests, dev tooling, and Jupyter cruft that drift in via transitive
    # deps. Excluding them shrinks the bundle by tens of MB.
    excludes=[
        # CRITICAL: aiodns + pycares pull in the c-ares C library
        # which aiohttp auto-detects at import time and uses as its
        # default async DNS resolver. c-ares runs ON the asyncio
        # event loop and has wedged the daemon repeatedly when a
        # single slow DNS lookup hits an internal mutex. By
        # excluding them from the bundle the c library is not even
        # present at runtime, so aiohttp falls back to
        # ThreadedResolver unconditionally. ccxt (which transitively
        # pulled aiodns in) works fine without it — aiodns is
        # OPTIONAL for aiohttp.
        "aiodns",
        "pycares",
        # Other dev/test/dead-weight modules.
        "tkinter",
        "matplotlib",
        "IPython",
        "jupyter",
        "notebook",
        "pytest",
        "_pytest",
        "pyinstaller",
        "PyInstaller",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="delfi-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX shrinks the binary but breaks code signing on macOS
    # notarization.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Hide the console window on Windows. macOS spawns the sidecar as a
    # subprocess of the Tauri shell, so there is no terminal anyway.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
