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
    """Dump the wallet cache. Useful for tests and manual refresh."""
    _CACHE.clear()
    _CLOB_BALANCE_CACHE.clear()


# ── CLOB-side balance (authoritative for live trading) ──────────────────────
#
# `get_live_usdc_balance` above queries USDC contracts directly via
# eth_call. That works if the user holds collateral at their EOA. It
# does NOT work for funds the user has deposited into their Polymarket
# Account (a proxy / Magic wallet derived from the EOA) — and that's
# where every Polymarket UI deposit actually lands. The CLOB's
# `/balance-allowance` endpoint knows the proxy and returns the
# balance the bot can actually trade with, which is exactly what the
# Dashboard "Balance" tile needs to show.

_CLOB_BALANCE_CACHE: Dict[str, Tuple[float, float]] = {}
_CLOB_BALANCE_TTL_SECONDS = 60.0


def get_live_clob_balance(private_key: Optional[str]) -> Optional[float]:
    """
    Ask Polymarket's CLOB how much collateral this signer can spend.

    Source-of-truth for live-mode bankroll. Caches per-key in-memory
    for 60 s so the Dashboard's poll loop doesn't slam the CLOB API.
    Returns USD float on success, None on any failure (key missing,
    SDK error, network) — caller decides what to display.

    Synchronous (the SDK is sync); call sites wrap it in an executor
    when running on the asyncio loop.
    """
    if not private_key or not isinstance(private_key, str):
        return None
    import hashlib
    cache_key = hashlib.sha256(private_key.encode("utf-8")).hexdigest()[:16]
    now = time.monotonic()
    cached = _CLOB_BALANCE_CACHE.get(cache_key)
    if cached is not None:
        bal, ts = cached
        if now - ts < _CLOB_BALANCE_TTL_SECONDS:
            return bal

    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import (
            AssetType, BalanceAllowanceParams,
        )
        # Two-step construction per the SDK README: derive API key
        # from the signing key, then build a fully-authed client.
        # Lifecycle is short — used once per /api/summary poll inside
        # the 60 s cache window. We don't try to share pm_executor's
        # cached client to avoid coupling the read path on the
        # executor's internals.
        CLOB_HOST = "https://clob.polymarket.com"
        POLYGON_CHAIN_ID = 137
        seed = ClobClient(host=CLOB_HOST, chain_id=POLYGON_CHAIN_ID, key=private_key)
        creds = seed.create_or_derive_api_key()
        client = ClobClient(
            host=CLOB_HOST, chain_id=POLYGON_CHAIN_ID,
            key=private_key, creds=creds,
        )
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        result = client.get_balance_allowance(params) or {}
        raw = result.get("balance")
        if raw is None:
            return None
        # CLOB returns the raw uint256 string in 6-decimal USDC units.
        try:
            value = int(raw) / (10 ** _USDC_DECIMALS)
        except (TypeError, ValueError):
            return None
    except Exception as exc:
        print(f"[polymarket_wallet] CLOB balance fetch failed: {exc}",
              file=sys.stderr)
        return None

    _CLOB_BALANCE_CACHE[cache_key] = (float(value), now)
    return float(value)
