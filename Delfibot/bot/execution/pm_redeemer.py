"""
On-chain redemption of resolved Polymarket positions.

When a binary outcome resolves the user holds ERC-1155 outcome tokens.
Polymarket's web UI auto-prompts winners to redeem; until this module
landed, Delfi only updated the DB on settlement and left the
underlying tokens in the wallet, which meant the on-chain pUSD
balance silently disagreed with the in-app P&L until the user clicked
"Redeem" on the Polymarket UI.

This module closes that gap by calling the standard Gnosis
Conditional Tokens Framework method directly:

    function redeemPositions(
        IERC20 collateralToken,
        bytes32 parentCollectionId,
        bytes32 conditionId,
        uint256[] calldata indexSets
    ) external

For binary YES/NO markets:
  * collateralToken = pUSD (0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB),
    the active V2 collateral after the 2026-04-28 cutover. Pre-V2
    positions on the same wallet would need USDC.e instead, but the
    bot has only opened V2 positions since the cutover.
  * parentCollectionId = 0x00..00 (binary markets are top-level).
  * conditionId is what we already persist on every pm_positions row.
  * indexSets selects which outcome tokens to redeem:
        [1]  redeem the YES tokens (use when YES won)
        [2]  redeem the NO tokens  (use when NO won)

Everything is gated by the same DELFI_LIVE_KILLSWITCH_OFF env var
that gates _open_live, so flipping the dashboard to Live alone
cannot send transactions. Default is OFF; the operator must
explicitly opt in.

This module DOES sign and broadcast a real Polygon transaction when
called with the kill switch off. There is no dry-run mode -- the
caller is responsible for only invoking it on confirmed winners.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

# CTF (Gnosis Conditional Tokens) on Polygon mainnet. Same address as
# the V1 CTF; V2 reused it. Source: py_clob_client_v2/config.py.
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# pUSD - the V2 collateral token. Pre-V2 trades against USDC.e are
# legacy; the bot has not opened anything against USDC.e since the
# 2026-04-28 cutover, so we hard-code pUSD here. If a user has stuck
# pre-V2 winners they can redeem them on the Polymarket web UI.
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# Polygon mainnet.
POLYGON_CHAIN_ID = 137

# Public Polygon RPC. Same env override as polymarket_wallet.py.
POLYGON_RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")

# Minimal ABI: only redeemPositions. Avoids shipping the full CTF ABI
# (which has dozens of methods we never call).
CTF_REDEEM_ABI = [
    {
        "inputs": [
            {"name": "collateralToken",     "type": "address"},
            {"name": "parentCollectionId",  "type": "bytes32"},
            {"name": "conditionId",         "type": "bytes32"},
            {"name": "indexSets",           "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# 2 minutes is enough headroom on Polygon's 2s block time even when
# the public RPC is congested.
TX_RECEIPT_TIMEOUT_SECONDS = 120


def _live_killswitch_off() -> bool:
    """True iff the operator has explicitly opted into real on-chain
    redemption. Mirror of pm_executor._live_killswitch_off so this
    module stays standalone."""
    return os.environ.get("DELFI_LIVE_KILLSWITCH_OFF", "").strip() in (
        "1", "true", "True",
    )


@dataclass
class RedeemResult:
    """What the caller in pm_executor needs after a redeem attempt."""

    redeemed: bool
    """True iff the transaction was mined with status=1. False on
    kill-switch-still-on, missing creds, RPC error, revert, or
    timeout. Inspect `error` for the specific reason."""

    tx_hash: Optional[str]
    """0x-prefixed hex hash on broadcast (whether or not it later
    succeeded). None when we never managed to send."""

    error: Optional[str]
    """Human-readable failure reason. None on success. Surfaced into
    the position row's reasoning so the operator can grep for it."""


def index_sets_for_outcome(side: str, outcome: str) -> Optional[Sequence[int]]:
    """Decide which CTF indexSets to redeem for a binary YES/NO market.

    Returns None when there's nothing to redeem on-chain (loser, or
    bad inputs). The settler MUST skip the redeem call in that case.

    Outcome conventions:
      side ('YES' | 'NO')        - the side this position held.
      outcome ('YES' | 'NO' | 'INVALID') - what the market resolved to.

    Index conventions for binary markets in the standard CTF layout
    Polymarket uses:
      [1] = YES tokens
      [2] = NO tokens
    For an INVALID market both YES and NO outcome tokens settle at
    0.5 each. Each pm_positions row only ever holds one side, so we
    redeem only that side's index regardless of outcome.
    """
    side_u    = (side or "").upper()
    outcome_u = (outcome or "").upper()
    if outcome_u == "INVALID":
        if side_u == "YES":
            return [1]
        if side_u == "NO":
            return [2]
        return None
    if side_u == outcome_u == "YES":
        return [1]
    if side_u == outcome_u == "NO":
        return [2]
    # Loser or malformed inputs: nothing to redeem.
    return None


def _normalise_condition_id(condition_id: str) -> Optional[bytes]:
    """Pad / strip a hex condition id to a 32-byte value."""
    if not condition_id:
        return None
    h = condition_id.lower().strip()
    if h.startswith("0x"):
        h = h[2:]
    if len(h) != 64:
        return None
    try:
        return bytes.fromhex(h)
    except ValueError:
        return None


def redeem_winning_position(
    *,
    condition_id: str,
    side: str,
    outcome: str,
    wallet: str,
    private_key: str,
) -> RedeemResult:
    """Call CTF.redeemPositions for a single resolved binary market.

    Returns immediately (without sending a transaction) when:
      * the kill switch is on,
      * there's nothing to redeem (loser, malformed inputs),
      * creds are missing,
      * the condition id isn't 32 bytes of hex.

    Returns after the transaction is mined (or after
    TX_RECEIPT_TIMEOUT_SECONDS elapses) when it does send.

    Args:
      condition_id: 0x-prefixed 32-byte hex from market gamma. The
                    same value we already persist on every
                    pm_positions row.
      side:         'YES' | 'NO' - which side the position held.
      outcome:      'YES' | 'NO' | 'INVALID'.
      wallet:       0x-prefixed Polygon address; must match the
                    private key.
      private_key:  raw hex (with or without 0x prefix). Read from
                    the OS keychain by the caller; never logged.
    """
    if not _live_killswitch_off():
        return RedeemResult(
            redeemed=False,
            tx_hash=None,
            error="kill switch on; on-chain redemption skipped",
        )

    index_sets = index_sets_for_outcome(side, outcome)
    if index_sets is None:
        return RedeemResult(
            redeemed=False,
            tx_hash=None,
            error="no on-chain redemption needed (loser or invalid inputs)",
        )

    if not wallet or not private_key:
        return RedeemResult(
            redeemed=False,
            tx_hash=None,
            error="missing wallet or private key for on-chain redemption",
        )

    cond_bytes = _normalise_condition_id(condition_id)
    if cond_bytes is None:
        return RedeemResult(
            redeemed=False,
            tx_hash=None,
            error=f"condition_id is not 32 bytes of hex: {condition_id!r}",
        )

    # Defer web3 import so a unit-test of index_sets_for_outcome
    # doesn't need the heavy stack.
    try:
        from web3 import Web3
        from eth_account import Account
    except Exception as exc:
        return RedeemResult(
            redeemed=False,
            tx_hash=None,
            error=f"web3/eth_account import failed: {exc}",
        )

    pk = private_key.strip()
    if not pk.startswith("0x"):
        pk = "0x" + pk

    try:
        w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL, request_kwargs={"timeout": 15}))
        acct = Account.from_key(pk)
        if acct.address.lower() != wallet.lower():
            return RedeemResult(
                redeemed=False,
                tx_hash=None,
                error=("private key does not match wallet "
                       f"({acct.address} vs {wallet})"),
            )

        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_REDEEM_ABI,
        )
        nonce = w3.eth.get_transaction_count(acct.address)
        tx = ctf.functions.redeemPositions(
            Web3.to_checksum_address(PUSD_ADDRESS),
            b"\x00" * 32,                                      # parentCollectionId
            cond_bytes,
            list(index_sets),
        ).build_transaction({
            "from":    acct.address,
            "chainId": POLYGON_CHAIN_ID,
            "nonce":   nonce,
            # Let web3 pick gas; the public RPC reliably suggests
            # something sane for Polygon. We don't override gas price
            # because Polygon's EIP-1559 fees are dynamic.
        })
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction")
        sent_hash = w3.eth.send_raw_transaction(raw).hex()
    except Exception as exc:
        # Build / sign / send failure. Don't include the private key
        # in the error text; eth_account does not echo it but be
        # defensive in case web3 ever does.
        return RedeemResult(
            redeemed=False,
            tx_hash=None,
            error=f"redeem broadcast failed: {exc}",
        )

    # Wait for the receipt. If it reverts or times out we still have
    # the hash so the operator can investigate on Polygonscan.
    try:
        receipt = w3.eth.wait_for_transaction_receipt(
            sent_hash, timeout=TX_RECEIPT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return RedeemResult(
            redeemed=False,
            tx_hash=sent_hash,
            error=f"redeem mined timeout: {exc}",
        )

    if int(receipt.get("status", 0)) != 1:
        return RedeemResult(
            redeemed=False,
            tx_hash=sent_hash,
            error="redeem reverted on-chain",
        )

    print(
        f"[pm_redeemer] redeemed condition_id={condition_id} "
        f"side={side} outcome={outcome} tx={sent_hash}",
        file=sys.stderr,
        flush=True,
    )
    return RedeemResult(redeemed=True, tx_hash=sent_hash, error=None)
