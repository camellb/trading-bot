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


# Public Polygon RPC - override via env for paid providers.
POLYGON_RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")

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

_POLY_SIGNER_CACHE: Dict[str, Tuple[Optional[dict], float]] = {}
_POLY_SIGNER_TTL_SECONDS = 300.0  # 5 minutes


def _build_clob_client(
    private_key: str,
    signature_type: int = -1,
    funder: Optional[str] = None,
):
    """Two-step construction per the py-clob-client-v2 SDK: derive an
    api-key with the signing key first, then build the fully-authed
    client. Helpers in this module use it; pm_executor builds its
    own cached version for order placement."""
    from py_clob_client_v2.client import ClobClient
    CLOB_HOST = "https://clob.polymarket.com"
    POLYGON_CHAIN_ID = 137
    seed_kwargs = dict(host=CLOB_HOST, chain_id=POLYGON_CHAIN_ID, key=private_key)
    if signature_type != -1:
        seed_kwargs["signature_type"] = signature_type
        if funder:
            seed_kwargs["funder"] = funder
    seed = ClobClient(**seed_kwargs)
    creds = seed.create_or_derive_api_key()
    client_kwargs = dict(seed_kwargs)
    client_kwargs["creds"] = creds
    return ClobClient(**client_kwargs)


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

    try:
        from py_clob_client_v2.clob_types import (
            AssetType, BalanceAllowanceParams,
        )
        # Derive the EOA from the key first — used as the funder for
        # proxy queries.
        seed_client = _build_clob_client(private_key)
        eoa = seed_client.get_address()
    except Exception as exc:
        print(f"[polymarket_wallet] CLOB signer-info init failed: {exc}",
              file=sys.stderr)
        return None

    # The funder we pass to the SDK is what ends up as the order's
    # `maker` field. For sig_type=1 it must be the POLY_PROXY (not
    # the EOA); for sig_type=2 it must be the GNOSIS_SAFE. We use
    # the EOA only for sig_type=0 (true EOA accounts). For balance
    # queries the CLOB also accepts the proxy/safe directly via
    # the `funder` param paired with the right sig_type.
    funder_by_sig = {
        0: eoa,
        1: _derive_poly_proxy(eoa),
        2: _derive_poly_safe(eoa),
    }

    chosen: Optional[dict] = None
    for sig_type in (0, 1, 2):
        funder = funder_by_sig[sig_type]
        try:
            client = _build_clob_client(
                private_key, signature_type=sig_type, funder=funder,
            )
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL, signature_type=sig_type,
            )
            result = client.get_balance_allowance(params) or {}
            raw = result.get("balance")
            raw_allow = result.get("allowance")
            if raw is None:
                continue
            try:
                value = int(raw) / (10 ** _USDC_DECIMALS)
                allowance = int(raw_allow) / (10 ** _USDC_DECIMALS) if raw_allow is not None else 0.0
            except (TypeError, ValueError):
                continue
            if value > 0:
                chosen = {
                    "signature_type": sig_type,
                    "funder":         funder,
                    "eoa":            eoa,
                    "balance":        float(value),
                    "allowance":      float(allowance),
                }
                print(
                    f"[polymarket_wallet] account shape: sig_type={sig_type} "
                    f"funder={funder} balance=${value:.4f} allowance=${allowance:.4f}",
                    file=sys.stderr,
                )
                break
        except Exception as exc:
            print(f"[polymarket_wallet] probe sig_type={sig_type} failed: {exc}",
                  file=sys.stderr)
            continue

    if chosen is None:
        # All three probed clean / zero. Default to POLY_PROXY shape
        # (the modal case for Polymarket UI users); a fresh deposit
        # will land in the proxy and the next probe catches it.
        chosen = {
            "signature_type": 1,
            "funder":         funder_by_sig[1],
            "eoa":            eoa,
            "balance":        0.0,
            "allowance":      0.0,
        }

    _POLY_SIGNER_CACHE[cache_key] = (chosen, now)
    return chosen


def get_live_clob_balance(private_key: Optional[str]) -> Optional[float]:
    """Backwards-compat shim returning just the balance number.

    New code should call get_poly_signer_info directly; that's the
    function that also exposes signature_type for order placement.
    """
    info = get_poly_signer_info(private_key)
    return float(info["balance"]) if info else None
