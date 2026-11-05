"""TEMPORARY (commit 1 of the safe_fetch / media_policy split; deleted in commit 2 together with the bridge).

`engine.render.asset_sourcer` ends with a forwarding bridge: reads, writes and deletes of the OLD names of the
moved definitions (and `httpx`, `threading`, `socket`) go to the module that owns them now. It exists so the
unmodified suite can drive the moved code with the patches it already uses. This file proves the bridge itself,
driven by the oracle in tests/_seam_oracle.py and not by the bridge's own table (spec 6, "Commit 1").
"""
import importlib
import types
from unittest import mock

import pytest

from engine.render import asset_sourcer as AS
from tests._seam_oracle import BRIDGE_EXTRAS, MOVED

_MODULE_PATH = "engine.render.asset_sourcer"

# old name -> (owner module name, name there), from the ORACLE (not from the bridge table)
EXPECTED = {old: (owner, new) for old, (owner, new, _kind) in MOVED.items()}
EXPECTED.update({name: ("safe_fetch", name) for name in BRIDGE_EXTRAS})
NAMES = sorted(EXPECTED)


def _owner(old):
    return importlib.import_module(f"engine.render.{EXPECTED[old][0]}")


def _new(old):
    return EXPECTED[old][1]


def _owner_value(old):
    return getattr(_owner(old), _new(old))


def test_the_bridge_is_installed():
    assert type(AS) is not types.ModuleType, "asset_sourcer is a plain module: the bridge class swap did not happen"
    assert type(AS).__name__ == "_BridgeModule"


def test_the_table_is_exactly_the_oracle():
    assert set(AS._BRIDGE_TABLE) == set(MOVED) | set(BRIDGE_EXTRAS)
    assert {k: tuple(v) for k, v in AS._BRIDGE_TABLE.items()} == EXPECTED


def test_every_owner_has_the_new_name():
    missing = [f"{EXPECTED[o][0]}.{EXPECTED[o][1]}" for o in NAMES if not hasattr(_owner(o), _new(o))]
    assert not missing


def test_no_moved_name_is_in_the_module_dict():
    # a leftover copy would shadow __getattr__ and silently stop forwarding
    assert sorted(set(vars(AS)) & set(EXPECTED)) == []


def test_a_name_outside_the_table_raises_attribute_error():
    with pytest.raises(AttributeError, match="no_such_name_in_the_table"):
        AS.no_such_name_in_the_table
    assert not hasattr(AS, "no_such_name_in_the_table")
    # a name that stays in asset_sourcer is a normal module attribute
    assert AS.settings is importlib.import_module("api.config").settings
    assert "settings" in vars(AS)


def test_a_name_that_exists_on_an_owner_but_is_not_in_the_table_is_not_forwarded():
    # the table is the whole contract: new names (`now`, `open_download`, ...) and the owners' own imports
    # (`zlib`, `ContextVar`, ...) are not reachable through asset_sourcer
    names = set()
    for owner in ("safe_fetch", "media_policy"):
        names |= {n for n in dir(importlib.import_module(f"engine.render.{owner}")) if not n.startswith("__")}
    leaked = sorted(n for n in names - set(vars(AS)) - set(EXPECTED) if hasattr(AS, n))
    assert leaked == []
    assert {"now", "open_download", "zlib", "image_ok"} <= names - set(vars(AS)) - set(EXPECTED)


def test_setattr_and_delattr_of_a_non_table_name_behave_as_on_a_module():
    assert "_probe_not_in_the_table" not in EXPECTED
    AS._probe_not_in_the_table = 7
    try:
        assert vars(AS)["_probe_not_in_the_table"] == 7
        assert AS._probe_not_in_the_table == 7
    finally:
        del AS._probe_not_in_the_table
    assert "_probe_not_in_the_table" not in vars(AS)
    with pytest.raises(AttributeError):
        del AS._probe_not_in_the_table
    for owner in ("safe_fetch", "media_policy"):
        assert not hasattr(importlib.import_module(f"engine.render.{owner}"), "_probe_not_in_the_table")


@pytest.mark.parametrize("old", NAMES)
def test_from_import_binds_the_owners_value(old):
    namespace = {}
    exec(f"from {_MODULE_PATH} import {old} as bound", namespace)       # the oracle's names only
    assert namespace["bound"] is _owner_value(old)


@pytest.mark.parametrize("old", NAMES)
def test_a_read_reaches_the_owner(old):
    assert getattr(AS, old) is _owner_value(old)
    assert old not in vars(AS)


@pytest.mark.parametrize("old", NAMES)
def test_monkeypatch_setattr_reaches_the_owner_and_undoes(old):
    original, sentinel = _owner_value(old), object()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(AS, old, sentinel)
        assert _owner_value(old) is sentinel
        assert getattr(AS, old) is sentinel
        assert old not in vars(AS)
    assert _owner_value(old) is original
    assert old not in vars(AS)


@pytest.mark.parametrize("old", NAMES)
def test_patch_object_reaches_the_owner_and_restores_through_delattr_hasattr_setattr(old):
    original, sentinel = _owner_value(old), object()
    with mock.patch.object(AS, old, sentinel):
        assert _owner_value(old) is sentinel
        assert getattr(AS, old) is sentinel
    assert _owner_value(old) is original
    assert hasattr(AS, old)
    assert old not in vars(AS)


@pytest.mark.parametrize("old", NAMES)
def test_patch_with_a_string_target_reaches_the_owner_and_restores(old):
    original, sentinel = _owner_value(old), object()
    with mock.patch(f"{_MODULE_PATH}.{old}", sentinel):
        assert _owner_value(old) is sentinel
    assert _owner_value(old) is original
    assert old not in vars(AS)


@pytest.mark.parametrize("old", NAMES)
def test_direct_assignment_and_delete_reach_the_owner_and_can_be_restored(old):
    original, sentinel = _owner_value(old), object()
    owner, new = _owner(old), _new(old)
    try:
        setattr(AS, old, sentinel)
        assert getattr(owner, new) is sentinel
        delattr(AS, old)
        assert not hasattr(owner, new)
        assert not hasattr(AS, old)
    finally:
        setattr(AS, old, original)
    assert getattr(owner, new) is original
    assert getattr(AS, old) is original
    assert old not in vars(AS)
