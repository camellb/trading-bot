"""
Polymarket wallet helpers.

Query the user's on-chain USDC-equivalent balance on Polygon so the
dashboard can show the real bankroll when the bot is in live mode.
Polymarket runs on Polygon (chain id 137); balances live in the user's
wallet address which is stored in user_config.wallet_address.

After the 2026-04-28 V2 exchange upgrade, Polymarket's collateral token
is pUSD - an ERC-20 wrapper that represents a 1:1 USDC claim and uses
the same 6-decimal precision. Pre-migration users still hold native
USDC or bridged USDC.e until their first V2 trade triggers the
Collateral Onramp wrap. We query all three tokens and sum so a wallet
shows the right number whether the user has migrated, is mid-migration,
or hasn't started:

    pUSD            0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB  (V2 collateral)
    native USDC     0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359
    bridged USDC.e  0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174

All three are 6-decimal, 1:1 USD-pegged ERC-20 on Polygon, so summing
their balances and dividing by 1e6 yields a single bankroll figure.

Balances are cached per wallet for 60s so a dashboard refresh loop
doesn't hammer public RPC.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Dict, Optional, Tuple

import aiohttp


# Public Polygon RPC fallbacks. Tried in order until one returns a
# usable result. The legacy single-URL constant POLYGON_RPC_URL stays
# as the FIRST entry for backward-compat with code/tests that reach
# for one URL. New code should iterate POLYGON_RPC_URLS. Same set as
# execution.pm_redeemer's _DEFAULT_RPC_URLS, kept independent to avoid
# a cross-module import cycle (pm_redeemer imports this file).
POLYGON_RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
_env_rpc_urls = os.environ.get("POLYGON_RPC_URLS")
if _env_rpc_urls:
    POLYGON_RPC_URLS = [u.strip() for u in _env_rpc_urls.split(",") if u.strip()]
else:
    POLYGON_RPC_URLS = [
        POLYGON_RPC_URL,  # honor any single-URL env override at index 0
        "https://polygon-bor-rpc.publicnode.com",
        "https://1rpc.io/matic",
        "https://polygon.drpc.org",
        "https://polygon.llamarpc.com",
        "https://rpc.ankr.com/polygon",
    ]

# USDC-equivalent collateral contracts on Polygon.
#
# pUSD is the active collateral after Polymarket's 2026-04-28 V2 exchange
# upgrade. Native USDC and bridged USDC.e still appear in wallets that
# haven't migrated yet (the Collateral Onramp wraps to pUSD on first V2
# trade, not on schedule). Querying all three and summing means we read
# the right bankroll regardless of where the user is in the migration.
#
# All three are 6-decimal, 1:1 USD-pegged. If Polymarket adds another
# collateral surface, append it here.
_COLLATERAL_CONTRACTS: Tuple[str, ...] = (
    "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",  # pUSD (V2 collateral)
    "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # native USDC
    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # bridged USDC.e
)

# Back-compat alias: prior code referenced `_USDC_CONTRACTS`. Keep a
# pointer so any forgotten in-tree caller doesn't break; new code reads
# `_COLLATERAL_CONTRACTS`.
_USDC_CONTRACTS = _COLLATERAL_CONTRACTS

# ERC-20 balanceOf(address) selector
_BALANCE_OF_SELECTOR = "0x70a08231"

# All three collateral tokens are 6 decimals on Polygon.
_USDC_DECIMALS = 6

# Cache: wallet_lower -> (balance_usd, monotonic_ts)
_CACHE: Dict[str, Tuple[float, float]] = {}
_CACHE_TTL_SECONDS = 60.0


def _encode_balance_of_call(wallet_address: str) -> str:
    """Build the eth_call data field for ERC-20 balanceOf(wallet)."""
    wallet = wallet_address.lower().replace("0x", "")
    padded = wallet.rjust(64, "0")
    return _BALANCE_OF_SELECTOR + padded


async def _rpc_balance_of(
    session: aiohttp.ClientSession,
    contract: str,
    data: str,
) -> Optional[int]:
    """
    One balanceOf eth_call. Returns the raw uint256 token balance, or
    None if the RPC fails. Caller sums across contracts.
    """
    payload = {
        "jsonrpc": "2.0",
        "method":  "eth_call",
        "params":  [{"to": contract, "data": data}, "latest"],
        "id":      1,
    }
    try:
        async with session.post(
            POLYGON_RPC_URL,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            body = await resp.json()
            raw = body.get("result")
            if not isinstance(raw, str) or not raw.startswith("0x"):
                return None
            return int(raw, 16)
    except Exception as exc:
        print(
            f"[polymarket_wallet] eth_call to {contract} failed: {exc}",
            file=sys.stderr,
        )
        return None


async def get_live_usdc_balance(wallet_address: str) -> Optional[float]:
    """
    Sum USDC + USDC.e balances for the given wallet on Polygon.

    Returns the total in USD on success, or None on failure so callers
    can fall through to a DB-derived bankroll instead of pretending the
    wallet is empty. A 60s in-memory cache avoids hammering public RPC.
    """
    if not wallet_address or not isinstance(wallet_address, str):
        return None
    wallet = wallet_address.strip()
    # Basic sanity: "0x" + 40 hex chars = 42 chars total
    if not wallet.startswith("0x") or len(wallet) != 42:
        return None
    cache_key = wallet.lower()

    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached is not None:
        bal, ts = cached
        if now - ts < _CACHE_TTL_SECONDS:
            return bal

    data = _encode_balance_of_call(wallet)
    try:
        async with aiohttp.ClientSession() as session:
            results = await asyncio.gather(*[
                _rpc_balance_of(session, contract, data)
                for contract in _COLLATERAL_CONTRACTS
            ])
    except Exception as exc:
        print(
            f"[polymarket_wallet] balance fetch session failed: {exc}",
            file=sys.stderr,
        )
        return None

    # If every contract call failed the wallet might be unreachable or
    # the RPC is down. Returning None lets the dashboard fall back to
    # the DB-derived bankroll rather than flashing $0.
    if all(r is None for r in results):
        return None

    raw_total = sum((r or 0) for r in results)
    balance = raw_total / (10 ** _USDC_DECIMALS)
    _CACHE[cache_key] = (balance, now)
    return float(balance)


def clear_cache() -> None:
    """Dump every per-key cache in this module. Useful for tests and
    for forcing a re-probe after the user funds their Polymarket
    account (the 5-min sig-type cache would otherwise hold stale
    'balance=0' info)."""
    _CACHE.clear()
    _CLOB_BALANCE_CACHE.clear()
    _POLY_SIGNER_CACHE.clear()
    # Also flush the CLOB client cache - a fresh credential rotation
    # invalidates the api-key bound inside the cached client.
    _CLOB_CLIENT_CACHE.clear()


def invalidate_signer_cache(private_key: Optional[str]) -> None:
    """Remove one key's entry from the signer-info cache.

    Call this immediately after placing a live order so the next
    order attempt in the same scan re-probes the on-chain balance
    rather than reading the pre-order (higher) cached value. Also
    clears _CLOB_BALANCE_CACHE entirely since it is keyed by derived
    wallet address (which we don't have here) and has a short 60s
    TTL anyway.
    """
    if not private_key or not isinstance(private_key, str):
        return
    import hashlib
    cache_key = hashlib.sha256(private_key.encode("utf-8")).hexdigest()[:16]
    _POLY_SIGNER_CACHE.pop(cache_key, None)
    _CLOB_BALANCE_CACHE.clear()


# ── CLOB-side balance (authoritative for live trading) ──────────────────────
#
# Polymarket users have one of three account shapes, all signed by
# the same EOA private key but holding funds at different on-chain
# addresses:
#
#   signature_type=0  EOA               (MetaMask-connect users)
#   signature_type=1  POLY_PROXY        (the classic Polymarket Magic
#                                        proxy contract — default for
#                                        anyone who signed up via the
#                                        Polymarket UI)
#   signature_type=2  POLY_GNOSIS_SAFE  (newer Gnosis-Safe magic
#                                        accounts)
#
# We don't know up-front which one applies. So we PROBE: call
# /balance-allowance once with each signature_type, pick the one
# that reports a non-zero balance. The result is cached for 5
# minutes; that's long enough to skip re-probing on every
# Dashboard poll but short enough that a fresh deposit shows up
# quickly. The bot's order-placement path reads the same cache so
# orders are signed with the correct signature_type — otherwise
# the CLOB would reject every order with "insufficient collateral"
# even though the user has funds at their proxy.

_CLOB_BALANCE_CACHE: Dict[str, Tuple[float, float]] = {}
_CLOB_BALANCE_TTL_SECONDS = 60.0

# Cache for the data-api positions sum (per funder).
# Used by pm_executor.get_portfolio_stats to compute Locked Capital
# from the authoritative Polymarket source (= every position the
# wallet holds, including manual trades the bot didn't open). 60s
# TTL is enough that the Dashboard's 5s poll doesn't hammer the
# data-api endpoint.
# Cached aggregates from data-api /positions. Tuple:
#   (currentValue_sum, cashPnl_sum, fetched_at)
# currentValue_sum drives "Locked capital" / equity (every position
# the wallet holds, including manual trades).
# cashPnl_sum drives unrealized P&L so the Dashboard's number
# matches Polymarket's portfolio P&L to the cent (their accounting
# uses mid-price + relayer fees, both bundled into cashPnl). Without
# this the bot's local "currentValue - cost_usd" computation
# overstates unrealized by the relayer-fee delta and the bid/ask
# spread.
_POSITIONS_VALUE_CACHE: Dict[str, Tuple[float, float, float]] = {}
_POSITIONS_VALUE_TTL_SECONDS = 60.0
# Single-flight lock for get_total_open_positions_value (defined later
# next to its sibling _POLY_SIGNER_LOCK after the threading import).
_POSITIONS_VALUE_LOCK = None  # type: ignore[assignment]

_POLY_SIGNER_CACHE: Dict[str, Tuple[Optional[dict], float]] = {}
_POLY_SIGNER_TTL_SECONDS = 300.0  # 5 minutes

# Cache of fully-built CLOB clients keyed by (pk_hash, sig_type, funder,
# manual). Polymarket's `create_or_derive_api_key` POST is the dominant
# cost in `_build_clob_client` (one round-trip, sometimes 2 on
# auto-derive retries), and on accounts where the auto-derive flow
# returns HTTP 400 it stacks SSL reads that hold the GIL across many
# concurrent /api/summary calls. Cache forever in-process; the client
# is stateless other than its api-key bundle, which is itself stable
# under a fixed (sig_type, funder). On rotation we clear the entry.
_CLOB_CLIENT_CACHE: dict = {}

# Serializes the full signer probe so concurrent /api/summary calls
# do not all race past a cache miss and run the (expensive) probe in
# parallel. The probe makes 4 separate /balance-allowance round-trips
# plus one create_or_derive_api_key POST. Without this lock, the
# first slow probe holds threadpool slots while the second through
# N-th probes start fresh, multiplying the work and saturating the
# Python GIL (every thread re-entering Python after its SSL read
# contends for the GIL, the asyncio main loop starves, and
# `accept()` on the listener socket falls behind - the user sees
# "/api/state: timed out after 30s" until the loop watchdog
# SIGKILLs the daemon).
import threading as _threading
_POLY_SIGNER_LOCK = _threading.Lock()
# Same single-flight pattern for the data-api positions probe.
# Without this, a 7-endpoint cold burst fires 7 parallel data-api
# HTTPS calls with 8s timeout each, saturating the executor pool.
_POSITIONS_VALUE_LOCK = _threading.Lock()


# Process-level tracking of which (sig_type, funder) tuple we've
# rotated the api-key under. Polymarket binds each api-key to the
# (signer, funder) context it was created in; if we switch funder
# (e.g. user goes through the V2 migration POLY_PROXY → DepositWallet)
# and keep the OLD api-key, orders get rejected with "the order
# signer address has to be the address of the API KEY". Force-rotate
# once per context change so subsequent orders pass.
_API_KEY_ROTATED_CTX: dict = {}


def _build_clob_client(
    private_key: str,
    signature_type: int = -1,
    funder: Optional[str] = None,
    rotate_api_key: bool = False,
):
    """Two-step construction per the py-clob-client-v2 SDK: derive an
    api-key with the signing key first, then build the fully-authed
    client. Helpers in this module use it; pm_executor builds its
    own cached version for order placement.

    MANUAL API CREDS SHORT-CIRCUIT
        When the user has pasted Polymarket Trading API Keys via
        Settings -> Polymarket API Key, we use them directly and skip
        the SDK's `create_or_derive_api_key()` POST entirely. Without
        this short-circuit, every probe would hit `/auth/api-key`,
        and when the auto-derive path is broken (returns 400 "Could
        not create api key" - typical post-V2-migration accounts
        where the deposit-wallet signature doesn't match the auto-
        derive's signer context), each retry blocks the GIL for the
        full SSL round-trip. The GUI polls `/api/summary` every 5s
        which triggers this probe; stacked probes saturate the
        thread pool, the asyncio event loop can't service `accept()`,
        and every HTTP endpoint times out. The user sees
        "/api/state: timed out after 30s" until the loop watchdog
        SIGKILLs the daemon. Honoring manual creds breaks the loop
        at the source: no failing POST, no GIL contention, no wedge.

    `rotate_api_key=True` deletes the existing api-key (if any) and
    creates a fresh one under the current (sig_type, funder)
    context. Set this when the caller knows the api-key context
    changed (e.g. post-migration). The rotation runs at most once
    per process per (sig_type, funder) tuple — repeated calls with
    the same tuple are no-ops. Skipped entirely when manual creds
    are present (the user has already committed to a specific
    api-key; rotation would invalidate it).
    """
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds  # type: ignore
    CLOB_HOST = "https://clob.polymarket.com"
    POLYGON_CHAIN_ID = 137
    seed_kwargs = dict(host=CLOB_HOST, chain_id=POLYGON_CHAIN_ID, key=private_key)
    if signature_type != -1:
        seed_kwargs["signature_type"] = signature_type
        if funder:
            seed_kwargs["funder"] = funder

    # MANUAL api-key short-circuit. Pull once per call; the underlying
    # secrets store has its own in-process cache so this is cheap.
    manual_creds = None
    try:
        from engine.user_config import get_polymarket_api_creds
        manual_creds = get_polymarket_api_creds()
    except Exception:
        manual_creds = None

    # Cache key: pk_hash + sig_type + funder + manual-vs-auto. We do NOT
    # include rotate_api_key - the cache only stores clients built AFTER
    # rotation (if any), so on a future call with rotate_api_key=True we
    # bypass the cache and rebuild, then cache the new one. Rotation is
    # one-shot per (sig_type, funder) tuple anyway via _API_KEY_ROTATED_CTX.
    import hashlib
    pk_hash = hashlib.sha256(private_key.encode("utf-8")).hexdigest()[:16]
    cache_tag = "m" if manual_creds else "a"
    cache_key = (pk_hash, signature_type, (funder or "").lower(), cache_tag)
    if not rotate_api_key:
        cached = _CLOB_CLIENT_CACHE.get(cache_key)
        if cached is not None:
            return cached

    if manual_creds:
        creds = ApiCreds(
            api_key=manual_creds["api_key"],
            api_secret=manual_creds["api_secret"],
            api_passphrase=manual_creds["api_passphrase"],
        )
        client_kwargs = dict(seed_kwargs)
        client_kwargs["creds"] = creds
        client = ClobClient(**client_kwargs)
        _CLOB_CLIENT_CACHE[cache_key] = client
        return client

    seed = ClobClient(**seed_kwargs)

    # Optional one-time api-key rotation. Gated by an in-process
    # cache so we don't churn the api-key on every probe.
    import hashlib
    pk_hash = hashlib.sha256(private_key.encode("utf-8")).hexdigest()[:16]
    rotate_key = (pk_hash, signature_type, (funder or "").lower())
    if rotate_api_key and rotate_key not in _API_KEY_ROTATED_CTX:
        try:
            # Derive whatever key is currently on file, use it to delete.
            existing = seed.derive_api_key()
            if existing and getattr(existing, "api_key", None):
                authed = ClobClient(**{**seed_kwargs, "creds": existing})
                try:
                    authed.delete_api_key()
                    print(
                        f"[polymarket_wallet] rotated api-key for "
                        f"sig_type={signature_type} funder={funder}",
                        file=sys.stderr,
                    )
                except Exception as del_exc:
                    print(
                        f"[polymarket_wallet] api-key delete failed "
                        f"(continuing): {del_exc}",
                        file=sys.stderr,
                    )
            _API_KEY_ROTATED_CTX[rotate_key] = True
        except Exception as exc:
            # Couldn't even derive the existing key — nothing to
            # rotate. Mark as "tried" so we don't loop.
            _API_KEY_ROTATED_CTX[rotate_key] = True
            print(
                f"[polymarket_wallet] api-key rotation skipped: {exc}",
                file=sys.stderr,
            )

    creds = seed.create_or_derive_api_key()
    client_kwargs = dict(seed_kwargs)
    client_kwargs["creds"] = creds
    client = ClobClient(**client_kwargs)
    _CLOB_CLIENT_CACHE[cache_key] = client
    return client


def _derive_poly_proxy(eoa: str) -> str:
    """Derive the user's POLY_PROXY (sig_type=1) address from their EOA.

    Polymarket's ProxyWalletFactory deploys a CREATE2 clone for each
    EOA on first deposit. The proxy is the address that actually
    holds the user's pUSD; it's what gets set as the `maker` field
    of every order. Without using this (and signing with the EOA),
    the CLOB rejects orders with "maker address not allowed".

    Constants extracted from the Polymarket V2 web bundle (chunk
    012oy0ftdrorf.js, deriveProxyWallet function).
    """
    from eth_utils import keccak, to_checksum_address
    PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
    PROXY_INIT_CODE_HASH = "0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b"
    # salt = keccak256(packed(eoa)) — 20 raw bytes, NOT abi-encoded.
    eoa_packed = bytes.fromhex(eoa.lower().replace("0x", ""))
    salt = keccak(eoa_packed)
    factory_bytes = bytes.fromhex(PROXY_FACTORY.lower().replace("0x", ""))
    init_code_hash = bytes.fromhex(PROXY_INIT_CODE_HASH.replace("0x", ""))
    addr = keccak(b"\xff" + factory_bytes + salt + init_code_hash)[12:]
    return to_checksum_address(addr)


def _derive_deposit_wallet(eoa: str) -> str:
    """Derive the V2 Polymarket DepositWallet address for this EOA.

    Polymarket's 2026-04-28 V2 upgrade introduced a new account type
    ("DepositWallet") that users get migrated to on first V2 trade.
    Their UI shows the prompt "you need to upgrade to continue
    trading"; after the user accepts, post-migration orders MUST use
    this address as their maker.

    Derivation ported from polymarket.com's frontend bundle
    (chunk 012oy0ftdrorf.js, deriveDepositWallet function). It's a
    Solady CWIA (Clone With Immutable Args) CREATE2 with the EOA
    encoded into the deployment salt + initcode.
    """
    from eth_utils import keccak, to_checksum_address
    from eth_abi import encode
    FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
    IMPL    = "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB"
    # salt = keccak256(abi.encode(factory, padded_eoa))
    eoa_padded = bytes.fromhex(eoa.lower().replace("0x", "").rjust(64, "0"))
    d = encode(["address", "bytes32"], [FACTORY, eoa_padded])
    salt = keccak(d)
    # initcode = Solady CWIA prefix + impl + tail + d (immutable args)
    c = len(d)  # = 64
    header10 = (0x61003d3d8160233d3973 + (c << 56)).to_bytes(10, "big")
    impl_bytes = bytes.fromhex(IMPL.lower().replace("0x", ""))
    mid1 = bytes.fromhex("6009")
    mid2 = bytes.fromhex("5155f3363d3d373d3d363d7f360894a13ba1a3210667c828492db98dca3e2076")
    mid3 = bytes.fromhex("cc3735a920a3ca505d382bbc545af43d6000803e6038573d6000fd5b3d6000f3")
    initcode = header10 + impl_bytes + mid1 + mid2 + mid3 + d
    bytecode_hash = keccak(initcode)
    factory_bytes = bytes.fromhex(FACTORY.lower().replace("0x", ""))
    addr = keccak(b"\xff" + factory_bytes + salt + bytecode_hash)[12:]
    return to_checksum_address(addr)


def _derive_poly_safe(eoa: str) -> str:
    """Derive the user's POLY_GNOSIS_SAFE (sig_type=2) address from their EOA.

    Newer Polymarket Magic accounts use a Gnosis Safe instead of the
    ProxyWallet. Same CREATE2 idea, different factory + init code
    hash + salt encoding (abi-encoded 32-byte address vs packed 20).
    """
    from eth_utils import keccak, to_checksum_address
    SAFE_FACTORY = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b"
    SAFE_INIT_CODE_HASH = "0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf"
    # salt = keccak256(abi.encode(eoa)) — 32-byte left-padded.
    eoa_padded = bytes.fromhex(eoa.lower().replace("0x", "").rjust(64, "0"))
    salt = keccak(eoa_padded)
    factory_bytes = bytes.fromhex(SAFE_FACTORY.lower().replace("0x", ""))
    init_code_hash = bytes.fromhex(SAFE_INIT_CODE_HASH.replace("0x", ""))
    addr = keccak(b"\xff" + factory_bytes + salt + init_code_hash)[12:]
    return to_checksum_address(addr)


def get_poly_signer_info(private_key: Optional[str]) -> Optional[dict]:
    """Detect the user's Polymarket account shape.

    Returns a dict ``{signature_type, funder, balance}`` describing
    the on-chain account this signing key controls. balance is in
    USD (6-decimal converted). funder is the EOA address derived
    from the private key — Polymarket uses it both as the EOA for
    sig_type=0 and as the "underlying owner" of sig_type=1 / 2
    proxy contracts.

    Probing strategy: call /balance-allowance for sig_types 0, 1, 2
    in order. Return the first that reports balance > 0. If all
    three return 0, return {sig_type=0, balance=0} as the "safe
    EOA default" — the user is unfunded; any of the three answers
    is equivalent.

    Cached per-key for 5 minutes. Failure (no key, SDK error,
    network) returns None so callers can fall back gracefully.
    """
    if not private_key or not isinstance(private_key, str):
        return None
    import hashlib
    cache_key = hashlib.sha256(private_key.encode("utf-8")).hexdigest()[:16]
    now = time.monotonic()
    cached = _POLY_SIGNER_CACHE.get(cache_key)
    if cached is not None:
        info, ts = cached
        if now - ts < _POLY_SIGNER_TTL_SECONDS:
            return info

    # Serialize the probe. The GUI polls /api/summary every 5 seconds
    # and the dashboard fires 7 endpoints in parallel; without
    # coordination, every burst spawned 7+ concurrent probes that
    # contended for the GIL on every SSL read.
    #
    # Non-blocking acquire: if another thread is already probing,
    # return INSTANTLY with whatever's cached (even stale, even None).
    # The old `timeout=1.5s` blocked every waiter for 1.5s on cold
    # cache; an /api/summary burst of 7 endpoints = 7 * 1.5s of
    # serialized lock waits, which manifests as the user-visible
    # "app times out" wedge of 2026-05-20. With non-blocking, the
    # first request holds the lock and probes; subsequent requests
    # return None / stale immediately. The first probe populates
    # the cache; the next poll cycle hits warm cache for everyone.
    acquired = _POLY_SIGNER_LOCK.acquire(blocking=False)
    if not acquired:
        cached = _POLY_SIGNER_CACHE.get(cache_key)
        if cached is not None:
            info, _ = cached
            # Serve stale silently. The in-progress probe will
            # refresh the cache shortly.
            return info
        # No cache yet AND another thread is probing. Returning None
        # is the right answer - the caller (get_bankroll) falls back
        # to _LIVE_BANKROLL_FALLBACK or 0.0 in live mode rather than
        # blocking the caller waiting for a probe it doesn't own.
        return None
    try:
        # Double-check: another waiter may have populated the cache
        # while we were blocked on the lock.
        cached = _POLY_SIGNER_CACHE.get(cache_key)
        if cached is not None:
            info, ts = cached
            if time.monotonic() - ts < _POLY_SIGNER_TTL_SECONDS:
                return info

        try:
            from py_clob_client_v2.clob_types import (
                AssetType, BalanceAllowanceParams,
            )
            # Derive the EOA from the key first - used as the funder
            # for proxy queries.
            seed_client = _build_clob_client(private_key)
            eoa = seed_client.get_address()
        except Exception as exc:
            print(f"[polymarket_wallet] CLOB signer-info init failed: {exc}",
                  file=sys.stderr)
            # CLOB unreachable. Serve stale cache rather than returning
            # None so the Dashboard keeps showing the last-known balance
            # instead of flashing to fallback / $0. Do NOT overwrite the
            # cache; next probe replaces it with fresh data.
            stale = _POLY_SIGNER_CACHE.get(cache_key)
            if stale is not None:
                info, _ts = stale
                if info is not None:
                    return info
            return None

        # The funder we pass to the SDK is what ends up as the order's
        # `maker` field. We try every known Polymarket account shape:
        #
        #   sig=0 + EOA            classic MetaMask user
        #   sig=1 + POLY_PROXY     legacy V1 Magic-account proxy
        #   sig=2 + GNOSIS_SAFE    older Gnosis-Safe magic account
        #   sig=1/2/3 + DEPOSIT_WALLET
        #                          NEW V2 account (2026-04-28 cutover).
        #                          Polymarket's UI prompts users to
        #                          "upgrade to continue trading" on
        #                          first V2 order; that migration
        #                          deploys this contract. Which
        #                          sig_type the CLOB expects for the
        #                          DepositWallet isn't documented;
        #                          probe sig=1, sig=2, and sig=3
        #                          (POLY_1271, the EIP-1271 path used
        #                          by smart-contract wallets).
        poly_proxy     = _derive_poly_proxy(eoa)
        poly_safe      = _derive_poly_safe(eoa)
        deposit_wallet = _derive_deposit_wallet(eoa)

        # Order matters: a freshly-migrated user has $0 at the old
        # proxy but the CLOB will report the right balance when probed
        # at the DepositWallet. We probe DepositWallet+sig3 (POLY_1271)
        # FIRST because:
        #   - The DepositWallet is a SMART CONTRACT wallet (Solady CWIA).
        #   - Polymarket accepts sig_type=1 for /balance-allowance
        #     queries (the CLOB serves the post-migration balance
        #     against any of its registered signature types).
        #   - But orders MUST use sig_type=3 (POLY_1271, EIP-1271
        #     contract signatures) when the maker is a smart-contract
        #     wallet. Submitting a DepositWallet order under sig_type=1
        #     gives "maker address not allowed".
        #   - Confirmed empirically (2026-05-17): a user's working
        #     manual Polymarket trade drew pUSD from their DepositWallet,
        #     received CTF tokens at their DepositWallet, and was
        #     processed by the V2 CTF Exchange — implying maker=DW
        #     and sig=POLY_1271.
        # If sig_type=3 returns nothing we fall back to other shapes.
        probe_candidates = [
            (3, deposit_wallet, "DepositWallet+sig3 (POLY_1271, V2 default)"),
            (1, deposit_wallet, "DepositWallet+sig1 (legacy fallback)"),
            (2, deposit_wallet, "DepositWallet+sig2"),
            (1, poly_proxy,     "POLY_PROXY+sig1 (V1 legacy)"),
            (2, poly_safe,      "POLY_GNOSIS_SAFE+sig2"),
            (0, eoa,            "EOA+sig0"),
        ]

        chosen: Optional[dict] = None
        # Track whether any probe returned a usable response (even
        # balance=0). If every probe raises (CLOB unreachable, DNS
        # wedge), we DO NOT want to cache the fake $0 default - that
        # would persist for 5 minutes and show the user "Balance $0"
        # while their wallet actually has money. Instead, fall back
        # to stale cache after the loop.
        any_probe_responded = False
        for sig_type, funder, label in probe_candidates:
            try:
                client = _build_clob_client(
                    private_key, signature_type=sig_type, funder=funder,
                )
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL, signature_type=sig_type,
                )
                result = client.get_balance_allowance(params) or {}
                # If we got here without raising, at least this probe
                # got a response from the CLOB - balance may be 0 or
                # positive, but the network and SDK both worked.
                any_probe_responded = True
                raw = result.get("balance")
                raw_allow = result.get("allowance")
                if raw is None:
                    continue
                try:
                    value = int(raw) / (10 ** _USDC_DECIMALS)
                    allowance = int(raw_allow) / (10 ** _USDC_DECIMALS) if raw_allow is not None else 0.0
                except (TypeError, ValueError):
                    continue
                print(
                    f"[polymarket_wallet] probe {label:32} sig={sig_type} "
                    f"funder={funder} → balance=${value:.4f}",
                    file=sys.stderr,
                )
                if value > 0:
                    # Also probe legacy USDC.e at the funder. These are
                    # the "pending deposit" funds returned by V1 markets
                    # at settlement — not yet tradeable (V2 trades pUSD
                    # only) but the auto-activator (pm_activate_legacy
                    # in main.py) wraps them on a 10-min cadence, so for
                    # bankroll-display purposes they should count as part
                    # of the user's spendable balance. Without this, the
                    # Telegram WIN message reports a "Balance" that omits
                    # winnings still in USDC.e form — user sees
                    # "Won +$5" and "Balance $3.47" and the math doesn't
                    # add up.
                    # USDC.e balance probe. Iterate through every
                    # configured Polygon RPC and stop at the first
                    # success. The earlier version hardcoded
                    # POLYGON_RPC_URL (= POLYGON_RPC_URLS[0]) which
                    # is polygon.llamarpc.com; DNS resolution for
                    # that host has been failing on this network. On
                    # failure the silent fallback to 0 caused the
                    # Telegram "Balance" to omit any USDC.e at the
                    # funder ("WIN +$1.67, Balance $9.99" while the
                    # on-chain wallet was actually $14.99 with $5 of
                    # the win sitting unwrapped as USDC.e). Same
                    # iteration pattern as the earlier fixes in
                    # pm_redeemer.resolve_collateral_for_position
                    # and pm_redeemer.activate_legacy_collateral_balance.
                    usdce_at_funder = 0.0
                    try:
                        import requests as _r
                        # NB: _encode_balance_of_call already returns
                        # the "0x" prefix (it returns
                        # _BALANCE_OF_SELECTOR + padded, and the
                        # selector is "0x70a08231"). Prepending another
                        # "0x" produces "0x0x..." which every RPC
                        # rejects as "invalid hex" — this is why the
                        # probe has been quietly returning 0 USDC.e for
                        # weeks regardless of which RPC we hit. Fix is
                        # the single character below.
                        _usdce_payload = {
                            "jsonrpc": "2.0", "id": 1,
                            "method": "eth_call",
                            "params": [
                                {"to": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                                 "data": _encode_balance_of_call(funder)},
                                "latest",
                            ],
                        }
                        _usdce_errors: list[str] = []
                        _usdce_ok = False
                        for _rpc_url in POLYGON_RPC_URLS:
                            try:
                                _resp = _r.post(
                                    _rpc_url, json=_usdce_payload,
                                    timeout=10,
                                    headers={
                                        "Content-Type": "application/json",
                                        "Accept": "application/json",
                                    },
                                )
                                _body = _resp.json() if _resp is not None else {}
                                _raw = (_body or {}).get("result")
                                if not _raw:
                                    # Include first 120 chars of the
                                    # body so we can see whether the RPC
                                    # is rate-limiting, returning an
                                    # error, or just garbage.
                                    _body_str = (
                                        str(_body)[:120] if _body
                                        else str(_resp.text)[:120]
                                    )
                                    _usdce_errors.append(
                                        f"{_rpc_url} status={_resp.status_code} "
                                        f"body={_body_str}"
                                    )
                                    continue
                                usdce_at_funder = (
                                    int(_raw, 16) / (10 ** _USDC_DECIMALS)
                                )
                                _usdce_ok = True
                                break
                            except Exception as _exc:
                                _usdce_errors.append(
                                    f"{_rpc_url}: {type(_exc).__name__}: "
                                    f"{str(_exc)[:60]}"
                                )
                                continue
                        if not _usdce_ok:
                            print(
                                f"[polymarket_wallet] usdce probe failed "
                                f"on all RPCs: "
                                f"{' | '.join(_usdce_errors[:3])}",
                                file=sys.stderr,
                            )
                    except Exception as exc:
                        print(
                            f"[polymarket_wallet] usdce probe failed: {exc}",
                            file=sys.stderr,
                        )
                    chosen = {
                        "signature_type": sig_type,
                        "funder":         funder,
                        "eoa":            eoa,
                        "balance":        float(value),
                        "allowance":      float(allowance),
                        "usdce_legacy":   float(usdce_at_funder),
                    }
                    print(
                        f"[polymarket_wallet] account shape: sig_type={sig_type} "
                        f"funder={funder} ({label}) balance=${value:.4f} "
                        f"allowance=${allowance:.4f}",
                        file=sys.stderr,
                    )
                    # Force an api-key rotation under THIS context. Once
                    # per process per (sig_type, funder). Fixes the
                    # "the order signer address has to be the address of
                    # the API KEY" rejection that happens when the
                    # api-key on file was created under an OLDER context
                    # (e.g. legacy POLY_PROXY pre-V2-migration).
                    try:
                        _build_clob_client(
                            private_key,
                            signature_type=sig_type,
                            funder=funder,
                            rotate_api_key=True,
                        )
                    except Exception as exc:
                        print(f"[polymarket_wallet] post-probe rotate failed: {exc}",
                              file=sys.stderr)
                    break
            except Exception as exc:
                print(f"[polymarket_wallet] probe {label} failed: {exc}",
                      file=sys.stderr)
                continue

        if chosen is None:
            if not any_probe_responded:
                # Every probe RAISED an exception (CLOB unreachable /
                # DNS wedge / SSL timeout). The user's wallet might
                # have money - we just couldn't read it. Serve stale
                # cache if any; do NOT cache a zero default that
                # would override real balance for 5 minutes after
                # CLOB recovers.
                stale = _POLY_SIGNER_CACHE.get(cache_key)
                if stale is not None:
                    info, _ts = stale
                    if info is not None:
                        return info
                return None
            # At least one probe got a response and they all said $0.
            # Default to the V2 DepositWallet shape - that's the path
            # Polymarket's UI now pushes every new user through. A
            # fresh deposit will land there and the next probe catches
            # it. Cache the default so subsequent reads don't re-probe.
            chosen = {
                "signature_type": 1,
                "funder":         deposit_wallet,
                "eoa":            eoa,
                "balance":        0.0,
                "allowance":      0.0,
            }

        _POLY_SIGNER_CACHE[cache_key] = (chosen, now)
        return chosen
    finally:
        _POLY_SIGNER_LOCK.release()


def get_live_clob_balance(private_key: Optional[str]) -> Optional[float]:
    """Backwards-compat shim returning just the balance number.

    New code should call get_poly_signer_info directly; that's the
    function that also exposes signature_type for order placement.
    """
    info = get_poly_signer_info(private_key)
    return float(info["balance"]) if info else None


# ── Non-blocking helpers (for the request hot path) ─────────────────────────
#
# CRITICAL: these helpers NEVER acquire _POLY_SIGNER_LOCK and NEVER make a
# network call. They serve from the in-process cache only. The hot path
# (/api/summary, polled every 5s by the dashboard) goes through these so
# a slow probe in a background thread cannot wedge user-facing requests.
#
# A separate scheduled job in main.py runs `get_poly_signer_info` every
# 60s to keep the cache fresh. If that background job blocks (DNS
# wedge, c-ares hang, anything), only the BACKGROUND cache update is
# delayed — the dashboard keeps serving the last known good balance.
#
# Returns the cached value even if past the TTL. Stale cache > no
# value; the cache only gets a "missing" answer on cold start before
# the first background refresh completes. /api/summary's overlay
# falls back to the DB-derived bankroll in that case.

def get_cached_poly_signer_info(private_key: Optional[str]) -> Optional[dict]:
    """Read the cached signer info without any blocking call.

    Returns the last-known signer dict (signature_type, funder, eoa,
    balance, allowance) or None if no probe has completed yet for
    this key. NEVER touches the network and NEVER acquires the
    probe lock. Safe to call from any request handler at any time.
    """
    if not private_key or not isinstance(private_key, str):
        return None
    import hashlib
    cache_key = hashlib.sha256(private_key.encode("utf-8")).hexdigest()[:16]
    cached = _POLY_SIGNER_CACHE.get(cache_key)
    if cached is None:
        return None
    info, _ = cached
    return info


def get_cached_live_clob_balance(private_key: Optional[str]) -> Optional[float]:
    """Non-blocking variant of get_live_clob_balance.

    Returns the last-known wallet balance (USD float) without any
    network call. None if no probe has populated the cache yet for
    this key. Use this from request handlers; /api/summary's live
    overlay reads it.

    "Balance" here is the V2 tradeable pUSD only. For total wealth
    including soon-to-be-activated USDC.e, use
    get_cached_total_funder_balance() instead.
    """
    info = get_cached_poly_signer_info(private_key)
    return float(info["balance"]) if info else None


def get_cached_total_funder_balance(
    private_key: Optional[str],
) -> Optional[float]:
    """Non-blocking total collateral balance at the funder.

    pUSD (tradeable) + USDC.e legacy (auto-activated within ~10 min by
    pm_activate_legacy). This is what should be reported as "bankroll"
    or "Balance" to the user, because every dollar in either bucket is
    spendable on the next trade once the activator's tick lands.

    Returns None if no probe has populated the cache. Falls back to
    just `balance` (pUSD) when the USDC.e field is missing — covers
    caches written by older builds.
    """
    info = get_cached_poly_signer_info(private_key)
    if not info:
        return None
    pusd  = float(info.get("balance") or 0.0)
    usdce = float(info.get("usdce_legacy") or 0.0)
    return pusd + usdce


def _refresh_positions_cache(funder_address: str) -> bool:
    """Internal: fetch data-api /positions and refresh the per-funder
    cache with both currentValue and cashPnl sums.

    Returns True on success, False on any error. Callers read the
    cache directly afterwards (or pre-existing stale entry on
    failure). Single-flight via _POSITIONS_VALUE_LOCK.

    Data-api endpoint:
        https://data-api.polymarket.com/positions?user=<funder>
    Returns a JSON array of position dicts. Each row carries:
        title:        market question
        outcome:      'Yes' | 'No' (or sport-specific labels)
        size:         shares held
        curPrice:     current mid / ask for the held side
        currentValue: size * curPrice (USD value at current prices)
        initialValue: size * entry price (incl. trading fees)
        cashPnl:      currentValue - initialValue (signed)
    Lost positions linger in the response with curPrice=0 (and
    currentValue=0). Their cashPnl appears as a negative number
    (= -initialValue), which IS the realized loss until the user
    redeems the (zero-value) winning side to clear the row from
    the wallet. We sum cashPnl across the whole list so losers
    pull the total down correctly.
    """
    import requests as _r
    resp = _r.get(
        "https://data-api.polymarket.com/positions",
        params={"user": funder_address},
        timeout=8,
    )
    if resp.status_code != 200:
        print(
            f"[polymarket_wallet] data-api positions returned "
            f"{resp.status_code} for {funder_address}",
            file=sys.stderr,
        )
        return False
    data = resp.json()
    if not isinstance(data, list):
        return False
    total_value = 0.0
    total_pnl = 0.0
    for row in data:
        try:
            v = float(row.get("currentValue") or 0.0)
            if v > 0:
                total_value += v
        except (TypeError, ValueError):
            pass
        try:
            # cashPnl can be negative (losing positions); include
            # in the sum without a sign filter.
            p = float(row.get("cashPnl") or 0.0)
            total_pnl += p
        except (TypeError, ValueError):
            pass
    _POSITIONS_VALUE_CACHE[funder_address.lower()] = (
        float(total_value), float(total_pnl), time.monotonic(),
    )
    return True


def get_total_open_positions_value(
    funder_address: Optional[str],
) -> Optional[float]:
    """Sum currentValue of every open position the user holds on
    Polymarket, INCLUDING positions opened manually outside the bot.

    Authoritative source for the Dashboard's "Locked Capital" tile and
    the equity number on every Telegram message. Bot-internal P&L /
    win-rate counts still come from pm_positions filtered by user_id +
    mode, so manual trades don't pollute the bot's track record.

    Returns the sum in USD on success, or None on any failure (caller
    falls through to the DB-tracked sum of current_value_usd, then to
    cost_usd via the COALESCE chain in pm_executor.get_portfolio_stats).
    Cached per funder for 60s so the Dashboard's 5s /api/summary poll
    doesn't hammer Polymarket's data-api.
    """
    if not funder_address:
        return None
    key = funder_address.lower()
    now = time.monotonic()
    cached = _POSITIONS_VALUE_CACHE.get(key)
    if cached is not None and now - cached[2] < _POSITIONS_VALUE_TTL_SECONDS:
        return cached[0]
    # Single-flight: /api/summary fires 7 endpoints in parallel. On
    # cold positions cache, without this lock each request would
    # independently fire its own HTTPS to data-api with an 8s
    # timeout, holding an executor worker the entire time.
    # Non-blocking acquire: first request runs the probe;
    # subsequent concurrent requests instantly get the stale cache
    # (None on the very first probe, the fresh value once it lands).
    acquired = _POSITIONS_VALUE_LOCK.acquire(blocking=False)
    if not acquired:
        return cached[0] if cached is not None else None
    try:
        # Re-check the cache: another thread may have populated it
        # while we were waiting on the GIL between get and acquire.
        cached = _POSITIONS_VALUE_CACHE.get(key)
        if cached is not None and time.monotonic() - cached[2] < _POSITIONS_VALUE_TTL_SECONDS:
            return cached[0]
        if _refresh_positions_cache(funder_address):
            return _POSITIONS_VALUE_CACHE[key][0]
        return cached[0] if cached is not None else None
    except Exception as exc:
        print(
            f"[polymarket_wallet] data-api positions fetch failed for "
            f"{funder_address}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        # Network blip: serve stale cache if any. The Dashboard's
        # "Locked Capital" should keep showing the last sensible
        # number rather than flashing to $0 when data-api hiccups.
        return cached[0] if cached is not None else None
    finally:
        _POSITIONS_VALUE_LOCK.release()


# Cache for Polymarket's All-Time P&L number. This is what their
# portfolio UI shows as "All-Time Profit/Loss". Reconstructed from
# their bookkeeping (positions, redeems, deposits, withdrawals).
# Source: https://user-pnl-api.polymarket.com/user-pnl
#                                  ?user_address=<funder>
#                                  &interval=all&fidelity=1d
# Returns a time series; the LAST point is the current All-Time P&L.
# Cache 60s (same as positions sum) so the dashboard's 5s poll stays
# cheap.
_USER_PNL_CACHE: Dict[str, Tuple[float, float]] = {}
_USER_PNL_TTL_SECONDS = 60.0
_USER_PNL_LOCK = _threading.Lock()


def cached_user_total_pnl(funder_address: Optional[str]) -> Optional[float]:
    """Cache-only read: return the most-recently-cached All-Time P&L
    if we have one, else None. Never makes a network call.

    NO staleness cutoff. Polymarket is the source of truth — if their
    endpoint is briefly unreachable the right answer is "show the
    last Polymarket-authoritative value we got", not "fall back to a
    local realized+unrealized computation that drifts by $1-3 from
    what the user sees on polymarket.com". The refresh job retries
    every 60s; when it succeeds the cache updates. If it stays down
    for hours the user sees a slightly stale Polymarket number,
    which is still better than a wrong local number that pretends
    to be authoritative.

    The cache is warmed at daemon boot by _prewarm_poly_caches and
    refreshed every 60s by pm_balance_refresh. There is a window of
    ~1-3 seconds at startup where the cache is genuinely empty
    (boot has begun but prewarm hasn't completed the round-trip);
    during that window this returns None.
    """
    if not funder_address:
        return None
    key = funder_address.lower()
    cached = _USER_PNL_CACHE.get(key)
    if cached is None:
        return None
    return cached[0]


def cached_total_open_positions_cash_pnl(
    funder_address: Optional[str],
) -> Optional[float]:
    """Cache-only read counterpart to
    get_total_open_positions_cash_pnl. Same no-staleness-cutoff
    policy as cached_user_total_pnl — always return the most recent
    Polymarket-authoritative value if we ever got one.
    """
    if not funder_address:
        return None
    key = funder_address.lower()
    cached = _POSITIONS_VALUE_CACHE.get(key)
    if cached is None:
        return None
    return cached[1]


def refresh_pnl_caches(funder_address: Optional[str]) -> None:
    """Background-only entry point. Called from the pm_pnl_refresh
    scheduler job (every 60s) to keep both PnL caches warm. The
    network IO happens here, OFF the request path, so a slow
    Polymarket endpoint can never wedge a /api/* read.

    Catches and logs any exception so a transient endpoint outage
    doesn't poison the scheduler job (which would stop ALL future
    refreshes).
    """
    if not funder_address:
        return
    try:
        get_user_total_pnl(funder_address)
    except Exception as exc:
        print(
            f"[polymarket_wallet] background user-pnl refresh failed "
            f"for {funder_address}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
    try:
        # _refresh_positions_cache populates BOTH currentValue and
        # cashPnl in one fetch, so this also keeps the locked-capital
        # cache warm without an extra round-trip.
        acquired = _POSITIONS_VALUE_LOCK.acquire(blocking=False)
        if acquired:
            try:
                _refresh_positions_cache(funder_address)
            finally:
                _POSITIONS_VALUE_LOCK.release()
    except Exception as exc:
        print(
            f"[polymarket_wallet] background positions refresh failed "
            f"for {funder_address}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def get_user_total_pnl(funder_address: Optional[str]) -> Optional[float]:
    """Fetch Polymarket's authoritative All-Time P&L for this wallet.

    This is the number that appears on Polymarket's own portfolio UI
    as "All-Time Profit/Loss" — derived from their own bookkeeping
    (trade fills, redemptions, deposits, withdrawals). Local math
    can't reconstruct it from the bot's pm_positions table alone
    because the user may have manual trades, settled losers that
    haven't been redeemed (so they don't appear in `realized_pnl`
    yet), or trading fees that the bot's cost_usd didn't capture.

    Returns the last value (USD, signed) on success, None on any
    failure. Callers fall back to the bot's local realized +
    unrealized computation.

    Cached per funder for 60s + single-flight lock so the dashboard's
    5s /api/summary poll doesn't hammer the endpoint.
    """
    if not funder_address:
        return None
    key = funder_address.lower()
    now = time.monotonic()
    cached = _USER_PNL_CACHE.get(key)
    if cached is not None and now - cached[1] < _USER_PNL_TTL_SECONDS:
        return cached[0]
    acquired = _USER_PNL_LOCK.acquire(blocking=False)
    if not acquired:
        return cached[0] if cached is not None else None
    try:
        cached = _USER_PNL_CACHE.get(key)
        if cached is not None and time.monotonic() - cached[1] < _USER_PNL_TTL_SECONDS:
            return cached[0]
        import requests as _r
        # Tight (connect, read) timeout. user-pnl-api is on a
        # different host than data-api and we've seen it spike
        # well above 5s under load; the api_executor only has 8
        # workers, so a single 8s hang here can starve every other
        # /api/* read endpoint waiting for a worker slot.
        resp = _r.get(
            "https://user-pnl-api.polymarket.com/user-pnl",
            params={
                "user_address": funder_address,
                "interval": "all",
                "fidelity": "1d",
            },
            timeout=(2.5, 3.5),
        )
        if resp.status_code != 200:
            print(
                f"[polymarket_wallet] user-pnl returned {resp.status_code} "
                f"for {funder_address}",
                file=sys.stderr,
            )
            return cached[0] if cached is not None else None
        data = resp.json()
        if not isinstance(data, list) or not data:
            return cached[0] if cached is not None else None
        # Last point in the time series = current All-Time P&L.
        last = data[-1]
        if not isinstance(last, dict) or "p" not in last:
            return cached[0] if cached is not None else None
        value = float(last["p"])
        _USER_PNL_CACHE[key] = (value, now)
        return value
    except Exception as exc:
        print(
            f"[polymarket_wallet] user-pnl fetch failed for "
            f"{funder_address}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return cached[0] if cached is not None else None
    finally:
        _USER_PNL_LOCK.release()


def get_total_open_positions_cash_pnl(
    funder_address: Optional[str],
) -> Optional[float]:
    """Sum Polymarket's per-position cashPnl across every position
    the wallet holds. This is the authoritative unrealized P&L
    number — same formula Polymarket's own portfolio UI uses, so
    the Dashboard's P&L matches the Polymarket portfolio page to
    the cent.

    Why prefer this over local `currentValue - cost_usd`:
      1. Polymarket uses mid-price for currentValue; the bot's
         cost_usd was recorded from execution price (taker side).
         Local math overstates unrealized by the bid/ask spread.
      2. Polymarket folds in trading fees on initialValue; local
         cost_usd doesn't include them, so local unrealized is
         additionally inflated by the fee delta.
      3. Lost positions still in the wallet show cashPnl as the
         realized loss; local math treats them as zero (currentValue=0
         minus cost_usd) but that lands in unrealized rather than
         realized until redemption. cashPnl puts them in the right
         bucket.

    Returns the signed sum (USD) on success, None on any failure
    (network blip, parse error, no funder). Callers fall back to the
    local computation.

    Shares the 60s cache + single-flight lock with
    get_total_open_positions_value so /api/summary makes ONE data-api
    round-trip per minute regardless of how many fields read from it.
    """
    if not funder_address:
        return None
    key = funder_address.lower()
    now = time.monotonic()
    cached = _POSITIONS_VALUE_CACHE.get(key)
    if cached is not None and now - cached[2] < _POSITIONS_VALUE_TTL_SECONDS:
        return cached[1]
    acquired = _POSITIONS_VALUE_LOCK.acquire(blocking=False)
    if not acquired:
        return cached[1] if cached is not None else None
    try:
        cached = _POSITIONS_VALUE_CACHE.get(key)
        if cached is not None and time.monotonic() - cached[2] < _POSITIONS_VALUE_TTL_SECONDS:
            return cached[1]
        if _refresh_positions_cache(funder_address):
            return _POSITIONS_VALUE_CACHE[key][1]
        return cached[1] if cached is not None else None
    except Exception as exc:
        print(
            f"[polymarket_wallet] data-api cashPnl fetch failed for "
            f"{funder_address}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return cached[1] if cached is not None else None
    finally:
        _POSITIONS_VALUE_LOCK.release()


def force_refresh_all_polymarket_caches(private_key: Optional[str]) -> None:
    """One-shot cache invalidation for every Polymarket-side number
    a user-visible message might read: wallet balance, data-api
    positions sum (locked_capital), data-api cashPnl (unrealized
    P&L), and user-pnl all-time P&L.

    Use BEFORE constructing any Telegram message or computing values
    for a downstream notification. Without this, two messages that
    fire within the wallet cache's 5-minute TTL show different
    numbers depending on which cache happened to be fresh when each
    message was built — observed 2026-05-23 with a WIN at T0, OPEN
    at T1, LOSS at T2 all showing wildly inconsistent Balance /
    Locked capital / Total equity values that should have formed a
    coherent sequence.

    All four refreshes are best-effort; failures are swallowed.
    Slowest is the wallet probe (~2-5s on a cold network); the
    others are sub-second after the signer info is warm.
    """
    if not private_key:
        return
    try:
        refresh_live_balance_cache(private_key)
    except Exception as exc:
        print(f"[polymarket_wallet] force-refresh wallet failed: {exc}",
              file=sys.stderr)
    try:
        info = get_poly_signer_info(private_key)
        funder = (info or {}).get("funder") if info else None
        if funder:
            # refresh_pnl_caches fetches /positions (currentValue +
            # cashPnl) AND user-pnl in one go.
            refresh_pnl_caches(funder)
    except Exception as exc:
        print(f"[polymarket_wallet] force-refresh pnl caches "
              f"failed: {exc}", file=sys.stderr)


def refresh_live_balance_cache(private_key: Optional[str]) -> bool:
    """Force-refresh the cached wallet probe for this key.

    Invalidates the existing cache entry and runs a fresh probe so the
    very next ``get_cached_total_funder_balance`` read reflects current
    on-chain state instead of a stale value from up to 5 minutes ago.

    Call this immediately after an on-chain state change for the user's
    funder:

      * new position opened (cost spent from wallet)
      * winning position redeemed (payout arrives as USDC.e or pUSD)
      * USDC.e -> pUSD wrap completes
      * deposit / withdrawal observed

    Without the explicit invalidation, the previous implementation just
    called ``get_poly_signer_info`` which served from cache when fresh,
    making this function a no-op when it was called immediately after a
    state change (exactly the case it exists for). That broke the
    "Balance in WIN notification includes the just-won money" UX:
    settle_position redeemed synchronously, then the next
    ``get_portfolio_stats`` call read the stale cached wallet and the
    Telegram message rendered a pre-payout balance.

    Used by the scheduler's pm_balance_refresh job (main.py),
    pm_analyst's post-open hook, and settle_position's post-redeem hook.
    Returns True on success, False on probe failure / lock timeout.
    """
    if not private_key:
        return False
    # CRITICAL: do NOT pre-delete the existing cache entry. The
    # earlier implementation called `_POLY_SIGNER_CACHE.pop()` here
    # to force `get_poly_signer_info` to actually probe (it would
    # otherwise serve the still-fresh cache). The problem: when the
    # Polymarket CLOB is briefly unreachable (transient network or
    # CLOB-side hiccup, observed 2026-05-20), the probe fails, the
    # cache stays empty, and every /api/summary thereafter pays the
    # full 8s+ probe-timeout cost. We want exactly the opposite -
    # stale cache should keep serving instantly while the background
    # job retries.
    #
    # Trick: temporarily set the cached timestamp to expired so
    # get_poly_signer_info bypasses the freshness short-circuit and
    # runs a probe. On probe success the cache is overwritten with
    # the new value. On probe failure the old cache entry remains in
    # the dict (probe never wrote anything), and downstream callers
    # served via the lock-free `get_cached_poly_signer_info` see the
    # last-known value rather than None.
    try:
        import hashlib
        cache_key = hashlib.sha256(
            private_key.encode("utf-8")
        ).hexdigest()[:16]
        cached = _POLY_SIGNER_CACHE.get(cache_key)
        if cached is not None:
            info, _ = cached
            # Re-stamp with monotonic=0 so the TTL check fires but
            # the value stays available if the probe fails.
            _POLY_SIGNER_CACHE[cache_key] = (info, 0.0)
    except Exception:
        pass
    info = get_poly_signer_info(private_key)
    return info is not None
