"""
Polymarket wallet helpers.

Query the user's on-chain USDC balance on Polygon so the dashboard can
show the real bankroll when the bot is in live mode. Polymarket runs on
Polygon (chain id 137); balances live in the user's wallet address
which is stored in user_config.wallet_address.

Two USDC variants exist on Polygon and both have been used by
Polymarket over the years:
    native USDC     0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359
    bridged USDC.e  0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174
Both are 6-decimal ERC-20. We query each, sum the results, and return
the total so a user holding either variant sees the right number.

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

# USDC contracts on Polygon. Polymarket currently uses native USDC but
# bridged USDC.e is still held in many older wallets, so we query both
# and sum to avoid missing funds.
_USDC_CONTRACTS: Tuple[str, ...] = (
    "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # native USDC
    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # bridged USDC.e
)

# ERC-20 balanceOf(address) selector
_BALANCE_OF_SELECTOR = "0x70a08231"

# USDC is 6 decimals on Polygon (both variants)
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
                for contract in _USDC_CONTRACTS
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
