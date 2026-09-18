import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from engine import user_config as uc


def _conn(cid: str, provider: str = "anthropic", model: str = "claude-sonnet-5") -> dict:
    return {
        "id": cid, "provider": provider, "label": cid.upper(),
        "model": model, "base_url": "", "api_key": "sk-test",
    }


@pytest.fixture
def store(monkeypatch):
    data = {
        "llm_connections": [
            _conn("a"),
            _conn("b", "gemini", "gemini-flash-latest"),
            _conn("c", "openai", "gpt-5"),
        ],
    }
    monkeypatch.setattr(uc, "_read_secrets", lambda: data)
    monkeypatch.setattr(uc, "_write_secrets", lambda d: data.update(d))
    return data


def _ids(chain: list[dict]) -> list[str]:
    return [c["id"] for c in chain]


def test_legacy_role_slots_migrate_into_ordered_lists(store) -> None:
    store["llm_roles"] = {
        "forecaster_primary": "a", "forecaster_backup": "b",
        "search_primary": "b", "search_backup": None,
    }
    assert uc.get_llm_assignments() == {"forecaster": ["a", "b"], "search": ["b"], "review": []}


def test_set_assignments_keeps_order_drops_unknowns_and_rewrites_roles(store) -> None:
    clean = uc.set_llm_assignments({
        "forecaster": ["c", "a", "c", "zzz"],
        "search": [],
        "review": ["b"],
        "bogus": ["a"],
    })
    assert clean == {"forecaster": ["c", "a"], "search": [], "review": ["b"]}
    assert store["llm_assignments"] == clean
    roles = uc.get_llm_roles()
    assert roles["forecaster_primary"] == "c"
    assert roles["forecaster_backup"] == "a"
    assert roles["search_primary"] is None and roles["search_backup"] is None
    assert store["llm_roles"] == roles


def test_chain_follows_priority_and_falls_back_to_forecasting(store) -> None:
    uc.set_llm_assignments({"forecaster": ["a", "b"], "search": [], "review": []})
    assert _ids(uc.resolve_llm_chain("forecaster")) == ["a", "b"]
    assert _ids(uc.resolve_llm_chain("search")) == ["a", "b"]
    assert _ids(uc.resolve_llm_chain("review")) == ["a", "b"]
    assert uc.has_dedicated_search_connection() is False

    uc.set_llm_assignments({"forecaster": ["a", "b"], "search": ["c"], "review": ["b", "a"]})
    assert _ids(uc.resolve_llm_chain("search")) == ["c"]
    assert _ids(uc.resolve_llm_chain("review")) == ["b", "a"]
    assert uc.has_dedicated_search_connection() is True


def test_use_everywhere_puts_the_connection_first_in_every_job(store) -> None:
    uc.set_llm_assignments({"forecaster": ["a", "b"], "search": ["c"], "review": []})
    result = uc.assign_connection_everywhere("b")
    assert result == {"forecaster": ["b", "a"], "search": ["b", "c"], "review": ["b"]}
    with pytest.raises(ValueError):
        uc.assign_connection_everywhere("nope")


def test_deleting_a_connection_prunes_every_list(store) -> None:
    uc.set_llm_assignments({"forecaster": ["a", "b"], "search": ["a"], "review": ["a", "c"]})
    assert uc.delete_llm_connection("a") is True
    assert uc.get_llm_assignments() == {"forecaster": ["b"], "search": [], "review": ["c"]}
    assert uc.get_llm_roles()["forecaster_primary"] == "b"
    assert uc.delete_llm_connection("a") is False


def test_legacy_set_roles_writes_the_ordered_lists(store) -> None:
    uc.set_llm_assignments({"forecaster": [], "search": [], "review": ["c"]})
    roles = uc.set_llm_roles({
        "forecaster_primary": "b", "forecaster_backup": "a",
        "search_primary": None, "search_backup": "c",
    })
    assert roles["forecaster_primary"] == "b" and roles["forecaster_backup"] == "a"
    assert uc.get_llm_assignments() == {"forecaster": ["b", "a"], "search": ["c"], "review": ["c"]}


def test_first_connection_lands_on_forecasting_later_ones_do_not(store) -> None:
    store["llm_connections"] = []
    a = uc.add_llm_connection(_conn("a"))
    assert uc.get_llm_assignments() == {"forecaster": [a["id"]], "search": [], "review": []}
    assert _ids(uc.resolve_llm_chain("search")) == [a["id"]]
    assert _ids(uc.resolve_llm_chain("review")) == [a["id"]]
    b = uc.add_llm_connection(_conn("b", "gemini", "gemini-flash-latest"))
    assert uc.get_llm_assignments()["forecaster"] == [a["id"]]
    assert b["id"] not in uc.get_llm_assignments()["forecaster"]


def test_setup_flags_do_not_depend_on_the_provider(store) -> None:
    uc.set_llm_assignments({"forecaster": [], "search": [], "review": []})
    assert uc.llm_setup_flags() == {
        "has_llm_key": False, "has_llm_backup_key": False, "has_search_llm": False,
    }
    uc.set_llm_assignments({"forecaster": ["c"], "search": [], "review": []})
    assert uc.llm_setup_flags() == {
        "has_llm_key": True, "has_llm_backup_key": False, "has_search_llm": False,
    }
    uc.set_llm_assignments({"forecaster": ["c", "a"], "search": ["b"], "review": []})
    assert uc.llm_setup_flags() == {
        "has_llm_key": True, "has_llm_backup_key": True, "has_search_llm": True,
    }
