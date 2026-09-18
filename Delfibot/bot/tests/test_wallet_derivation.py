import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from eth_account import Account

sys.path.insert(0, str(Path(__file__).parents[1]))

from engine import user_config as uc


@pytest.fixture
def acct():
    # Generated per run: no key material lives in the repo.
    a = Account.create()
    return SimpleNamespace(key=a.key.hex(), address=a.address)


@pytest.fixture
def cfg_store(monkeypatch):
    state = {"wallet_address": None, "key": None, "writes": []}

    def _update(user_id=uc.DEFAULT_USER_ID, **changes):
        state["writes"].append(changes)
        state.update(changes)

    monkeypatch.setattr(uc, "update_user_config", _update)
    monkeypatch.setattr(uc, "get_user_config",
                        lambda user_id=uc.DEFAULT_USER_ID: SimpleNamespace(
                            wallet_address=state["wallet_address"]))
    monkeypatch.setattr(uc, "_keyring_set", lambda k, v: state.__setitem__("key", v or None))
    monkeypatch.setattr(uc, "_keyring_get", lambda k: state["key"])
    return state


def test_derive_accepts_prefixed_and_bare_hex_and_rejects_garbage(acct) -> None:
    bare = acct.key[2:] if acct.key.startswith("0x") else acct.key
    assert uc.derive_polymarket_address("0x" + bare) == acct.address
    assert uc.derive_polymarket_address(bare) == acct.address
    assert uc.derive_polymarket_address("  0x" + bare + "  ") == acct.address
    assert uc.derive_polymarket_address("") is None
    assert uc.derive_polymarket_address("0x1234") is None
    assert uc.derive_polymarket_address("zz" * 32) is None


def test_saving_the_key_alone_also_stores_the_wallet(cfg_store, acct) -> None:
    uc.set_user_polymarket_creds(private_key=acct.key)
    assert cfg_store["key"] == acct.key
    assert cfg_store["wallet_address"] == acct.address


def test_an_explicit_wallet_is_written_once_and_not_overridden(cfg_store, acct) -> None:
    uc.set_user_polymarket_creds(private_key=acct.key, wallet_address=acct.address)
    assert cfg_store["writes"] == [{"wallet_address": acct.address}]


def test_removing_the_key_does_not_derive_anything(cfg_store) -> None:
    uc.set_user_polymarket_creds(private_key="")
    assert cfg_store["writes"] == []


def test_heal_fills_an_empty_wallet_and_fixes_a_stale_one(cfg_store, acct) -> None:
    assert uc.heal_wallet_address() is None          # no key stored
    cfg_store["key"] = acct.key
    assert uc.heal_wallet_address() == acct.address   # empty wallet
    assert cfg_store["wallet_address"] == acct.address
    assert uc.heal_wallet_address() is None          # already matches
    cfg_store["wallet_address"] = "0x" + "11" * 20    # a pasted deposit address
    assert uc.heal_wallet_address() == acct.address
    cfg_store["wallet_address"] = acct.address.lower()
    assert uc.heal_wallet_address() is None          # case-insensitive match
