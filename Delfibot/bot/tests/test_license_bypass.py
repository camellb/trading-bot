import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))


OLD_PUBLIC_TOKEN = "DELFI-OWNER-LOCAL-2026"


def _reload_license(monkeypatch, env_value):
    if env_value is None:
        monkeypatch.delenv("DELFI_OWNER_BYPASS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("DELFI_OWNER_BYPASS_TOKEN", env_value)
    from engine import license as lic
    return importlib.reload(lic)


def test_old_public_token_no_longer_unlocks(monkeypatch) -> None:
    # The constant used to ship in the public repo and in every binary;
    # pasting it into Settings unlocked any install for free.
    lic = _reload_license(monkeypatch, None)
    assert lic.OWNER_BYPASS_TOKEN is None
    result = lic.verify_license(OLD_PUBLIC_TOKEN)
    assert result.valid is False


def test_env_token_unlocks_only_the_matching_value(monkeypatch) -> None:
    lic = _reload_license(monkeypatch, "DELFI-OWNER-test-only-value")
    assert lic.verify_license("DELFI-OWNER-test-only-value").valid is True
    assert lic.verify_license(OLD_PUBLIC_TOKEN).valid is False
    assert lic.verify_license("").valid is False


def test_blank_env_disables_bypass(monkeypatch) -> None:
    lic = _reload_license(monkeypatch, "   ")
    assert lic.OWNER_BYPASS_TOKEN is None
    assert lic.verify_license("   ").valid is False
