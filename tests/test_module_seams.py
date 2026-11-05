"""Guards and consumer-level probes for the safe_fetch / media_policy seams
(spec docs/specs/2026-11-safe-fetch-media-policy-module-design.md, 4.5 and 6 "Commit 3").

`engine/render/asset_sourcer.py` was split into the adapters (it keeps), `safe_fetch` (hardened HTTP, the clock and
the asset budget) and `media_policy` (the admit / downscale / refuse decision for downloaded media). A patch aimed
at the wrong module binds nothing and the real code runs, and a seam captured by a from-import, an alias or a
default argument silently ignores a test's patch. These tests make both a failing test.

The AST checkers live in tests/_seam_ast.py (standard library only, so the fidelity script can reuse them); every
rule here has
  * a positive control: the checker finds a realistic number of things on the REAL tree and passes it, and
  * synthetic violations: the same checker, fed small in-memory sources, fires on each forbidden form.

G1  no stale / moved-name access through asset_sourcer in tests; nobody patches `safe_fetch.now` (D2)
G2  patches of the three modules are strict (no raising=False / create=True) and every target resolves
G3  production code never captures a seam (module-attribute access only, callables only called or caught)
G4  one clock, one ContextVar
G5  dependency direction, completeness of the move, no bridge left behind
G6  asset_sourcer has no transport; url_ok is one of the two adapter predicates; API URLs are constant-rooted

The second half is behaviour: seam probes (patch a sentinel on the OWNING module and watch each consumer use it),
fixture effectiveness at the consumer (the conftest switch-offs are on for unmarked tests and off for the four
`real_*` markers), `now()` and the logger names.

Run with `python -m pytest` (a bare `pytest` from a worktree can import the main checkout).
"""
import ast
import importlib
import logging
import socket
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from engine.render import asset_sourcer as AS
from engine.render import media_policy, safe_fetch
from engine.render.asset_sourcer import HuggingFaceImageSource, HuggingFaceVideoSource, PexelsVideoSource
from tests import _seam_ast as S
from tests.test_sourcer_download_guards import (
    PEXELS_OK, WIKI_OK, _Resp, _Stream, _fake_clock, _pexels, _wiki,
)
from tests.test_sourcer_selection import _summary, _video, _vf

ROOT = S.ROOT
O = S.O
MODULES = {"asset_sourcer": AS, "safe_fetch": safe_fetch, "media_policy": media_policy}


def _test_sources():
    return {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted((ROOT / "tests").glob("**/*.py"))}


TESTS = _test_sources()
PROD = S.production_sources()
ASSET_PATH = "engine/render/asset_sourcer.py"
SAFE_PATH = "engine/render/safe_fetch.py"
MEDIA_PATH = "engine/render/media_policy.py"


def _fmt(problems):
    return "\n".join(str(p) for p in problems)


def _no_problems(problems):
    assert not problems, "\n" + _fmt(problems)


def _fires(problems, rule=None):
    assert problems, "the guard did not fire"
    if rule is not None:
        assert any(p.rule == rule for p in problems), _fmt(problems)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# the modules under test come from THIS checkout
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_the_modules_under_test_come_from_this_checkout():
    """A bare `pytest` from a worktree can import the main checkout; the guards read files under ROOT."""
    for module in MODULES.values():
        assert Path(module.__file__).resolve().is_relative_to(ROOT.resolve()), module.__file__


# ══════════════════════════════════════════════════════════════════════════════════════════════
# G1 - no stale or moved-name access through asset_sourcer in tests
# ══════════════════════════════════════════════════════════════════════════════════════════════
# Recorded on the retargeted tree (spec section 10). These are deterministic; a new patch of a moved seam in the
# suite changes them on purpose, and this is the one place to update.
G1_EXPECTED_COUNTS = {
    ("asset_sourcer", "f{_LOG}"): 6,
    ("asset_sourcer", "f{_MOD}"): 18,
    ("asset_sourcer", "literal"): 5,
    ("asset_sourcer", "object:literal"): 53,
    ("asset_sourcer", "object:name"): 2,
    ("media_policy", "literal"): 2,
    ("media_policy", "object:literal"): 80,
    ("safe_fetch", "f{_FETCH_MOD}"): 24,
    ("safe_fetch", "literal"): 3,
    ("safe_fetch", "object:literal"): 49,
}


def test_G1_the_real_test_tree_has_no_stale_access():
    _no_problems(S.g1_problems(TESTS))


def test_G1_positive_control_the_resolver_finds_every_form_on_the_real_tree_exactly():
    counts = S.g1_counts(TESTS)
    assert counts == G1_EXPECTED_COUNTS, (
        "the per-form figures of resolved patch targets changed; if a test was added or retargeted on purpose, "
        f"update G1_EXPECTED_COUNTS (and spec section 10) to:\n{counts}")
    # the figures are not vacuous: every form the suite uses is represented, per owning module
    assert sum(counts.values()) == 242
    assert {owner for owner, _ in counts} == {"asset_sourcer", "safe_fetch", "media_policy"}


def test_G1_positive_control_the_excluded_files_are_not_scanned_but_still_feed_constants():
    assert S.G1_EXCLUDED == ("tests/test_module_seams.py", "tests/_seam_oracle.py")
    assert all(p in TESTS for p in S.G1_EXCLUDED)
    # this file patches safe_fetch / media_policy a lot: scanning it would change the figures
    counts_with_this_file = S.g1_counts(TESTS, exclude=("tests/_seam_oracle.py",))
    assert counts_with_this_file != G1_EXPECTED_COUNTS


_G1_HEAD = (
    "import pytest\n"
    "from unittest.mock import patch\n"
    "from engine.render import asset_sourcer as AS\n"
    "from engine.render import safe_fetch, media_policy\n"
    "_MOD = 'engine.render.asset_sourcer'\n"
)


def _g1(body, *, head=_G1_HEAD, extra=None):
    sources = {"tests/test_x.py": head + body}
    if extra:
        sources.update(extra)
    return S.g1_problems(sources)


@pytest.mark.parametrize("body", [
    "def t():\n    return AS._http_stream\n",
    "def t():\n    AS._monotonic = lambda: 0.0\n",
    "def t():\n    del AS._monotonic\n",
    "def t(clock):\n    AS._monotonic, saved = clock, AS._monotonic\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(AS, '_image_ok', lambda s: True)\n",
    "def t():\n    with patch.object(AS, '_api_get'):\n        pass\n",
    "def t():\n    delattr(AS, '_api_get')\n",
    "def t():\n    return getattr(AS, '_api_get')\n",
    "def t():\n    setattr(AS, '_api_post', 1)\n",
    "def t(monkeypatch):\n    monkeypatch.delattr(AS, '_open_download')\n",
    "def t():\n    with patch('engine.render.asset_sourcer._http_stream'):\n        pass\n",
    "def t():\n    with patch(f'{_MOD}._http_stream'):\n        pass\n",
    "def t():\n    with patch(f'{AS.__name__}._http_stream'):\n        pass\n",
    "def t():\n    with patch(_MOD + '._http_stream'):\n        pass\n",
    "def t(monkeypatch):\n    monkeypatch.setattr('engine.render.asset_sourcer._image_ok', lambda s: True)\n",
    "@patch('engine.render.asset_sourcer._sniff_ok')\ndef t(m):\n    pass\n",
    "def t():\n    with patch(f'{_MOD}.httpx.get'):\n        pass\n",
    "def t():\n    with patch('engine.render.asset_sourcer.httpx.get'):\n        pass\n",
    "def t():\n    return AS.httpx\n",
    "def t():\n    return AS.threading.Timer\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(AS.httpx, 'get', 1)\n",
    "def t():\n    return AS.socket\n",
    "def t():\n    with patch.multiple(AS, _api_get=1):\n        pass\n",
    "def t():\n    with patch.multiple('engine.render.asset_sourcer', _api_get=1):\n        pass\n",
    "@pytest.mark.parametrize('name', ['_api_get'])\ndef t(monkeypatch, name):\n    monkeypatch.setattr(AS, name, 1)\n",
    "@pytest.mark.parametrize('cls,name', [(1, '_api_get')])\ndef t(monkeypatch, cls, name):\n    monkeypatch.setattr(AS, name, 1)\n",
    "@pytest.mark.parametrize('cls, name', [(1, 'settings'), (2, '_api_get')])\ndef t(monkeypatch, cls, name):\n    monkeypatch.setattr(AS, name, 1)\n",
    "def t(monkeypatch):\n    for n in ('_atomic_write', '_api_get'):\n        monkeypatch.setattr(AS, n, 1)\n",
    "def t():\n    M = AS\n    return M._api_get\n",
    "def t():\n    with patch(f'{_MOD}.RENDER_ASSET_BUDGET_S', 1):\n        pass\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(target=AS, name='_api_get', value=1)\n",
    "from engine.render.asset_sourcer import _api_get\n",
    "from engine.render.asset_sourcer import WikipediaImageSource, _fit_image\n",
])
def test_G1_fires_on_each_stale_form(body):
    _fires(_g1(body), "G1")


@pytest.mark.parametrize("head, body", [
    ("import engine.render.asset_sourcer as X\n", "def t():\n    return X._open_download\n"),
    ("import engine.render.asset_sourcer\n", "def t():\n    return engine.render.asset_sourcer._open_download\n"),
    ("from engine import render\n", "def t():\n    return render.asset_sourcer._open_download\n"),
    ("from engine.render import asset_sourcer\n", "def t():\n    return asset_sourcer._open_download\n"),
    ("from unittest import mock\nfrom engine.render import asset_sourcer as AS\n",
     "def t():\n    with mock.patch.object(AS, '_api_get'):\n        pass\n"),
    ("import unittest.mock\nfrom engine.render import asset_sourcer as AS\n",
     "def t():\n    with unittest.mock.patch('engine.render.asset_sourcer._api_get'):\n        pass\n"),
    ("import unittest.mock as um\nfrom engine.render import asset_sourcer as AS\n",
     "def t():\n    with um.patch(f'{AS.__name__}._api_get'):\n        pass\n"),
])
def test_G1_sees_every_import_form_that_binds_asset_sourcer(head, body):
    _fires(S.g1_problems({"tests/test_x.py": head + body}), "G1")


def test_G1_resolves_a_constant_imported_from_another_test_file():
    """8 of the 9 `_MOD` users get it from tests.test_sourcer_selection: the two-file fixture for that path."""
    consts = "_MOD = 'engine.render.asset_sourcer'\n_LOG = 'engine.render.asset_sourcer'\n"
    user = ("from unittest.mock import patch\n"
            "from tests.test_consts import _MOD\n"
            "def t():\n    with patch(f'{_MOD}._http_stream'):\n        pass\n")
    sources = {"tests/test_consts.py": consts, "tests/test_user.py": user}
    problems = S.g1_problems(sources)
    _fires(problems, "G1")
    assert [p.path for p in problems] == ["tests/test_user.py"]
    assert S.g1_counts(sources) == {("asset_sourcer", "f{_MOD}"): 1}
    # ... and through a chain of re-imports
    chain = {"tests/test_consts.py": consts,
             "tests/test_mid.py": "from tests.test_consts import _MOD\n",
             "tests/test_user.py": user.replace("tests.test_consts", "tests.test_mid")}
    assert S.g1_counts(chain) == {("asset_sourcer", "f{_MOD}"): 1}


def test_G1_an_imported_constant_that_cannot_be_resolved_is_a_failure():
    user = ("from unittest.mock import patch\nfrom tests.test_missing import _MOD\n"
            "def t():\n    with patch(f'{_MOD}._http_stream'):\n        pass\n")
    _fires(S.g1_problems({"tests/test_user.py": user}), "unresolved")


@pytest.mark.parametrize("body", [
    "def t():\n    return AS.settings\n",
    "def t():\n    return AS._atomic_write\n",
    "def t():\n    return AS.WikipediaImageSource\n",
    "def t():\n    return AS.__name__\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(AS, '_atomic_write', 1)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(AS.settings, 'pexels_api_key', 'k')\n",
    "def t():\n    with patch.object(AS, '_cache_asset'):\n        pass\n",
    "def t():\n    with patch(f'{_MOD}.time.sleep'):\n        pass\n",
    "def t():\n    with patch(f'{_MOD}.settings.pexels_api_key', 'k'):\n        pass\n",
    "def t():\n    with patch(f'{AS.__name__}._atomic_write'):\n        pass\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(media_policy, '_MAX_IMAGE_PIXELS', 5)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(safe_fetch, '_monotonic', lambda: 0.0)\n",
    "def t():\n    with patch('engine.render.safe_fetch._http_stream'):\n        pass\n",
    "def t():\n    with patch('httpx.get'), patch('httpx.post'):\n        pass\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(AS, 'settings', 1)\n",
    "@pytest.mark.parametrize('name', ['_MAX_IMAGE_BYTES', '_MAX_VIDEO_BYTES'])\n"
    "def t(monkeypatch, name):\n    monkeypatch.setattr(AS, name, 1)\n",
    "def t(monkeypatch):\n    for n in ('_atomic_write', '_tmp_path_for'):\n        monkeypatch.setattr(AS, n, 1)\n",
    # an HTTP PATCH on a TestClient is not unittest.mock.patch
    "def t(client):\n    return client.patch(f'{_MOD}/x')\n",
    "def t(client):\n    return client.patch('/api/cuts/1', data={})\n",
])
def test_G1_passes_names_that_correctly_stay_or_live_elsewhere(body):
    _no_problems(_g1(body))


def test_G1_ignores_a_patch_that_is_not_unittest_mock_patch():
    src = "import requests\ndef t():\n    requests.patch(f'{x}/y')\n    patch(some_unknown)\n"
    _no_problems(S.g1_problems({"tests/test_x.py": src}))


@pytest.mark.parametrize("body", [
    "def t():\n    with patch('engine.render.safe_fetch.now'):\n        pass\n",
    "def t():\n    with patch(f'{_FETCH}.now'):\n        pass\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(safe_fetch, 'now', lambda: 0.0)\n",
    "def t():\n    with patch.object(safe_fetch, 'now'):\n        pass\n",
    "def t(monkeypatch):\n    sf = safe_fetch\n    monkeypatch.setattr(sf, 'now', lambda: 0.0)\n",
    "def t():\n    with patch(TARGET):\n        pass\n",
])
def test_G1_fires_on_patching_safe_fetch_now_directly_or_through_an_alias(body):
    head = _G1_HEAD + "_FETCH = 'engine.render.safe_fetch'\nTARGET = _FETCH + '.now'\n"
    _fires(_g1(body, head=head), "G1")


@pytest.mark.parametrize("body", [
    "def t(target):\n    with patch(target):\n        pass\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(AS, make_name(), 1)\n",
    "def t(monkeypatch, name):\n    monkeypatch.setattr(AS, name, 1)\n",
    "@pytest.mark.parametrize('name', [make_name()])\ndef t(monkeypatch, name):\n    monkeypatch.setattr(AS, name, 1)\n",
    "def t(monkeypatch):\n    for n in names():\n        monkeypatch.setattr(AS, n, 1)\n",
])
def test_G1_an_unresolved_name_in_a_patch_position_is_a_failure(body):
    _fires(_g1(body), "unresolved")


def test_G1_the_only_allowlisted_unresolved_name_is_the_router_helper():
    assert S.UNRESOLVED_ALLOWLIST.keys() == {("tests/test_cuts_publish_router.py", "patch_target")}
    assert all(S.UNRESOLVED_ALLOWLIST.values()), "every allowlist entry carries a justification"
    src = "from unittest.mock import patch\ndef h(patch_target):\n    with patch(patch_target):\n        pass\n"
    _no_problems(S.g1_problems({"tests/test_cuts_publish_router.py": src}))
    _fires(S.g1_problems({"tests/test_other.py": src}), "unresolved")           # same name, another file
    other = src.replace("patch_target", "target")
    _fires(S.g1_problems({"tests/test_cuts_publish_router.py": other}), "unresolved")


def test_G1_the_router_helper_in_the_real_file_is_the_one_allowlisted_case():
    src = TESTS["tests/test_cuts_publish_router.py"]
    problems = S.g1_problems({"tests/test_cuts_publish_router.py": src})
    assert not problems
    # without the allowlist the same file fails: the allowlist entry is load-bearing, not decorative
    saved = dict(S.UNRESOLVED_ALLOWLIST)
    S.UNRESOLVED_ALLOWLIST.clear()
    try:
        _fires(S.g1_problems({"tests/test_cuts_publish_router.py": src}), "unresolved")
    finally:
        S.UNRESOLVED_ALLOWLIST.update(saved)


def test_G1_parametrize_with_comma_separated_argnames_resolves_the_real_limit_name_case():
    """tests/test_sourcer_api_streaming.py: argnames "cls,ctype,limit_name" -> _MAX_IMAGE_BYTES / _MAX_VIDEO_BYTES."""
    src = TESTS["tests/test_sourcer_api_streaming.py"]
    project = S.Project({"tests/test_sourcer_api_streaming.py": src})
    targets, problems = S.collect_targets(project)
    assert not problems
    named = [t for t in targets if t.form == "object:name"]
    assert sorted(t.attr for t in named) == ["_MAX_IMAGE_BYTES", "_MAX_VIDEO_BYTES"]
    assert {t.owner for t in named} == {"asset_sourcer"} and all(t.attr in O.STAYS for t in named)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# G2 - patches are strict
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _g2(body, head=_G1_HEAD):
    return S.g2_problems({"tests/test_x.py": head + body}, importlib.import_module)


def test_G2_every_patch_of_the_three_modules_in_the_real_tree_is_strict_and_resolves():
    _no_problems(S.g2_problems(TESTS, importlib.import_module))


def test_G2_positive_control_it_checks_a_realistic_number_of_targets():
    targets, problems = S.collect_targets(S.Project(TESTS))
    assert not problems
    owned = [t for t in targets if t.owner]
    assert len(owned) >= 270                      # this file's own probes are in the tree too (G1 skips them, G2 does not)
    by_owner = {o: sum(1 for t in owned if t.owner == o) for o in MODULES}
    assert all(n >= 20 for n in by_owner.values()), by_owner
    conftest = [t for t in targets if t.path == "tests/conftest.py" and t.owner]
    assert sorted(t.target for t in conftest) == [
        "engine.render.media_policy.image_ok", "engine.render.media_policy.sniff_ok",
        "engine.render.safe_fetch._dns_in_time", "engine.render.safe_fetch.api_get", "engine.render.safe_fetch.api_post"]
    assert all(t.raising is None and t.create is None for t in conftest)


@pytest.mark.parametrize("body", [
    "def t(monkeypatch):\n    monkeypatch.setattr('engine.render.media_policy.sniff_ok', lambda k, h: True, raising=False)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(safe_fetch, '_dns_in_time', lambda *a: True, raising=False)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(AS, '_atomic_write', 1, raising=False)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(safe_fetch, '_monotonic', 1, raising=flag)\n",
    "def t():\n    with patch('engine.render.safe_fetch._http_stream', create=True):\n        pass\n",
    "def t():\n    with patch.object(media_policy, 'sniff_ok', create=True):\n        pass\n",
    "def t():\n    with patch.object(AS, '_atomic_write', 1, None, True):\n        pass\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(safe_fetch, '_monotonic', 1, False)\n",
])
def test_G2_fires_on_raising_false_create_true_and_positional_flags(body):
    _fires(_g2(body), "G2")


@pytest.mark.parametrize("body", [
    "def t(monkeypatch):\n    monkeypatch.setattr('engine.render.safe_fetch.no_such_name', 1)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(media_policy, 'no_such_name', 1)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(AS, 'image_ok', 1)\n",                       # a new name, never on asset_sourcer
    "def t(monkeypatch):\n    monkeypatch.setattr(AS, '_http_stream', 1)\n",                      # an old name that is gone
    "def t():\n    with patch('engine.render.safe_fetch.httpx.nothing'):\n        pass\n",
    "def t():\n    with patch('engine.render.asset_sourcer.httpx.get'):\n        pass\n",       # AS has no httpx any more
])
def test_G2_fires_on_a_target_that_does_not_resolve(body):
    _fires(_g2(body), "G2")


@pytest.mark.parametrize("body", [
    "def t(monkeypatch):\n    monkeypatch.setattr('engine.render.safe_fetch._http_stream', 1)\n",
    "def t(monkeypatch):\n    monkeypatch.setattr(media_policy, '_MAX_IMAGE_PIXELS', 1, raising=True)\n",
    "def t():\n    with patch('engine.render.safe_fetch.httpx.get'):\n        pass\n",             # safe_fetch imports httpx
    "def t():\n    with patch.object(safe_fetch, 'now', create=False):\n        pass\n",
    "def t():\n    with patch('engine.render.asset_sourcer.time.sleep'):\n        pass\n",
    "def t(monkeypatch):\n    monkeypatch.setattr('somewhere.else.thing', 1, raising=False)\n",        # not one of the three
])
def test_G2_passes_strict_resolving_targets_and_ignores_other_modules(body):
    _no_problems(_g2(body))


# ══════════════════════════════════════════════════════════════════════════════════════════════
# G3 - no captured seam in production code
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_G3_production_code_captures_no_seam():
    result = S.g3_tree(PROD)
    _no_problems(result.problems)


def test_G3_positive_control_it_sees_the_module_accesses_and_the_five_constant_reads():
    result = S.g3_tree(PROD)
    assert result.accesses >= 45, result.accesses
    assert sorted((p, n) for p, _line, n in result.constant_reads) == [
        (ASSET_PATH, "safe_fetch.MAX_API_JSON_BYTES"),
        (ASSET_PATH, "safe_fetch.MAX_API_JSON_BYTES"),
        (ASSET_PATH, "safe_fetch.MAX_API_JSON_BYTES"),
        (ASSET_PATH, "safe_fetch.MAX_API_JSON_BYTES"),
        ("worker/tasks/render.py", "safe_fetch.RENDER_ASSET_BUDGET_S"),
    ]
    # every file that uses the modules is found (not just the two that matter most)
    assert {p for p in PROD if S.g3_check(PROD[p], p).accesses} == {ASSET_PATH, "worker/tasks/render.py"}


_G3_HEAD = "from engine.render import safe_fetch, media_policy\n"
_G3_PATH = "engine/render/consumer.py"

_G3_VIOLATIONS = {
    "from-import of a callable": "from engine.render.safe_fetch import open_download\n",
    "from-import of a constant": "from engine.render.safe_fetch import MAX_API_JSON_BYTES\n",
    "from-import of a media_policy name": "from engine.render.media_policy import fit_image\n",
    "from-import with an alias": "from engine.render.safe_fetch import now as _now\n",
    "star-import": "from engine.render.safe_fetch import *\n",
    "relative from-import": "from .safe_fetch import open_download\n",
    "relative star-import": "from .media_policy import *\n",
    "module alias": _G3_HEAD + "_sf = safe_fetch\n",
    "attribute alias": _G3_HEAD + "_open = safe_fetch.open_download\n",
    "attribute alias in a function": _G3_HEAD + "def f():\n    g = safe_fetch.open_download\n    return g\n",
    "functools.partial of a callable": _G3_HEAD + "import functools\ndef f():\n    return functools.partial(safe_fetch.open_download, 1)\n",
    "bare partial of a callable": _G3_HEAD + "from functools import partial\nX = partial(media_policy.fit_image)\n",
    "partial of a constant": _G3_HEAD + "from functools import partial\ndef f():\n    return partial(print, safe_fetch.MAX_API_JSON_BYTES)\n",
    "dict container": _G3_HEAD + "TABLE = {'a': safe_fetch.api_get}\n",
    "list container": _G3_HEAD + "def f():\n    return [safe_fetch.api_get, safe_fetch.api_post]\n",
    "constant in a container": _G3_HEAD + "def f():\n    return [safe_fetch.MAX_API_JSON_BYTES]\n",
    "callable default argument": _G3_HEAD + "def f(clock=safe_fetch.now):\n    return clock\n",
    "constant default argument": _G3_HEAD + "def f(limit=safe_fetch.MAX_API_JSON_BYTES):\n    return limit\n",
    "operand in a default argument": _G3_HEAD + "def f(limit=safe_fetch.MAX_API_JSON_BYTES - 1):\n    return limit\n",
    "keyword-only default": _G3_HEAD + "def f(*, limit=safe_fetch.MAX_API_JSON_BYTES):\n    return limit\n",
    "lambda default": _G3_HEAD + "F = lambda limit=safe_fetch.MAX_API_JSON_BYTES: limit\n",
    "decorator": _G3_HEAD + "@safe_fetch.asset_budget\ndef f():\n    pass\n",
    "decorator argument": _G3_HEAD + "import deco\n@deco.use(safe_fetch.now)\ndef f():\n    pass\n",
    "instance attribute": _G3_HEAD + "class A:\n    def __init__(self):\n        self._open = safe_fetch.open_download\n",
    "class attribute": _G3_HEAD + "class A:\n    OPEN = safe_fetch.open_download\n",
    "getattr": _G3_HEAD + "def f():\n    return getattr(safe_fetch, 'open_download')\n",
    "getattr on media_policy": _G3_HEAD + "def f(n):\n    return getattr(media_policy, n)\n",
    "vars": _G3_HEAD + "def f():\n    return vars(safe_fetch)\n",
    "dir": _G3_HEAD + "def f():\n    return dir(media_policy)\n",
    "setattr": _G3_HEAD + "def f():\n    setattr(safe_fetch, 'now', 1)\n",
    "the module passed as an argument": _G3_HEAD + "def f(g):\n    return g(safe_fetch)\n",
    "the module in a container": _G3_HEAD + "MODS = [safe_fetch, media_policy]\n",
    "__dict__": _G3_HEAD + "def f():\n    return safe_fetch.__dict__\n",
    "underscore attribute": _G3_HEAD + "def f():\n    return safe_fetch._http_stream('GET', 'u')\n",
    "underscore attribute of media_policy": _G3_HEAD + "def f():\n    return media_policy._MAX_IMAGE_PIXELS\n",
    "underscore callable": _G3_HEAD + "def f():\n    return media_policy._pixel_cap('JPEG')\n",
    "constant at module scope": _G3_HEAD + "LIMIT = safe_fetch.MAX_API_JSON_BYTES\n",
    "constant in a class body": _G3_HEAD + "class A:\n    LIMIT = safe_fetch.MAX_API_JSON_BYTES\n",
    "operand at module scope": _G3_HEAD + "LIMIT = safe_fetch.RENDER_ASSET_BUDGET_S - 1\n",
    "constant as a right-hand side": _G3_HEAD + "def f():\n    limit = safe_fetch.MAX_API_JSON_BYTES\n    return limit\n",
    "constant returned": _G3_HEAD + "def f():\n    return safe_fetch.RENDER_ASSET_BUDGET_S\n",
    "constant written": _G3_HEAD + "def f():\n    safe_fetch.MAX_API_JSON_BYTES = 1\n",
    "callable written": _G3_HEAD + "def f():\n    safe_fetch.now = lambda: 0.0\n",
    "callable used as a value in a call": _G3_HEAD + "def f(g):\n    return g(safe_fetch.now)\n",
    "callable in isinstance": _G3_HEAD + "def f(e):\n    return isinstance(e, safe_fetch.TooLarge)\n",
    "callable raised without a call": _G3_HEAD + "def f():\n    raise safe_fetch.TooLarge\n",
    "name that is not public": _G3_HEAD + "def f():\n    return safe_fetch.not_a_thing()\n",
    "public name of the other module": _G3_HEAD + "def f():\n    return safe_fetch.fit_image(b'x')\n",
    "dotted alias": "import engine.render.safe_fetch\nX = engine.render.safe_fetch\n",
    "dotted attribute alias": "import engine.render.safe_fetch\nOPEN = engine.render.safe_fetch.open_download\n",
    "import-as alias attribute": "import engine.render.safe_fetch as sf\nOPEN = sf.open_download\n",
    "import-as dotted constant at module scope": "import engine.render.media_policy as mp\nX = mp.refusal_stamp\n",
    "from-package import of a name then alias": "from engine.render import safe_fetch as sf\nOPEN = sf.open_download\n",
    "relative package import, aliased attribute": "from . import media_policy\nFN = media_policy.fit_image\n",
    "relative package import, aliased module": "from . import safe_fetch\nM = safe_fetch\n",
}

_G3_LEGIT = {
    "a call": _G3_HEAD + "def f():\n    return safe_fetch.open_download('u', ok, timeout=1.0)\n",
    "a call in a with": _G3_HEAD + "def f():\n    with safe_fetch.asset_budget(1.0):\n        pass\n",
    "a call at module scope": _G3_HEAD + "X = media_policy.refusal_stamp()\n",
    "an except type": _G3_HEAD + "try:\n    pass\nexcept safe_fetch.TooLarge:\n    pass\n",
    "an except tuple": _G3_HEAD + "try:\n    pass\nexcept (safe_fetch.TooLarge, ValueError):\n    pass\n",
    "an except type and a call": _G3_HEAD + "def f():\n    try:\n        return media_policy.fit_image(b'x')\n    except media_policy.OversizedImage:\n        return None\n",
    "a constant as a call argument": _G3_HEAD + "def f(resp):\n    return safe_fetch.body_too_large(resp, safe_fetch.MAX_API_JSON_BYTES)\n",
    "a constant as a keyword value": _G3_HEAD + "def f(g):\n    return g(limit=safe_fetch.MAX_API_JSON_BYTES)\n",
    "a constant as an operand": _G3_HEAD + "def f(spent):\n    return safe_fetch.asset_budget(safe_fetch.RENDER_ASSET_BUDGET_S - spent)\n",
    "a constant in a comparison": _G3_HEAD + "def f(n):\n    return n > safe_fetch.MAX_API_JSON_BYTES\n",
    "a constant in a method body": _G3_HEAD + "class A:\n    def m(self, resp):\n        return safe_fetch.body_too_large(resp, safe_fetch.MAX_API_JSON_BYTES)\n",
    "a relative package import": "from . import safe_fetch\ndef f():\n    return safe_fetch.now()\n",
    "a dotted call": "import engine.render.safe_fetch\ndef f():\n    return engine.render.safe_fetch.now()\n",
    "an import-as call": "import engine.render.safe_fetch as sf\ndef f():\n    return sf.now()\n",
    "an import-as constant read": "import engine.render.safe_fetch as sf\ndef f(resp):\n    return sf.body_too_large(resp, sf.MAX_API_JSON_BYTES)\n",
    "an unrelated name with the same attribute": "import other\ndef f():\n    return other.open_download\n",
    "an unrelated variable": "def f(safe_fetch):\n    return safe_fetch.anything\n",
}


@pytest.mark.parametrize("name", sorted(_G3_VIOLATIONS))
def test_G3_fires_on_each_forbidden_form(name):
    _fires(S.g3_check(_G3_VIOLATIONS[name], _G3_PATH).problems, "G3")


@pytest.mark.parametrize("name", sorted(_G3_LEGIT))
def test_G3_passes_each_legitimate_form(name):
    result = S.g3_check(_G3_LEGIT[name], _G3_PATH)
    _no_problems(result.problems)


def test_G3_a_shadowed_parameter_named_like_the_module_is_not_the_module():
    """Scope-insensitive on purpose: the cost is a false alarm, never a miss. This pins the one known false alarm
    so a reader is not surprised: a parameter named safe_fetch next to a real import IS treated as the module."""
    src = _G3_HEAD + "def f(safe_fetch):\n    return safe_fetch._anything\n"
    _fires(S.g3_check(src, _G3_PATH).problems, "G3")


def test_G3_the_kind_in_the_oracle_decides_the_allowed_position():
    """A callable labelled `constant` would be readable as an argument: the partial rule and the kind check close it."""
    flipped = dict(S.KIND)
    flipped[("safe_fetch", "open_download")] = "constant"
    src = _G3_HEAD + "import functools\ndef f():\n    return functools.partial(safe_fetch.open_download)\n"
    _fires(S.g3_check(src, _G3_PATH, flipped).problems, "G3")
    assert not S.g3_check(_G3_HEAD + "def f(g):\n    return g(safe_fetch.open_download)\n", _G3_PATH, flipped).problems
    _fires(S.g3_check(_G3_HEAD + "def f(g):\n    return g(safe_fetch.open_download)\n", _G3_PATH).problems, "G3")


def test_G3_the_oracle_kinds_match_the_real_objects():
    """If a callable were labelled `constant` (or the reverse) the G3 position rules would be applied wrongly."""
    for old, (owner, new, kind) in O.MOVED.items():
        obj = getattr(MODULES[owner], new)
        assert callable(obj) == (kind == "callable"), f"{old} -> {owner}.{new} is labelled {kind}"
    constants = {new for (_o, new, kind) in O.MOVED.values() if kind == "constant"}
    assert constants == {"MAX_API_JSON_BYTES", "RENDER_ASSET_BUDGET_S"}
    assert isinstance(safe_fetch.MAX_API_JSON_BYTES, int) and isinstance(safe_fetch.RENDER_ASSET_BUDGET_S, float)
    for name, (owner, kind) in O.NEW.items():
        assert kind == "callable" and callable(getattr(MODULES[owner], name))


def test_G3_the_public_surface_is_exactly_the_22_names_of_the_spec():
    assert S.PUBLIC["safe_fetch"] == sorted([
        "open_download", "api_get", "api_post", "iter_capped", "body_too_large", "TooLarge", "https_host",
        "MAX_API_JSON_BYTES", "now", "asset_budget", "clip_to_asset_budget", "asset_budget_spent",
        "RENDER_ASSET_BUDGET_S"])
    assert S.PUBLIC["media_policy"] == sorted([
        "sniff_ok", "image_ok", "fit_image", "OversizedImage", "require_media", "cached_media_ok",
        "refusal_marker", "refusal_stamp", "refusal_recorded"])
    assert sum(len(v) for v in S.PUBLIC.values()) == 22
    # read from the SOURCE: the conftest switch-offs replace api_get / api_post / sniff_ok / image_ok at runtime
    for owner, path in (("safe_fetch", SAFE_PATH), ("media_policy", MEDIA_PATH)):
        top = S.parse(PROD[path], path).body
        defined = {n.name for n in top if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and not n.name.startswith("_")}
        constants = {t.id for n in top if isinstance(n, ast.Assign) for t in n.targets
                     if isinstance(t, ast.Name) and not t.id.startswith("_")}
        assert defined | constants == set(S.PUBLIC[owner]), (owner, sorted((defined | constants) ^ set(S.PUBLIC[owner])))


# ══════════════════════════════════════════════════════════════════════════════════════════════
# G4 - one clock, one ContextVar
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _others():
    return {p: t for p, t in PROD.items() if p != SAFE_PATH and p.startswith(("engine/", "worker/"))}


def test_G4_one_clock_and_one_contextvar_on_the_real_tree():
    _no_problems(S.g4_problems(PROD[SAFE_PATH], _others()))


def test_G4_positive_control_the_other_modules_are_really_scanned():
    others = _others()
    assert len(others) > 40 and ASSET_PATH in others and "worker/tasks/render.py" in others
    assert S._binding_sites(S.parse(PROD[SAFE_PATH]), S._G4_NAMES) == {"_asset_deadline_at": 1, "_monotonic": 1}


@pytest.mark.parametrize("other", [
    "_asset_deadline_at = None\n",
    "import time\n_monotonic = time.monotonic\n",
    "def _monotonic():\n    return 0.0\n",
    "from contextvars import ContextVar\n_asset_deadline_at = ContextVar('x')\n",
    "from engine.render.safe_fetch import _monotonic as _asset_clock\n",       # the copy render.py used to bind
    "from somewhere import _asset_deadline_at\n",
    "import time as _monotonic\n",
    "def f():\n    global _monotonic\n    _monotonic = 1\n",
    "class _asset_deadline_at:\n    pass\n",
    "def f(_monotonic=None):\n    return _monotonic\n",
])
def test_G4_fires_on_a_second_clock_or_contextvar(other):
    _fires(S.g4_problems(PROD[SAFE_PATH], {"engine/render/asset_sourcer.py": other}), "G4")


def test_G4_fires_on_a_second_definition_inside_safe_fetch_and_on_a_missing_one():
    _fires(S.g4_problems(PROD[SAFE_PATH] + "\n_monotonic = lambda: 0.0\n", {}), "G4")
    _fires(S.g4_problems(PROD[SAFE_PATH] + "\n_asset_deadline_at = None\n", {}), "G4")
    _fires(S.g4_problems(PROD[SAFE_PATH].replace("_monotonic = time.monotonic", "pass"), {}), "G4")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# G5 - direction, completeness, no bridge
# ══════════════════════════════════════════════════════════════════════════════════════════════
MEDIA_FORBIDDEN = ("engine.render.safe_fetch", "engine.render.asset_sourcer")
SAFE_FORBIDDEN = ("engine.render.media_policy", "engine.render.asset_sourcer")


def test_G5_the_dependency_direction_on_the_real_tree():
    _no_problems(S.g5_import_problems(PROD[MEDIA_PATH], MEDIA_PATH, MEDIA_FORBIDDEN))
    _no_problems(S.g5_import_problems(PROD[SAFE_PATH], SAFE_PATH, SAFE_FORBIDDEN))


def test_G5_positive_control_both_modules_have_imports_and_they_are_seen():
    for path in (MEDIA_PATH, SAFE_PATH):
        assert "import " in PROD[path]
    # the same files do trip the rule when judged by the OTHER module's list
    _fires(S.g5_import_problems(PROD[ASSET_PATH], ASSET_PATH, MEDIA_FORBIDDEN), "G5")


@pytest.mark.parametrize("snippet", [
    "from engine.render import safe_fetch\n",
    "from engine.render.safe_fetch import https_host\n",
    "import engine.render.safe_fetch\n",
    "import engine.render.safe_fetch as sf\n",
    "from . import safe_fetch\n",
    "from .safe_fetch import https_host\n",
    "def f():\n    from engine.render import safe_fetch\n    return safe_fetch\n",
    "from engine.render import asset_sourcer\n",
    "from engine.render.asset_sourcer import settings\n",
    "from . import asset_sourcer\n",
])
def test_G5_media_policy_importing_safe_fetch_or_asset_sourcer_fires(snippet):
    _fires(S.g5_import_problems(snippet, MEDIA_PATH, MEDIA_FORBIDDEN), "G5")


@pytest.mark.parametrize("snippet", [
    "from engine.render import media_policy\n",
    "from engine.render.media_policy import fit_image\n",
    "import engine.render.media_policy\n",
    "from . import media_policy\n",
    "from .media_policy import fit_image\n",
    "from engine.render import asset_sourcer\n",
    "from . import asset_sourcer\n",
])
def test_G5_safe_fetch_importing_media_policy_or_asset_sourcer_fires(snippet):
    _fires(S.g5_import_problems(snippet, SAFE_PATH, SAFE_FORBIDDEN), "G5")


def test_G5_legitimate_imports_pass():
    src = "import logging\nimport httpx\nfrom engine.render import something_else\nfrom celery.exceptions import SoftTimeLimitExceeded\n"
    _no_problems(S.g5_import_problems(src, SAFE_PATH, SAFE_FORBIDDEN))


def test_G5_no_bridge_is_left_in_asset_sourcer():
    _no_problems(S.g5_bridge_problems(PROD[ASSET_PATH]))
    assert type(AS) is types.ModuleType, "asset_sourcer's class was swapped: the forwarding bridge is still there"
    for attr in ("__getattr__", "__setattr__", "__delattr__"):
        assert attr not in vars(AS)
    assert not hasattr(type(AS), "__getattr__")
    assert type(AS).__setattr__ is types.ModuleType.__setattr__ and type(AS).__delattr__ is types.ModuleType.__delattr__


@pytest.mark.parametrize("snippet", [
    "def __getattr__(name):\n    raise AttributeError(name)\n",
    "def __setattr__(self, name, value):\n    pass\n",
    "def __delattr__(self, name):\n    pass\n",
    "import sys\nsys.modules[__name__].__class__ = object\n",
    "__class__ = object\n",
    "import sys\nimport types\nclass _Bridge(types.ModuleType):\n    pass\n",
    "from types import ModuleType\nclass _Bridge(ModuleType):\n    pass\n",
    "import sys\nsys.modules[__name__] = object()\n",
    "import sys\nsetattr(sys.modules[__name__], '__class__', object)\n",
    "class A:\n    pass\nA().__class__ = object\n",
])
def test_G5_a_bridge_like_construct_in_asset_sourcer_fires(snippet):
    _fires(S.g5_bridge_problems(snippet), "G5")


def test_G5_a_method_named_getattr_inside_a_class_is_not_a_module_level_bridge():
    _no_problems(S.g5_bridge_problems("class A:\n    def __getattr__(self, name):\n        raise AttributeError(name)\n"))


def test_G5_every_moved_name_exists_on_its_owner_and_is_gone_from_asset_sourcer():
    assert len(O.MOVED) == 58
    for old, (owner, new, _kind) in O.MOVED.items():
        assert hasattr(MODULES[owner], new), f"{owner} lacks {new}"
        assert old not in vars(AS), f"asset_sourcer still has {old}"
    for name, (owner, _kind) in O.NEW.items():
        assert hasattr(MODULES[owner], name)
    for extra in O.BRIDGE_EXTRAS:
        assert extra not in vars(AS), f"asset_sourcer still has {extra}"
    for stays in O.STAYS:
        assert hasattr(AS, stays), f"asset_sourcer lost {stays}"


def test_G5_the_moved_set_is_disjoint_from_the_stay_set():
    assert not set(O.MOVED) & set(O.STAYS)


# ══════════════════════════════════════════════════════════════════════════════════════════════
# G6 - transport and allowlist stay in the adapters' contract
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_G6a_asset_sourcer_imports_no_transport():
    _no_problems(S.g6a_problems(PROD[ASSET_PATH]))


def test_G6a_positive_control_safe_fetch_does_import_the_transport():
    _fires(S.g6a_problems(PROD[SAFE_PATH], SAFE_PATH), "G6")


@pytest.mark.parametrize("snippet", [
    "import httpx\n", "import socket\n", "import ssl\n", "import http.client\n", "import requests\n",
    "import urllib.request\n", "from http import client\n", "from http.client import HTTPSConnection\n",
    "from urllib import request\n", "from urllib.request import urlopen\n", "import httpx as h\n",
    "def f():\n    import httpx\n    return httpx\n", "from httpx import Client\n",
])
def test_G6a_fires_on_a_transport_import(snippet):
    _fires(S.g6a_problems(snippet), "G6")


def test_G6a_urllib_parse_and_other_stdlib_are_fine():
    _no_problems(S.g6a_problems("import urllib.parse\nimport os\nfrom urllib.parse import quote\n"))


def test_G6b_every_open_download_call_passes_one_of_the_two_predicates():
    calls = 0
    for path, text in PROD.items():
        _no_problems(S.g6b_problems(text, path))
        calls += sum(1 for _ in S._safe_fetch_calls(S.parse(text, path), S.import_bindings(S.parse(text, path), path),
                                                    ("open_download",)))
    assert calls == 2


_G6_HEAD = "from engine.render import safe_fetch\n"
_G6_FN = ("def _pexels_url_ok(url):\n    return safe_fetch.https_host(url) == 'x'\n"
          "def _wikimedia_url_ok(url):\n    return safe_fetch.https_host(url) == 'y'\n")


@pytest.mark.parametrize("call", [
    "safe_fetch.open_download(u, lambda x: True, timeout=1.0)",
    "safe_fetch.open_download(u, url_ok=lambda x: True, timeout=1.0)",
    "safe_fetch.open_download(u, _local_ok, timeout=1.0)",
    "safe_fetch.open_download(u, safe_fetch.https_host, timeout=1.0)",
    "safe_fetch.open_download(u, ok, timeout=1.0)",
    "safe_fetch.open_download(u, None, timeout=1.0)",
    "safe_fetch.open_download(u, timeout=1.0)",
    "safe_fetch.open_download(u, _pexels_url_ok or _wikimedia_url_ok, timeout=1.0)",
])
def test_G6b_fires_on_any_other_url_ok(call):
    src = _G6_HEAD + _G6_FN + "def _local_ok(u):\n    return True\ndef f(u):\n    return " + call + "\n"
    _fires(S.g6b_problems(src, ASSET_PATH), "G6")


@pytest.mark.parametrize("call", [
    "safe_fetch.open_download(u, _pexels_url_ok, timeout=1.0)",
    "safe_fetch.open_download(u, _wikimedia_url_ok, timeout=1.0, deadline_at=1.0)",
    "safe_fetch.open_download(u, url_ok=_pexels_url_ok, timeout=1.0)",
])
def test_G6b_passes_the_two_predicates(call):
    _no_problems(S.g6b_problems(_G6_HEAD + _G6_FN + "def f(u):\n    return " + call + "\n", ASSET_PATH))


def test_G6b_the_predicates_must_be_asset_sourcer_functions_calling_https_host():
    call = "def f(u):\n    return safe_fetch.open_download(u, _pexels_url_ok, timeout=1.0)\n"
    no_host = ("def _pexels_url_ok(url):\n    return True\n"
               "def _wikimedia_url_ok(url):\n    return safe_fetch.https_host(url) == 'y'\n")
    _fires(S.g6b_problems(_G6_HEAD + no_host + call, ASSET_PATH), "G6")
    missing = "def _wikimedia_url_ok(url):\n    return safe_fetch.https_host(url) == 'y'\n"
    _fires(S.g6b_problems(_G6_HEAD + missing + call, ASSET_PATH), "G6")
    as_lambda = ("_pexels_url_ok = lambda u: True\n"
                 "def _wikimedia_url_ok(url):\n    return safe_fetch.https_host(url) == 'y'\n")
    _fires(S.g6b_problems(_G6_HEAD + as_lambda + call, ASSET_PATH), "G6")


def test_G6b_a_call_outside_asset_sourcer_is_flagged_even_with_a_valid_predicate_name():
    src = _G6_HEAD + _G6_FN + "def f(u):\n    return safe_fetch.open_download(u, _pexels_url_ok, timeout=1.0)\n"
    _fires(S.g6b_problems(src, "engine/render/other.py"), "G6")


def test_G6c_every_api_call_url_is_constant_rooted():
    calls = 0
    for path, text in PROD.items():
        _no_problems(S.g6c_problems(text, path))
        tree = S.parse(text, path)
        calls += sum(1 for _ in S._safe_fetch_calls(tree, S.import_bindings(tree, path), ("api_get", "api_post")))
    assert calls == 6


_G6C_CLASS = ("class Src:\n    _API = 'https://api.example/v1'\n    _N = 5\n"
              "    def m(self, model, url_from_response):\n        return {call}\n")


@pytest.mark.parametrize("call", [
    "safe_fetch.api_get(url_from_response, timeout=1.0)",
    "safe_fetch.api_get(model, timeout=1.0)",
    "safe_fetch.api_get(f'{model}/x', timeout=1.0)",
    "safe_fetch.api_get(f'{url_from_response}', timeout=1.0)",
    "safe_fetch.api_post(f'{self._missing}/x', timeout=1.0)",
    "safe_fetch.api_post(self._N, timeout=1.0)",                          # not a string constant
    "safe_fetch.api_post(self.other.url, timeout=1.0)",
    "safe_fetch.api_post(model + '/x', timeout=1.0)",
    "safe_fetch.api_post(None, timeout=1.0)",
    "safe_fetch.api_post(timeout=1.0)",
    "safe_fetch.api_get(self._API + url_from_response, timeout=1.0)",
    "safe_fetch.api_get(str(self._API), timeout=1.0)",
])
def test_G6c_fires_on_a_variable_or_response_derived_url(call):
    _fires(S.g6c_problems(_G6_HEAD + _G6C_CLASS.format(call=call), ASSET_PATH), "G6")


@pytest.mark.parametrize("call", [
    "safe_fetch.api_get('https://api.example/x', timeout=1.0)",
    "safe_fetch.api_get(self._API, timeout=1.0)",
    "safe_fetch.api_get(Src._API, timeout=1.0)",
    "safe_fetch.api_post(f'{self._API}/{model}', timeout=1.0)",
    "safe_fetch.api_post(f'https://api.example/{model}', timeout=1.0)",
    "safe_fetch.api_post(url=self._API, timeout=1.0)",
    "safe_fetch.api_get(_TOP, timeout=1.0)",
    "safe_fetch.api_get(f'{_TOP}/x', timeout=1.0)",
])
def test_G6c_passes_a_literal_or_a_class_or_module_constant(call):
    src = _G6_HEAD + "_TOP = 'https://top.example'\n" + _G6C_CLASS.format(call=call)
    _no_problems(S.g6c_problems(src, ASSET_PATH))


def test_G6c_self_attributes_resolve_against_the_enclosing_class_only():
    src = (_G6_HEAD + "class A:\n    _API = 'https://a.example'\n"
           "class B:\n    def m(self):\n        return safe_fetch.api_get(self._API, timeout=1.0)\n")
    _fires(S.g6c_problems(src, ASSET_PATH), "G6")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# the checkers' own plumbing (so a broken resolver cannot make a guard vacuous)
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_the_resolver_evaluates_strings_through_every_supported_construct():
    src = ("A = 'engine.render'\nB = A + '.safe_fetch'\nC = f'{B}.now'\n"
           "import engine.render.asset_sourcer as M\nD = f'{M.__name__}.x'\n")
    ctx = S.FileCtx("tests/test_x.py", src)
    project = S.Project({"tests/test_x.py": src})

    def ev(name):
        return S.eval_str(ast.Name(id=name, ctx=ast.Load()), ctx, project)

    assert ev("B") == ["engine.render.safe_fetch"]
    assert ev("C") == ["engine.render.safe_fetch.now"]
    assert ev("D") == ["engine.render.asset_sourcer.x"]
    assert ev("nope") is None


def test_a_relative_import_resolves_against_the_files_package():
    tree = S.parse("from . import safe_fetch\nfrom .media_policy import fit_image\nfrom .. import x\n", "engine/render/c.py")
    b = S.import_bindings(tree, "engine/render/c.py")
    assert b["safe_fetch"] == {"engine.render.safe_fetch"}
    assert b["fit_image"] == {"engine.render.media_policy.fit_image"}
    assert b["x"] == {"engine.x"}


# ══════════════════════════════════════════════════════════════════════════════════════════════
# behaviour 1 - the clock
# ══════════════════════════════════════════════════════════════════════════════════════════════
def test_now_follows_a_patched_monotonic(monkeypatch):
    """A `time.time()` or an import-time copy of the clock would not see this."""
    monkeypatch.setattr(safe_fetch, "_monotonic", lambda: 123.5)
    assert safe_fetch.now() == 123.5
    monkeypatch.setattr(safe_fetch, "_monotonic", lambda: 7.0)
    assert safe_fetch.now() == 7.0


def test_the_unpatched_clock_is_the_real_monotonic_clock():
    before = safe_fetch.now()
    assert isinstance(before, float) and safe_fetch.now() >= before
    assert safe_fetch._monotonic is not None and safe_fetch._monotonic.__name__ == "monotonic"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# behaviour 2 - seam probes: a sentinel on the OWNING module is seen by each consumer
# ══════════════════════════════════════════════════════════════════════════════════════════════
URL = "https://api.example/x"
_HEAD16 = b"0123456789abcdef"


def test_probe_fit_image_consumes_media_policy_image_ok(monkeypatch):
    seen = []
    monkeypatch.setattr(media_policy, "image_ok", lambda source: seen.append(source) or True)
    assert media_policy.fit_image(b"payload") == b"payload"
    assert seen == [b"payload"]


def test_probe_cached_media_ok_consumes_both_sniff_ok_and_image_ok(tmp_path, monkeypatch):
    f = tmp_path / "a.png"
    f.write_bytes(_HEAD16 + b"-and-more")
    heads, images = [], []
    monkeypatch.setattr(media_policy, "sniff_ok", lambda kind, head: heads.append((kind, head)) or True)
    monkeypatch.setattr(media_policy, "image_ok", lambda source: images.append(source) or False)
    assert media_policy.cached_media_ok(f, "image") is False
    assert heads == [("image", _HEAD16)] and images == [f]
    assert not f.exists(), "the cached file the sentinel refused must be deleted"


def test_probe_require_media_consumes_media_policy_sniff_ok(monkeypatch):
    seen = []
    monkeypatch.setattr(media_policy, "sniff_ok", lambda kind, head: seen.append((kind, head)) or False)
    with pytest.raises(ValueError):
        media_policy.require_media("video", b"hdr")
    assert seen == [("video", b"hdr")]


def _hf_response(content_type, content=_HEAD16 + b"-body"):
    resp = MagicMock()
    resp.headers = {"content-type": content_type}
    resp.content = content
    resp.raise_for_status.return_value = None
    return resp


def test_probe_the_hf_image_adapter_consumes_sniff_ok_then_image_ok(tmp_path, monkeypatch):
    resp = _hf_response("image/png")
    monkeypatch.setattr(safe_fetch, "api_post", lambda url, **kw: resp)
    sniffed, decoded = [], []
    verdict = {"sniff": False, "image": True}
    monkeypatch.setattr(media_policy, "sniff_ok", lambda kind, head: sniffed.append((kind, head)) or verdict["sniff"])
    monkeypatch.setattr(media_policy, "image_ok", lambda source: decoded.append(source) or verdict["image"])
    src = HuggingFaceImageSource("k", "org/m", tmp_path)
    assert src.generate("a prompt") is None
    assert sniffed == [("image", resp.content[:16])] and decoded == []
    verdict["sniff"] = True
    verdict["image"] = False
    assert src.generate("a prompt") is None
    assert decoded == [resp.content] and list(tmp_path.iterdir()) == []
    verdict["image"] = True
    assert src.generate("a prompt").local_path.read_bytes() == resp.content


def test_probe_the_hf_video_adapter_consumes_sniff_ok(tmp_path, monkeypatch):
    resp = _hf_response("video/mp4")
    monkeypatch.setattr(safe_fetch, "api_post", lambda url, **kw: resp)
    sniffed = []
    verdict = {"sniff": False}
    monkeypatch.setattr(media_policy, "sniff_ok", lambda kind, head: sniffed.append((kind, head)) or verdict["sniff"])
    src = HuggingFaceVideoSource("k", "org/m", tmp_path)
    assert src.generate("a prompt") is None
    assert sniffed == [("video", resp.content[:16])] and list(tmp_path.iterdir()) == []
    verdict["sniff"] = True
    assert src.generate("a prompt").local_path.read_bytes() == resp.content


def test_probe_the_adapters_consume_safe_fetch_api_get_and_api_post(tmp_path, monkeypatch):
    calls = []

    def boom(url, **kw):
        calls.append(url)
        raise RuntimeError("probe")

    monkeypatch.setattr(safe_fetch, "api_get", boom)
    monkeypatch.setattr(safe_fetch, "api_post", boom)
    assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is None
    assert calls[-1] == "https://api.pexels.com/videos/search"
    assert AS.WikipediaImageSource(tmp_path).search("Lionel Messi") is None
    assert calls[-1] == AS.WikipediaImageSource._SEARCH
    assert HuggingFaceImageSource("k", "org/m", tmp_path).generate("p") is None
    assert calls[-1] == "https://api-inference.huggingface.co/models/org/m"
    assert HuggingFaceVideoSource("k", "org/v", tmp_path).generate("p") is None
    assert calls[-1] == "https://api-inference.huggingface.co/models/org/v"
    assert len(calls) == 4


def test_probe_the_download_adapters_read_through_safe_fetch_iter_capped(tmp_path, monkeypatch):
    seen = []

    def sentinel(r, limit, deadline_at=None, *, raw=False):
        seen.append((limit, deadline_at))
        yield b"SENTINEL-BYTES"

    monkeypatch.setattr(safe_fetch, "iter_capped", sentinel)
    pexels = _pexels(tmp_path / "p", [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], _Stream({PEXELS_OK: _Resp()}))
    assert pexels.local_path.read_bytes() == b"SENTINEL-BYTES"
    assert seen[-1][0] == AS._MAX_VIDEO_BYTES and seen[-1][1] is not None
    wiki = _wiki(tmp_path / "w", _summary(), _Stream({WIKI_OK: _Resp()}))
    assert wiki.local_path.read_bytes() == b"SENTINEL-BYTES"
    assert seen[-1][0] == AS._MAX_IMAGE_BYTES and seen[-1][1] is not None
    assert len(seen) == 2


def test_probe_open_download_consumes_the_http_stream_the_dns_gate_and_the_clock(monkeypatch):
    stream = _Stream({PEXELS_OK: _Resp()})
    monkeypatch.setattr(safe_fetch, "_http_stream", stream)
    dns = []
    monkeypatch.setattr(safe_fetch, "_dns_in_time", lambda host, port, timeout: dns.append((host, port, timeout)) or True)
    monkeypatch.setattr(safe_fetch, "_monotonic", lambda: 0.0)
    with safe_fetch.open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=5.0):
        pass
    assert stream.urls == [PEXELS_OK]
    assert stream.requests[0][1]["timeout"] == pytest.approx(6.0)        # 5 - 0 + the 1 s grace: the patched clock
    assert dns == [("videos.pexels.com", 443, pytest.approx(6.0))]       # the gate runs, with a deadline, via the owner
    dns.clear()
    with safe_fetch.open_download(PEXELS_OK, AS._pexels_url_ok, timeout=5.0):
        pass
    assert dns == [], "the DNS gate only runs when there is a deadline"


def test_probe_open_download_refuses_when_the_patched_clock_is_past_the_deadline(monkeypatch):
    monkeypatch.setattr(safe_fetch, "_http_stream", _Stream({PEXELS_OK: _Resp()}))
    monkeypatch.setattr(safe_fetch, "_monotonic", lambda: 100.0)
    with pytest.raises(ValueError, match="deadline exceeded before the request"):
        with safe_fetch.open_download(PEXELS_OK, AS._pexels_url_ok, timeout=5.0, deadline_at=50.0):
            pass


def test_probe_iter_capped_consumes_the_patched_clock(monkeypatch):
    monkeypatch.setattr(safe_fetch, "_monotonic", lambda: 10.0)
    with pytest.raises(ValueError, match="too slow"):
        list(safe_fetch.iter_capped(_Resp(chunks=(b"x",)), 100, 5.0))
    assert list(safe_fetch.iter_capped(_Resp(chunks=(b"x",)), 100, 50.0)) == [b"x"]


def test_probe_asset_budget_consumes_the_patched_clock(monkeypatch):
    clock = {"t": 10.0}
    monkeypatch.setattr(safe_fetch, "_monotonic", lambda: clock["t"])
    with safe_fetch.asset_budget(5.0):
        assert safe_fetch.clip_to_asset_budget(100.0) == 15.0
        assert safe_fetch.asset_budget_spent() is False
        clock["t"] = 15.0
        assert safe_fetch.asset_budget_spent() is True


def test_probe_the_search_budgets_consume_the_patched_clock(tmp_path, monkeypatch):
    """Reading 1 fixes the search's deadline, reading 2 is "now": 0 + 25 < 1000, so no download starts. An adapter
    on its own `time.monotonic()` would see a fresh deadline and download."""
    monkeypatch.setattr(AS, "_PEXELS_SEARCH_BUDGET_S", 25.0)
    monkeypatch.setattr(AS, "_WIKI_SEARCH_BUDGET_S", 25.0)
    monkeypatch.setattr(safe_fetch, "_monotonic", _fake_clock(0.0, 1000.0))
    stream = _Stream({PEXELS_OK: _Resp()})
    assert _pexels(tmp_path / "p", [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is None
    assert stream.requests == []
    monkeypatch.setattr(safe_fetch, "_monotonic", _fake_clock(0.0, 1000.0))
    stream = _Stream({WIKI_OK: _Resp()})
    assert _wiki(tmp_path / "w", _summary(), stream) is None
    assert stream.requests == []


def test_probe_the_download_adapters_hand_the_clock_derived_deadline_to_open_download(tmp_path, monkeypatch):
    monkeypatch.setattr(safe_fetch, "_monotonic", lambda: 5000.0)
    deadlines = []
    real = safe_fetch.open_download

    def spy(url, url_ok, **kw):
        deadlines.append(kw.get("deadline_at"))
        return real(url, url_ok, **kw)

    monkeypatch.setattr(safe_fetch, "open_download", spy)
    assert _pexels(tmp_path / "p", [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], _Stream({PEXELS_OK: _Resp()}))
    assert _wiki(tmp_path / "w", _summary(), _Stream({WIKI_OK: _Resp()}))
    assert deadlines == [5000.0 + AS._VIDEO_DEADLINE_S, 5000.0 + AS._IMAGE_DEADLINE_S]


@pytest.mark.real_api
def test_probe_api_call_consumes_http_stream_and_iter_capped(monkeypatch):
    stream = _Stream({URL: _Resp(chunks=(b"ignored",))})
    monkeypatch.setattr(safe_fetch, "_http_stream", stream)
    seen = []

    def sentinel(r, limit, deadline_at=None, *, raw=False):
        seen.append((limit, raw))
        yield b'{"sentinel": 1}'

    monkeypatch.setattr(safe_fetch, "iter_capped", sentinel)
    resp = safe_fetch.api_get(URL, timeout=1.0)
    assert resp.json() == {"sentinel": 1}
    assert stream.urls == [URL] and seen == [(safe_fetch.MAX_API_JSON_BYTES, False)]
    safe_fetch.api_post(URL, timeout=1.0, limit=77)
    assert seen[-1] == (77, False) and stream.urls == [URL, URL]


# ══════════════════════════════════════════════════════════════════════════════════════════════
# behaviour 3 - fixture effectiveness at the consumer
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _no_proxy(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)


def _resolver(monkeypatch):
    calls = []

    def getaddrinfo(host, port, *a, **kw):
        calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return calls


def _open_with_deadline(monkeypatch):
    monkeypatch.setattr(safe_fetch, "_http_stream", _Stream({PEXELS_OK: _Resp()}))
    with safe_fetch.open_download(PEXELS_OK, AS._pexels_url_ok, timeout=5.0, deadline_at=safe_fetch.now() + 100.0):
        pass


def test_unmarked_media_policy_accepts_placeholder_bytes(tmp_path):
    media_policy.require_media("image", b"junk")                 # does not raise
    media_policy.require_media("video", b"junk")
    assert media_policy.sniff_ok("image", b"junk") is True
    assert media_policy.fit_image(b"junk") == b"junk"
    assert media_policy.image_ok(b"junk") is True
    f = tmp_path / "a.png"
    f.write_bytes(b"junk")
    assert media_policy.cached_media_ok(f, "image") is True and f.exists()


def test_unmarked_open_download_performs_no_real_dns_lookup(monkeypatch):
    _no_proxy(monkeypatch)
    calls = _resolver(monkeypatch)
    _open_with_deadline(monkeypatch)
    assert calls == []


def test_unmarked_api_calls_reach_httpx_get_and_post(monkeypatch):
    sentinel = object()
    with patch("httpx.get", return_value=sentinel) as get, patch("httpx.post", return_value=sentinel) as post:
        assert safe_fetch.api_get(URL, timeout=1.0, limit=5, headers={"a": "b"}) is sentinel
        assert safe_fetch.api_post(URL, timeout=2.0, json={"x": 1}) is sentinel
    get.assert_called_once_with(URL, timeout=1.0, headers={"a": "b"})            # `limit` is dropped
    post.assert_called_once_with(URL, timeout=2.0, json={"x": 1})


@pytest.mark.real_media_sniffing
def test_real_media_sniffing_marker_gives_the_real_sniff():
    with pytest.raises(ValueError):
        media_policy.require_media("image", b"junk")
    assert media_policy.sniff_ok("image", b"junk") is False
    assert media_policy.sniff_ok("image", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) is True
    assert media_policy.sniff_ok("video", b"\x00\x00\x00\x18ftypmp42") is True


@pytest.mark.real_image_check
def test_real_image_check_marker_gives_the_real_decoder(tmp_path):
    assert media_policy.image_ok(b"junk") is False
    with pytest.raises(ValueError):
        media_policy.fit_image(b"junk")
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    assert media_policy.image_ok(f) is False


@pytest.mark.real_dns
def test_real_dns_marker_does_look_up_the_host(monkeypatch):
    _no_proxy(monkeypatch)
    calls = _resolver(monkeypatch)
    _open_with_deadline(monkeypatch)
    assert calls == [("videos.pexels.com", 443)]


@pytest.mark.real_api
def test_real_api_marker_streams_through_http_stream_not_httpx(monkeypatch):
    stream = _Stream({URL: _Resp(chunks=(b'{"a": 1}',))})
    monkeypatch.setattr(safe_fetch, "_http_stream", stream)
    with patch("httpx.get", side_effect=AssertionError("httpx.get must not be reached")), \
            patch("httpx.post", side_effect=AssertionError("httpx.post must not be reached")):
        assert safe_fetch.api_get(URL, timeout=1.0).json() == {"a": 1}
        assert safe_fetch.api_post(URL, timeout=1.0).json() == {"a": 1}
    assert stream.urls == [URL, URL]
    assert stream.requests[0][1]["headers"]["Accept-Encoding"] == "identity"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# behaviour 4 - logger names (D4)
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _names(caplog, text):
    return {r.name for r in caplog.records if text in r.getMessage()}


def test_open_download_logs_under_the_safe_fetch_logger(caplog):
    caplog.set_level(logging.WARNING)
    with pytest.raises(ValueError):
        with safe_fetch.open_download("http://evil.example/x", lambda u: False, timeout=1.0):
            pass
    assert _names(caplog, "Refusing download from a disallowed URL") == {"engine.render.safe_fetch"}


@pytest.mark.real_media_sniffing
def test_require_media_logs_under_the_media_policy_logger(caplog):
    caplog.set_level(logging.WARNING)
    with pytest.raises(ValueError):
        media_policy.require_media("image", b"junk")
    assert _names(caplog, "not a valid image") == {"engine.render.media_policy"}


@pytest.mark.real_image_check
def test_fit_image_logs_under_the_media_policy_logger(caplog):
    caplog.set_level(logging.WARNING)
    with pytest.raises(ValueError):
        media_policy.fit_image(b"junk")
    assert _names(caplog, "not a readable image") == {"engine.render.media_policy"}


def test_each_new_module_uses_its_own_dunder_name_logger():
    assert safe_fetch._log.name == "engine.render.safe_fetch" == safe_fetch.__name__
    assert media_policy._log.name == "engine.render.media_policy" == media_policy.__name__
    assert AS._log.name == "engine.render.asset_sourcer" == AS.__name__
