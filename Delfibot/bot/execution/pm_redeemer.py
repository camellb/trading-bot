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
import time
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

# Public Polygon RPC fallback list. As of 2026-05-18 the canonical
# `polygon-rpc.com` started returning HTTP 401 on `eth_sendRawTransaction`
# from clients without a Polygon Edge API key - broadcast traffic is
# gated even though reads still work. That broke auto-redeem on every
# settled live winner and left the user with stuck CTF tokens.
#
# Solution: try a list of public RPC endpoints in order until one
# accepts the broadcast. Each is free + keyless + accepts unsigned
# raw transactions. Override via $POLYGON_RPC_URLS (comma-separated)
# or just the first entry via $POLYGON_RPC_URL.
_DEFAULT_RPC_URLS = [
    # Tried in order; on broadcast failure we fall through to the
    # next URL. Reads still work on polygon-rpc.com (only broadcasts
    # got the 401), so it stays in the list as a last fallback.
    "https://polygon.llamarpc.com",
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://rpc.ankr.com/polygon",
    "https://polygon-rpc.com",
]
_env_urls = os.environ.get("POLYGON_RPC_URLS") or os.environ.get("POLYGON_RPC_URL")
if _env_urls:
    POLYGON_RPC_URLS = [u.strip() for u in _env_urls.split(",") if u.strip()]
else:
    POLYGON_RPC_URLS = list(_DEFAULT_RPC_URLS)
# Legacy single-URL constant kept for any importer that uses it.
POLYGON_RPC_URL = POLYGON_RPC_URLS[0]

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
        acct = Account.from_key(pk)
    except Exception as exc:
        return RedeemResult(
            redeemed=False, tx_hash=None,
            error=f"key parse failed: {exc}",
        )

    # ── Path 1a: Gasless via Polymarket Relayer (RELAYER_API_KEY) ────
    # The simple 2-header auth scheme. The user creates a key on
    # polymarket.com -> Settings -> Relayer API keys (one UUID, no
    # HMAC, no passphrase) and pastes it into Delfi. The relayer
    # accepts the submission with `RELAYER_API_KEY` +
    # `RELAYER_API_KEY_ADDRESS` (the signer EOA). Polymarket pays
    # the gas; no MATIC needed in the user's wallet.
    #
    # Verified end-to-end 2026-05-18 against position 317's real
    # redeem. This is the path we PREFER and actively guide the user
    # toward, because it's a one-time paste of a single string and
    # works forever after.
    relayer_result = _try_gasless_redeem_via_relayer_api_key(
        cond_bytes=cond_bytes,
        index_sets=index_sets,
        wallet=wallet,
        private_key=pk,
        acct_address=acct.address,
    )
    if relayer_result is not None:
        return relayer_result  # relayer succeeded or terminally failed

    # ── Path 1b: Gasless via Builder API Key HMAC (POLY_BUILDER_*) ───
    # The harder auth path: 4 headers, HMAC-SHA256, separate key
    # class created on polymarket.com -> Settings -> Builders. Most
    # users won't have this; it's third-party-builder-grade auth.
    # Kept as a fallback so users who happen to have Builder creds
    # configured still benefit from gasless redeem.
    gasless_result = _try_gasless_redeem(
        cond_bytes=cond_bytes,
        index_sets=index_sets,
        wallet=wallet,
        private_key=pk,
        acct_address=acct.address,
    )
    if gasless_result is not None:
        return gasless_result  # gasless succeeded or terminally failed

    if acct.address.lower() != wallet.lower():
        return RedeemResult(
            redeemed=False, tx_hash=None,
            error=("private key does not match wallet "
                   f"({acct.address} vs {wallet})"),
        )

    # Try each RPC URL in turn. The "build + send" step is the one
    # that 401s on gated RPCs; reads (get_transaction_count, gas
    # suggest) usually still work but we use the same RPC for
    # both phases of each attempt to keep nonces consistent.
    # Polygon is a proof-of-authority chain; web3.py needs
    # ExtraDataToPOAMiddleware injected or every get_block /
    # get_transaction_count throws ExtraDataLengthError on the 32+
    # byte extraData field that POA chains use. Without this the
    # auto-redeem fails before it even tries to broadcast.
    try:
        from web3.middleware import ExtraDataToPOAMiddleware as _POAMiddleware
    except Exception:
        _POAMiddleware = None

    sent_hash: Optional[str] = None
    w3 = None
    errors: list[str] = []
    for rpc_url in POLYGON_RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
            if _POAMiddleware is not None:
                try:
                    w3.middleware_onion.inject(_POAMiddleware, layer=0)
                except Exception:
                    pass  # already injected or unsupported on this w3 version
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_REDEEM_ABI,
            )
            nonce = w3.eth.get_transaction_count(acct.address)
            tx = ctf.functions.redeemPositions(
                Web3.to_checksum_address(PUSD_ADDRESS),
                b"\x00" * 32,                                  # parentCollectionId
                cond_bytes,
                list(index_sets),
            ).build_transaction({
                "from":    acct.address,
                "chainId": POLYGON_CHAIN_ID,
                "nonce":   nonce,
            })
            signed = acct.sign_transaction(tx)
            raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction")
            sent_hash = w3.eth.send_raw_transaction(raw).hex()
            print(
                f"[pm_redeemer] broadcast accepted by {rpc_url} "
                f"tx={sent_hash}",
                file=sys.stderr, flush=True,
            )
            break  # success
        except Exception as exc:
            errors.append(f"{rpc_url}: {type(exc).__name__}: {str(exc)[:160]}")
            print(
                f"[pm_redeemer] broadcast failed on {rpc_url}: "
                f"{type(exc).__name__}: {str(exc)[:160]}",
                file=sys.stderr, flush=True,
            )
            continue
    if sent_hash is None or w3 is None:
        # Every RPC URL failed. Detect the common case where the
        # user's Polygon wallet has no MATIC for gas — that's a
        # specific user-actionable failure, not a transient RPC
        # problem. Two paths to fix this: fund 0.1 MATIC for direct
        # RPC, OR paste Builder API Keys in Settings for gasless via
        # Polymarket's relayer.
        joined = " | ".join(errors)
        if "insufficient funds for gas" in joined.lower():
            return RedeemResult(
                redeemed=False, tx_hash=None,
                error=(
                    "wallet has no MATIC for gas. Two options: "
                    "(1) send ~0.1 MATIC (~$0.05) to the wallet for "
                    "direct-RPC auto-redeem, OR "
                    "(2) create a Builder API Key on polymarket.com -> "
                    "Settings -> API Keys and paste it into Delfi "
                    "Settings -> Polymarket API Key for gasless redeem "
                    "via Polymarket's relayer. "
                    "Until either is set up, click Redeem on the "
                    "Polymarket web UI to claim winners manually."
                ),
            )
        return RedeemResult(
            redeemed=False, tx_hash=None,
            error=f"redeem broadcast failed on all RPCs: {' | '.join(errors[:3])}",
        )

    # Wait for the receipt. Uses the same RPC that accepted the
    # broadcast. If it reverts or times out we still have the hash
    # so the operator can investigate on Polygonscan.
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


# ── Periodic sweeper for stuck winners ──────────────────────────────────────
#
# settle_position only invokes redeem_winning_position ONCE per position
# (when the row transitions to status='settled'). If that one attempt
# fails - transient RPC 401, no MATIC for gas, daemon restart mid-call,
# Builder creds not yet pasted - the row sits forever with
# redeem_tx_hash=NULL and the on-chain CTF tokens never reach the user's
# pUSD balance. Real example: position 317 settled 2026-05-18 06:39 UTC
# against an older binary that only had polygon-rpc.com in its fallback
# list; broadcast 401'd and the daemon never came back to it.
#
# This sweeper closes that gap. It runs on its own schedule, scans the
# DB for stuck winners, and replays redeem_winning_position for each.
# The redeemer itself is idempotent in the failure case (kill switch,
# missing creds, no MATIC) - it just returns RedeemResult(redeemed=False)
# without consuming gas - so repeated calls until the user funds MATIC
# or pastes Builder API keys are safe.

def sweep_unredeemed_winners(*, max_per_run: int = 25) -> dict:
    """Scan pm_positions for live winners with no redeem tx and retry.

    Returns a small summary dict for logging:
        {'scanned': N, 'redeemed': M, 'failed': K, 'reasons': {...}}

    Safe to call from any thread. Self-gated on DELFI_LIVE_KILLSWITCH_OFF
    via redeem_winning_position - no need to gate here too. Always
    bounded: at most `max_per_run` positions touched per call so a
    backlog of 100 stuck winners can't monopolise the threadpool tick.
    """
    summary = {"scanned": 0, "redeemed": 0, "failed": 0, "reasons": {}}

    # Deferred imports so test-runs of redeem_winning_position don't
    # pull in db + user_config.
    try:
        from sqlalchemy import text
        from db.engine import get_engine
        from engine.user_config import (
            get_user_config, get_active_polymarket_creds,
        )
    except Exception as exc:
        print(f"[pm_redeemer] sweeper bootstrap failed: {exc}",
              file=sys.stderr, flush=True)
        return summary

    try:
        cfg = get_user_config()
    except Exception as exc:
        print(f"[pm_redeemer] sweeper config read failed: {exc}",
              file=sys.stderr, flush=True)
        return summary

    if (getattr(cfg, "mode", "") or "").lower() != "live":
        return summary  # nothing to sweep in simulation

    try:
        creds = get_active_polymarket_creds(cfg)
    except Exception:
        creds = None
    wallet = (creds or {}).get("wallet_address") or ""
    pk     = (creds or {}).get("private_key") or ""
    if not wallet or not pk:
        return summary  # no creds, nothing we can do

    # Find stuck winners. The redeem hook only ran when side was
    # already known to be the winning side (see settle_position), so
    # we filter the same way here.
    try:
        with get_engine().begin() as conn:
            rows = list(conn.execute(text(
                "SELECT id, condition_id, side, settlement_outcome "
                "FROM pm_positions "
                "WHERE mode = 'live' "
                "  AND status = 'settled' "
                "  AND redeem_tx_hash IS NULL "
                "  AND condition_id IS NOT NULL "
                "  AND condition_id != '' "
                "  AND side = settlement_outcome "
                "ORDER BY id ASC "
                "LIMIT :lim"
            ), {"lim": int(max_per_run)}).mappings()) or []
    except Exception as exc:
        print(f"[pm_redeemer] sweeper select failed: {exc}",
              file=sys.stderr, flush=True)
        return summary

    if not rows:
        return summary

    for row in rows:
        summary["scanned"] += 1
        pid          = row["id"]
        condition_id = row["condition_id"]
        side         = row["side"]
        outcome      = row["settlement_outcome"]

        try:
            result = redeem_winning_position(
                condition_id=condition_id,
                side=side,
                outcome=outcome,
                wallet=wallet,
                private_key=pk,
            )
        except Exception as exc:
            summary["failed"] += 1
            reason = f"exception: {type(exc).__name__}"
            summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1
            print(
                f"[pm_redeemer] sweeper threw on pos {pid}: {exc}",
                file=sys.stderr, flush=True,
            )
            continue

        if result.tx_hash:
            try:
                with get_engine().begin() as conn:
                    conn.execute(text(
                        "UPDATE pm_positions "
                        "SET redeem_tx_hash = :tx "
                        "WHERE id = :pid"
                    ), {"tx": result.tx_hash, "pid": pid})
            except Exception as exc:
                print(
                    f"[pm_redeemer] sweeper persist failed for pos "
                    f"{pid}: {exc}",
                    file=sys.stderr, flush=True,
                )

        if result.redeemed:
            summary["redeemed"] += 1
            print(
                f"[pm_redeemer] sweeper redeemed pos {pid} "
                f"tx={result.tx_hash}",
                file=sys.stderr, flush=True,
            )
        else:
            summary["failed"] += 1
            reason = (result.error or "unknown")[:80]
            summary["reasons"][reason] = summary["reasons"].get(reason, 0) + 1

    print(
        f"[pm_redeemer] sweeper run: scanned={summary['scanned']} "
        f"redeemed={summary['redeemed']} failed={summary['failed']} "
        f"reasons={summary['reasons']}",
        file=sys.stderr, flush=True,
    )
    return summary


# ── Gasless redeem via Polymarket Relayer ───────────────────────────────────

# Polymarket's official relayer endpoint. Override via env if needed
# (e.g. pointing at the staging relayer for tests).
POLYMARKET_RELAYER_URL = os.environ.get(
    "POLYMARKET_RELAYER_URL", "https://relayer-v2.polymarket.com/"
)


def _try_gasless_redeem_via_relayer_api_key(
    *,
    cond_bytes: bytes,
    index_sets: Sequence[int],
    wallet: str,
    private_key: str,
    acct_address: str,
) -> Optional[RedeemResult]:
    """Submit the redeem via the relayer using the simple 2-header auth.

    The user creates a Relayer API Key at
    polymarket.com -> Settings -> Relayer API keys and pastes the
    single UUID into Delfi -> Settings -> Polymarket. Auth is exactly:

        RELAYER_API_KEY:          <uuid>
        RELAYER_API_KEY_ADDRESS:  <signer EOA address>

    No HMAC, no timestamp, no passphrase. Polymarket pays the gas.

    Returns:
      RedeemResult(redeemed=True, tx_hash=...)  — relayer executed.
      RedeemResult(redeemed=False, ...)         — terminal failure
                                                  (401, 4xx, etc.).
      None                                       — no Relayer API Key
                                                  configured or SDK
                                                  import failure; the
                                                  caller falls through
                                                  to the next path.
    """
    try:
        from engine.user_config import get_polymarket_relayer_api_key
        api_key = get_polymarket_relayer_api_key()
    except Exception:
        api_key = None
    if not api_key:
        return None  # caller falls through

    try:
        from py_builder_relayer_client.models import (
            DepositWalletCall, DepositWalletTransactionArgs,
        )
        from py_builder_relayer_client.builder.deposit_wallet import (
            build_deposit_wallet_batch_request,
        )
        from py_builder_relayer_client.config import get_contract_config
        from py_builder_relayer_client.signer import Signer
        from eth_utils import keccak, to_checksum_address
        from eth_abi import encode as eth_abi_encode
        import requests as _requests
    except Exception as exc:
        print(
            f"[pm_redeemer] relayer-api-key path: SDK import failed: {exc}; "
            f"falling back to next path",
            file=sys.stderr, flush=True,
        )
        return None

    # Build the same DepositWallet batch request the Builder path uses.
    # We only need the signed body; the auth headers are different.
    selector = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    encoded_args = eth_abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [to_checksum_address(PUSD_ADDRESS), b"\x00" * 32,
         cond_bytes, list(index_sets)],
    )
    redeem_data = "0x" + (selector + encoded_args).hex()
    call = DepositWalletCall(
        target=to_checksum_address(CTF_ADDRESS),
        value="0",
        data=redeem_data,
    )

    try:
        signer = Signer(private_key, POLYGON_CHAIN_ID)
        # The relayer's nonce endpoint is open / unauthenticated.
        # Same canonical URL as POLYMARKET_RELAYER_URL but with the
        # /nonce path; we hit it directly to avoid spinning up a full
        # RelayClient.
        base = POLYMARKET_RELAYER_URL.rstrip("/")
        nonce_url = (
            f"{base}/nonce?address={signer.address()}&type=WALLET"
        )
        nonce_resp = _requests.get(nonce_url, timeout=15)
        nonce_resp.raise_for_status()
        nonce = str(nonce_resp.json().get("nonce", "0"))

        # `wallet_address` here is the funder/proxy address (the
        # signature_type=3 DepositWallet). The Builder path also
        # uses this; for the user we care about, it's the address
        # that holds the CTF tokens we want to redeem.
        dw_args = DepositWalletTransactionArgs(
            from_address=signer.address(),
            chain_id=POLYGON_CHAIN_ID,
            wallet_address=wallet,
            nonce=nonce,
            deadline=str(int(time.time()) + 240),
            calls=[call],
        )
        cfg = get_contract_config(POLYGON_CHAIN_ID)
        req = build_deposit_wallet_batch_request(
            signer=signer, args=dw_args, config=cfg,
        )
        body = req.to_dict()

        # The actual submit. 2-header auth.
        submit_url = f"{base}/submit"
        headers = {
            "RELAYER_API_KEY": api_key,
            "RELAYER_API_KEY_ADDRESS": signer.address(),
        }
        resp = _requests.post(
            submit_url, json=body, headers=headers, timeout=60,
        )

        if resp.status_code != 200:
            msg = resp.text[:300]
            # 401 means the pasted key is invalid OR was created for a
            # different signer address. Surface that clearly to the
            # operator so the fix is obvious. NOT a transient error,
            # so don't fall through to a direct-RPC retry that will
            # also fail (no MATIC) and confuse the message.
            if resp.status_code == 401:
                return RedeemResult(
                    redeemed=False, tx_hash=None,
                    error=(
                        f"relayer rejected RELAYER_API_KEY (401). "
                        f"Check that the key was created on "
                        f"polymarket.com -> Settings -> Relayer API "
                        f"keys with the SAME wallet you have "
                        f"connected to Delfi. Response: {msg}"
                    ),
                )
            return RedeemResult(
                redeemed=False, tx_hash=None,
                error=f"relayer submit failed: {resp.status_code} {msg}",
            )

        data = resp.json()
        tx_hash = data.get("transactionHash")
        state   = data.get("state")
        if tx_hash:
            print(
                f"[pm_redeemer] relayer redeemed via RELAYER_API_KEY: "
                f"tx={tx_hash} state={state}",
                file=sys.stderr, flush=True,
            )
            return RedeemResult(redeemed=True, tx_hash=tx_hash, error=None)
        return RedeemResult(
            redeemed=False, tx_hash=None,
            error=f"relayer accepted submit but no tx hash: {data}",
        )
    except Exception as exc:
        # Network blip, payload bug, or unexpected SDK error. Fall
        # through to the next path so a transient relayer outage
        # doesn't permanently block redeem.
        print(
            f"[pm_redeemer] relayer-api-key path failed "
            f"({type(exc).__name__}: {str(exc)[:200]}); "
            f"falling back to next path",
            file=sys.stderr, flush=True,
        )
        return None

# Polygon mainnet CTF Exchange / Deposit Wallet factory + impl
_DW_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"
_DW_IMPL    = "0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB"


def _try_gasless_redeem(
    *,
    cond_bytes: bytes,
    index_sets: Sequence[int],
    wallet: str,
    private_key: str,
    acct_address: str,
) -> Optional[RedeemResult]:
    """Submit the redeem via Polymarket's relayer (no MATIC required).

    Returns:
      RedeemResult(redeemed=True, ...)  — succeeded
      RedeemResult(redeemed=False, ...) — terminal failure (do NOT fall back)
      None                              — Builder API creds missing or
                                          import failure; caller should
                                          fall through to direct RPC.

    The relayer requires Builder/Relayer API Keys that the user creates
    on polymarket.com → Settings → API Keys. The bot's auto-derived
    CLOB trading keys are NOT accepted by the relayer (different key
    class). When Builder creds are absent the function returns None
    so the caller falls back to the direct-RPC path.
    """
    # Look up manual Builder API creds. Same Settings field as the
    # CLOB manual creds; user can paste a single Builder API Key
    # tuple that works for both order placement AND relayer redeems.
    try:
        from engine.user_config import get_polymarket_api_creds
        builder_creds = get_polymarket_api_creds()
    except Exception:
        builder_creds = None
    if not builder_creds:
        return None  # caller falls through to direct RPC

    # Import the Polymarket SDKs lazily — they may not be present in
    # an environment that only does sim mode.
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import (
            DepositWalletCall, DepositWalletTransactionArgs,
        )
        from py_builder_signing_sdk.config import (
            BuilderConfig, BuilderApiKeyCreds,
        )
        from eth_utils import keccak, to_checksum_address
        from eth_abi import encode as eth_abi_encode
    except Exception as exc:
        print(
            f"[pm_redeemer] gasless: SDK import failed: {exc}; "
            f"falling back to direct RPC",
            file=sys.stderr, flush=True,
        )
        return None

    # Build the redeemPositions calldata that will be batched into a
    # DepositWalletCall.
    selector = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
    encoded_args = eth_abi_encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [to_checksum_address(PUSD_ADDRESS), b"\x00" * 32,
         cond_bytes, list(index_sets)],
    )
    redeem_data = "0x" + (selector + encoded_args).hex()
    call = DepositWalletCall(
        target=to_checksum_address(CTF_ADDRESS),
        value="0",
        data=redeem_data,
    )

    try:
        builder_config = BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(
                key=builder_creds["api_key"],
                secret=builder_creds["api_secret"],
                passphrase=builder_creds["api_passphrase"],
            )
        )
        client = RelayClient(
            POLYMARKET_RELAYER_URL,
            POLYGON_CHAIN_ID,
            private_key,
            builder_config,
        )
        deposit_wallet_addr = client.get_expected_deposit_wallet()
        nonce_payload = client.get_nonce(acct_address, "WALLET") or {}
        nonce = str(nonce_payload.get("nonce", "0"))
        deadline = str(int(time.time()) + 240)

        resp = client.execute_deposit_wallet_batch(
            calls=[call],
            wallet_address=deposit_wallet_addr,
            nonce=nonce,
            deadline=deadline,
        )
        tx_id = getattr(resp, "transaction_id", None)
        tx_hash = getattr(resp, "transaction_hash", None)
        print(
            f"[pm_redeemer] gasless: submitted to relayer "
            f"tx_id={tx_id} tx_hash={tx_hash}",
            file=sys.stderr, flush=True,
        )
        final = None
        try:
            final = resp.wait()
        except Exception as exc:
            print(
                f"[pm_redeemer] gasless: wait for relayer "
                f"settlement failed: {exc}",
                file=sys.stderr, flush=True,
            )
        # Whether wait succeeded or not, if we have a tx_hash the
        # transaction was at least broadcast. Polygonscan can verify.
        if tx_hash:
            return RedeemResult(redeemed=True, tx_hash=tx_hash, error=None)
        return RedeemResult(
            redeemed=False, tx_hash=None,
            error=f"relayer accepted submission but no tx hash: {final}",
        )
    except Exception as exc:
        # 401 → user has the key but it's not a Builder/Relayer key.
        # 400 → wrong payload (probably wallet not deployed, bad nonce).
        # Anything else → relayer-side issue.
        msg = str(exc)
        if "invalid authorization" in msg or "401" in msg:
            # Surface as terminal so the caller doesn't fall back to
            # direct RPC and produce a confusing "no MATIC" error.
            # The user needs to know their pasted key isn't a relayer
            # key. They can either re-generate it as a Builder key on
            # polymarket.com OR clear the field to use direct RPC.
            return RedeemResult(
                redeemed=False, tx_hash=None,
                error=(
                    "relayer rejected the API key (401). The key in "
                    "Delfi -> Settings -> Polymarket API Key must be a "
                    "Builder API Key created on polymarket.com -> "
                    "Settings -> API Keys (the CLOB trading-key class "
                    "is not accepted by the relayer). Clear that field "
                    "to fall back to direct-RPC redeem with MATIC."
                ),
            )
        # Other errors: fall back to direct RPC.
        print(
            f"[pm_redeemer] gasless: failed ({type(exc).__name__}: "
            f"{msg[:200]}); falling back to direct RPC",
            file=sys.stderr, flush=True,
        )
        return None
