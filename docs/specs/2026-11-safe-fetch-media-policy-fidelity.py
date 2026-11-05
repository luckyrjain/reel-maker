#!/usr/bin/env python3
"""Fidelity proof for the safe_fetch / media_policy split (spec 2026-11-safe-fetch-media-policy-module-design.md, section 6).

Run from the repo root (it inserts the repo root into sys.path itself):

    python docs/specs/2026-11-safe-fetch-media-policy-fidelity.py [--baseline REV] [--stage 0|1|2|3] [--tree DIR]

    --baseline REV  the revision the move started from (default d096524 = main before the split). Baseline files are
                    read with `git show REV:path`, never from the working tree.
    --stage N       0 = oracle self-checks only (nothing but the baseline and tests/_seam_oracle.py is needed);
                    1 = after commit 1 (modules exist, bridge allowed and required, loggers use the literal old name);
                    2 = after commit 2 (bridge gone, loggers use __name__, worker/tasks/render.py is compared too);
                    3 = after commit 3 (same as 2, plus the G1 resolver mode).
    --no-collect    (stage >= 2) skip the pytest --collect-only comparison of node ids.
    --cross-check-baseline
                    (stage 3) also run the resolver on the baseline test tree and compare it with the counting script.
    --tree DIR      where the files under test live (default: the repo root). Used to test the script itself on a
                    synthetic tree.

One line is printed per check: "OK name" or "FAIL name: detail" (a FAIL may be followed by indented detail lines).
The exit status is non-zero when any check failed. Standard library only; CI lints this file with ruff F,E9.

Checks
  ORACLE   tests/_seam_oracle.py against the baseline AST: MOVED == baseline top-level definitions that are not in
           STAYS, MOVED and STAYS are disjoint, 58 names (29 + 29), kinds derived (not trusted), 22 public names.
  A        every moved definition equals its baseline node after the rename table (ast.dump; docstrings compared,
           except the named EDIT_ALLOWLIST), in baseline order; each new module's top level is exactly
           docstring, imports, _log, the moved nodes (and `now` after `_monotonic` in safe_fetch).
  B        asset_sourcer.py (and from stage 2 worker/tasks/render.py): the multiset of top-level nodes of the
           inverse-transformed baseline equals the new file's, minus the planned import changes (and, at stage 1,
           the bridge).
  DEP      the new modules reference nothing defined in asset_sourcer or in each other, every public name has an
           outside caller in production code and no private name has one.
  C        tests (stage >= 2): every baseline test file, rewritten by a deterministic AST rewrite derived from the
           oracle, equals the retargeted file (ast.dump) except for the exhaustive residual list in the spec; plus
           per-file counts of assert / raises / approx, the test-function name set, the pytest node-id set
           (collected from an export of the baseline, `--no-collect` skips only this part) and the set of test files.
  G1       stage 3, resolver mode: tests/_seam_ast.py (the resolver the guards in tests/test_module_seams.py use, loaded
           by path; it is standard library only) resolves every patch target of the real test tree; the script prints
           the per-form, per-owning-module figures, requires zero stale access / unresolved targets and requires the
           figures to equal G1_EXPECTED_COUNTS recorded in tests/test_module_seams.py. With --cross-check-baseline
           the same resolver is run on the BASELINE test tree and its per-form totals must equal the independent
           regex totals of docs/specs/2026-11-safe-fetch-media-policy-counts.py (152 / 17 / 6 / 9 ...).

To allow another deliberate docstring edit, add the BASELINE name of the node (or "<module:NAME>" for a new module
docstring) to EDIT_ALLOWLIST below, with the reason; it is compared with its docstring removed on both sides.
"""
from __future__ import annotations

import argparse
import ast
import collections
import copy
import difflib
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ASSET_PATH = "engine/render/asset_sourcer.py"
RENDER_PATH = "worker/tasks/render.py"
OWNERS = ("safe_fetch", "media_policy")
OWNER_PATH = {o: f"engine/render/{o}.py" for o in OWNERS}
PRODUCTION_DIRS = ("api", "engine", "worker")

# Deliberate docstring edits (spec section 5 delta 2). Key: the BASELINE top-level name, or "<module:NAME>" for the
# docstring of a new module. The node is compared with its (top-level) docstring removed on both sides.
EDIT_ALLOWLIST = {
    "_any_https_host": "docstring said the hosts are constants in this module; false in safe_fetch (the host set is the caller's)",
    "_api_get": "docstring gains a sentence: the default url_ok is not an allowlist",
    "_api_post": "docstring gains a sentence: the default url_ok is not an allowlist",
    "<module:safe_fetch>": "new module docstring",
    "<module:media_policy>": "new module docstring",
}

# Baseline imports with no staying-code user (spec 4.1). Key: (kind, module, name, asname).
PLANNED_REMOVALS = frozenset({
    ("import", "ipaddress", None, None),
    ("import", "socket", None, None),
    ("import", "threading", None, None),
    ("import", "zlib", None, None),
    ("import", "warnings", None, None),
    ("import", "io", None, None),
    ("import", "httpx", None, None),
    ("import", "urllib.request", None, None),
    ("from", "contextlib", "contextmanager", None),
    ("from", "contextvars", "ContextVar", None),
})
PLANNED_ADDITIONS = frozenset({
    ("from", "engine.render", "media_policy", None),
    ("from", "engine.render", "safe_fetch", None),
})
# worker/tasks/render.py
RENDER_REMOVALS = frozenset({
    ("from", "engine.render.asset_sourcer", "RENDER_ASSET_BUDGET_S", None),
    ("from", "engine.render.asset_sourcer", "asset_budget", None),
    ("from", "engine.render.asset_sourcer", "_monotonic", "_asset_clock"),
})
RENDER_ADDITIONS = frozenset({("from", "engine.render", "safe_fetch", None)})
# Stage 1 only: what the bridge may import (optional individually; the bridge itself is required).
BRIDGE_IMPORTS = frozenset({
    ("import", "sys", None, None),
    ("import", "types", None, None),
    ("from", "types", "ModuleType", None),
})


def _load_oracle():
    path = ROOT / "tests" / "_seam_oracle.py"
    spec = importlib.util.spec_from_file_location("tests._seam_oracle", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


O = _load_oracle()
MOVED = O.MOVED
NEW = O.NEW
BRIDGE_EXTRAS = O.BRIDGE_EXTRAS
STAYS = O.STAYS
NEW_NAME = {old: new for old, (_o, new, _k) in MOVED.items()}
OWNER_OF = {old: owner for old, (owner, _n, _k) in MOVED.items()}
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


# --------------------------------------------------------------------------------------------------- reporting
class Report:
    def __init__(self):
        self.fails = 0

    def ok(self, name, note=""):
        print(f"OK {name}" + (f" ({note})" if note else ""))

    def fail(self, name, problems):
        self.fails += 1
        problems = [problems] if isinstance(problems, str) else list(problems)
        print(f"FAIL {name}: {problems[0]}")
        for line in problems[1:12]:
            print("    " + line.replace("\n", "\n    "))
        if len(problems) > 12:
            print(f"    ... and {len(problems) - 12} more")

    def check(self, name, problems, note=""):
        if problems:
            self.fail(name, problems)
        else:
            self.ok(name, note)

    def skip(self, name, why):
        print(f"SKIP {name} ({why})")


# --------------------------------------------------------------------------------------------------- AST helpers
def git_show(rev, path):
    proc = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git show {rev}:{path} failed: {proc.stderr.strip()}")
    return proc.stdout


def node_names(node):
    if isinstance(node, FUNC_TYPES):
        return [node.name]
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def is_import(node):
    return isinstance(node, (ast.Import, ast.ImportFrom))


def node_key(node):
    names = node_names(node)
    return f"{type(node).__name__}:{names[0] if names else ast.unparse(node)[:50]}"


def dump(node):
    return ast.dump(node)


def import_keys(nodes):
    keys = []
    for n in nodes:
        if isinstance(n, ast.Import):
            keys += [("import", a.name, None, a.asname) for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                keys += [("from", "." * n.level + (n.module or ""), a.name, a.asname) for a in n.names]
            else:
                keys += [("from", n.module, a.name, a.asname) for a in n.names]
    return keys


def has_doc(node):
    body = getattr(node, "body", None)
    return bool(body) and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
        and isinstance(body[0].value.value, str)


def strip_doc(node):
    node = copy.deepcopy(node)
    if has_doc(node):
        node.body = node.body[1:] or [ast.Pass()]
    return node


def unparse_diff(expected, actual, limit=14):
    a = ast.unparse(expected).splitlines()
    b = ast.unparse(actual).splitlines()
    lines = list(difflib.unified_diff(a, b, "expected", "actual", lineterm="", n=0))
    lines = [ln for ln in lines if not ln.startswith(("---", "+++"))]
    return lines[:limit] + (["..."] if len(lines) > limit else [])


def binders(node):
    """Names a definition body binds (assignment targets, arguments, keywords, except names, nested defs)."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.keyword) and n.arg:
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, FUNC_TYPES) and n is not node:
            out.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
    return out


# --------------------------------------------------------------------------------------------------- transforms
def make_doc_rewriter():
    pairs = {old: new for old, new in NEW_NAME.items() if old != new}
    pattern = re.compile(r"(?<!\w)(" + "|".join(sorted(map(re.escape, pairs), key=len, reverse=True)) + r")(?!\w)")
    return lambda text: pattern.sub(lambda m: pairs[m.group(1)], text)


class Renamer(ast.NodeTransformer):
    """Check A: apply the section 4.1 rename table to a baseline node (and to the names inside its docstrings)."""

    def __init__(self):
        self.doc = make_doc_rewriter()

    def _docstring(self, node):
        if has_doc(node):
            const = node.body[0].value
            const.value = self.doc(const.value)

    def visit_FunctionDef(self, node):
        node.name = NEW_NAME.get(node.name, node.name)
        self._docstring(node)
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Name(self, node):
        node.id = NEW_NAME.get(node.id, node.id)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        node.attr = NEW_NAME.get(node.attr, node.attr)
        return node

    def visit_Global(self, node):
        node.names = [NEW_NAME.get(n, n) for n in node.names]
        return node

    visit_Nonlocal = visit_Global


def _attr(module, name):
    return ast.Attribute(value=ast.Name(id=module, ctx=ast.Load()), attr=name, ctx=ast.Load())


class Inverse(ast.NodeTransformer):
    """Check B: rewrite a baseline staying node into the form the new file must have.

    name_map: old name -> (module, new name) for bare Name loads; now_callees: callee names that become
    safe_fetch.now() (arguments untouched).
    """

    def __init__(self, name_map, now_callees):
        self.name_map = name_map
        self.now_callees = now_callees

    def visit_Name(self, node):
        if node.id in self.name_map and isinstance(node.ctx, ast.Load):
            module, new = self.name_map[node.id]
            return ast.copy_location(_attr(module, new), node)
        return node

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in self.now_callees:
            node.func = ast.copy_location(_attr("safe_fetch", "now"), node.func)
            node.args = [self.visit(a) for a in node.args]
            node.keywords = [self.visit(k) for k in node.keywords]
            return node
        self.generic_visit(node)
        return node


# --------------------------------------------------------------------------------------------------- baseline facts
def top_level_defs(tree):
    """name -> node for the top-level FunctionDef/ClassDef/Assign/AnnAssign names; also the duplicates."""
    defs, dups = {}, []
    for n in tree.body:
        for name in node_names(n):
            if name in defs:
                dups.append(name)
            defs[name] = n
    return defs, dups


def derived_kind(old, new, node):
    if isinstance(node, FUNC_TYPES) or old == "_monotonic":
        return "callable"
    return "data" if new.startswith("_") else "constant"


def import_bound_names(tree):
    out = set()
    for n in tree.body:
        if isinstance(n, ast.Import):
            out |= {(a.asname or a.name.split(".")[0]) for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out |= {(a.asname or a.name) for a in n.names}
    return out


# --------------------------------------------------------------------------------------------------- ORACLE
def oracle_checks(R, base_tree):
    defs, dups = top_level_defs(base_tree)
    R.check("ORACLE baseline defines each top-level name once", [f"duplicate: {d}" for d in dups])

    moved = set(MOVED)
    derived = {n for n in defs if n not in STAYS}
    probs = [f"in MOVED but not derived from the baseline: {n}" for n in sorted(moved - derived)]
    probs += [f"derived from the baseline (not in STAYS) but missing from MOVED: {n}" for n in sorted(derived - moved)]
    R.check("ORACLE MOVED keys == baseline top-level definitions minus STAYS", probs)

    R.check("ORACLE MOVED and STAYS are disjoint", [f"in both: {n}" for n in sorted(moved & set(STAYS))])

    counts = collections.Counter(o for o, _n, _k in MOVED.values())
    probs = []
    if len(MOVED) != 58:
        probs.append(f"len(MOVED) == {len(MOVED)}, expected 58")
    if counts != collections.Counter({"safe_fetch": 29, "media_policy": 29}):
        probs.append(f"owner counts {dict(counts)}, expected 29 + 29")
    if set(counts) - set(OWNERS):
        probs.append(f"unknown owner module: {sorted(set(counts) - set(OWNERS))}")
    R.check("ORACLE 58 moved names, 29 per owner", probs)

    probs = []
    for old, (owner, new, kind) in sorted(MOVED.items()):
        if old in defs:
            want = derived_kind(old, new, defs[old])
            if kind != want:
                probs.append(f"{old}: kind {kind!r}, derived {want!r}")
        if new != old and new != old[1:]:
            probs.append(f"{old}: new name {new!r} is neither the same nor the name without its leading underscore")
    constants = {new for _o, (_ow, new, k) in MOVED.items() if k == "constant"}
    if constants != {"MAX_API_JSON_BYTES", "RENDER_ASSET_BUDGET_S"}:
        probs.append(f"constant kinds are {sorted(constants)}, expected MAX_API_JSON_BYTES and RENDER_ASSET_BUDGET_S")
    R.check("ORACLE kinds derived from the baseline (callable / constant / data)", probs,
            f"{sum(k == 'callable' for *_x, k in MOVED.values())} callable, "
            f"{sum(k == 'constant' for *_x, k in MOVED.values())} constant, "
            f"{sum(k == 'data' for *_x, k in MOVED.values())} data")

    probs = []
    if NEW != {"now": ("safe_fetch", "callable")}:
        probs.append(f"NEW is {NEW!r}")
    if set(BRIDGE_EXTRAS) != {"httpx", "threading", "socket"}:
        probs.append(f"BRIDGE_EXTRAS is {sorted(BRIDGE_EXTRAS)!r}")
    R.check("ORACLE NEW and BRIDGE_EXTRAS", probs)

    bound = import_bound_names(base_tree)
    probs = [f"STAYS name not defined or imported in the baseline: {n}" for n in sorted(STAYS)
             if n not in defs and n not in bound]
    probs += [f"STAYS name defined in the baseline is also moved: {n}" for n in sorted(STAYS) if n in moved]
    R.check("ORACLE every STAYS name exists in the baseline", probs, f"{len(STAYS)} names")

    public = {o: sorted(new for _x, (ow, new, _k) in MOVED.items() if ow == o and not new.startswith("_"))
              for o in OWNERS}
    public["safe_fetch"] = sorted(public["safe_fetch"] + [n for n, (ow, _k) in NEW.items() if ow == "safe_fetch"])
    all_new = [new for _x, (_o, new, _k) in MOVED.items()] + list(NEW)
    probs = []
    if (len(public["safe_fetch"]), len(public["media_policy"])) != (13, 9):
        probs.append(f"public surface is {len(public['safe_fetch'])} + {len(public['media_policy'])}, expected 13 + 9")
    dup_new = [n for n, c in collections.Counter(all_new).items() if c > 1]
    probs += [f"new name used twice: {n}" for n in dup_new]
    R.check("ORACLE 22 public names (13 safe_fetch + 9 media_policy), new names unique", probs)


def oracle_owner_checks(R, trees):
    probs = []
    for owner in OWNERS:
        defined = set(top_level_defs(trees[owner])[0])
        want = {new for _o, (ow, new, _k) in MOVED.items() if ow == owner}
        want |= {n for n, (ow, _k) in NEW.items() if ow == owner}
        probs += [f"{owner} does not define {n}" for n in sorted(want - defined)]
    R.check("ORACLE owner modules define their new names", probs)


# --------------------------------------------------------------------------------------------------- check A
def check_a(R, base_tree, trees, stage):
    rewrite = Renamer()
    for owner in OWNERS:
        new_tree = trees[owner]
        body = list(new_tree.body)
        probs = []

        # --- shape of the top level
        i = 0
        doc_note = ""
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            i = 1
            doc_note = f"<module:{owner}>" if f"<module:{owner}>" in EDIT_ALLOWLIST else ""
            if not doc_note:
                probs.append(f"module docstring is not in EDIT_ALLOWLIST (<module:{owner}>)")
        imports = []
        while i < len(body) and is_import(body[i]):
            imports.append(body[i])
            i += 1
        if i >= len(body) or node_names(body[i]) != ["_log"]:
            probs.append("the node after the imports is not `_log = logging.getLogger(...)`")
            rest = body[i:]
        else:
            rest = body[i + 1:]
            probs += _log_problems(body[i], stage)

        # --- imports: module and name must come from a baseline import; no module-level PIL import
        base_keys = set(import_keys(base_tree.body))
        for key in import_keys(imports):
            if key[1] == "PIL" or str(key[1]).startswith("PIL."):
                probs.append(f"module-level PIL import: {key}")
            elif key not in base_keys:
                probs.append(f"import not in the baseline: {key}")

        # --- a stand-in for ruff F401: every top-level import must be used by the module's own code
        used = {n.id for r in body if not is_import(r) for n in ast.walk(r) if isinstance(n, ast.Name)}
        for n in imports:
            for a in n.names:
                bound = a.asname or a.name.split(".")[0]
                if bound not in used:
                    probs.append(f"unused import: {bound}")

        # --- the moved nodes: names, order, dumps
        expected = [n for n in base_tree.body if node_names(n) and node_names(n)[0] in MOVED
                    and OWNER_OF[node_names(n)[0]] == owner]
        want_names = []
        for n in expected:
            want_names.append(NEW_NAME[node_names(n)[0]])
            if owner == "safe_fetch" and node_names(n)[0] == "_monotonic":
                want_names.append("now")
        got_names = [(node_names(n) or [f"<{type(n).__name__}>"])[0] for n in rest]
        if got_names != want_names:
            probs += _list_diff(want_names, got_names)
        by_name = {}
        for n in rest:
            by_name.setdefault((node_names(n) or [None])[0], n)
        notes = []
        for base_node in expected:
            old = node_names(base_node)[0]
            new = NEW_NAME[old]
            actual = by_name.get(new)
            if actual is None:
                continue  # already reported by the name list
            exp = rewrite.visit(copy.deepcopy(base_node))
            if old in EDIT_ALLOWLIST:
                exp, actual_cmp = strip_doc(exp), strip_doc(actual)
                if not has_doc(actual):
                    probs.append(f"{old} -> {new}: allowlisted docstring edit but the new node has no docstring")
                notes.append(old)
            else:
                actual_cmp = actual
            if dump(exp) != dump(actual_cmp):
                probs.append(f"{old} -> {new} differs from the renamed baseline:")
                probs += ["  " + ln for ln in unparse_diff(exp, actual_cmp)]
        if owner == "safe_fetch":
            probs += _now_problems(by_name.get("now"))
        extra = f"{len(expected)} moved nodes"
        if notes:
            extra += "; docstring edits allowed: " + ", ".join(notes)
        if not doc_note:
            extra += "; no module docstring"
        R.check(f"A {owner}: moved definitions equal the renamed baseline, top level is exactly as specified",
                probs, extra)


def _log_problems(node, stage):
    want = "engine.render.asset_sourcer" if stage <= 1 else None
    ok = (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.value, ast.Call)
          and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "getLogger"
          and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == "logging"
          and len(node.value.args) == 1 and not node.value.keywords)
    if not ok:
        return ["`_log` is not `logging.getLogger(<one argument>)`"]
    arg = node.value.args[0]
    if want is not None:
        if not (isinstance(arg, ast.Constant) and arg.value == want):
            return [f"stage 1: `_log` must be getLogger({want!r}), got {ast.unparse(arg)}"]
    elif not (isinstance(arg, ast.Name) and arg.id == "__name__"):
        return [f"stage >= 2: `_log` must be getLogger(__name__), got {ast.unparse(arg)}"]
    return []


def _now_problems(node):
    if node is None:
        return []  # reported by the name list
    node = strip_doc(node)
    want = ast.parse("def now() -> float:\n    return _monotonic()\n").body[0]
    if dump(node) != dump(want):
        return ["`now` is not `def now() -> float: return _monotonic()`:"] + ["  " + ln for ln in unparse_diff(want, node)]
    return []


def _list_diff(want, got):
    out = []
    if sorted(want) != sorted(got):
        out += [f"missing: {n}" for n in want if n not in got]
        out += [f"unexpected top-level node: {n}" for n in got if n not in want]
    else:
        pos = next(k for k, (a, b) in enumerate(zip(want, got)) if a != b)
        out.append(f"order differs at position {pos}: expected {want[pos]}, found {got[pos]}")
    return out


# --------------------------------------------------------------------------------------------------- check B
def _compare_multiset(expected, actual):
    ce, ca = collections.Counter(map(dump, expected)), collections.Counter(map(dump, actual))
    if ce == ca:
        return [], []
    miss_d, extra_d = ce - ca, ca - ce
    miss, extra = [], []
    for n in expected:
        if miss_d.get(dump(n), 0) > 0:
            miss_d[dump(n)] -= 1
            miss.append(n)
    for n in actual:
        if extra_d.get(dump(n), 0) > 0:
            extra_d[dump(n)] -= 1
            extra.append(n)
    return miss, extra


def _describe_diffs(miss, extra):
    probs = []
    pool = list(extra)
    for m in miss:
        twin = next((e for e in pool if node_key(e) == node_key(m)), None)
        if twin is not None:
            pool.remove(twin)
            probs.append(f"{node_key(m)} differs from the inverse-transformed baseline:")
            probs += ["  " + ln for ln in unparse_diff(m, twin)]
        else:
            probs.append(f"missing from the new file: {node_key(m)}")
    probs += [f"unexpected in the new file: {node_key(e)}" for e in pool]
    return probs


def _binder_problems(label, nodes, forbidden):
    probs = []
    for n in nodes:
        bad = binders(n) & forbidden
        if bad:
            probs.append(f"{label}: {node_key(n)} binds {sorted(bad)}")
    return probs


def _bridge_problems(extra, table_expected):
    """Stage 1: the extra nodes must be exactly the bridge class, its literal table and the __class__ assignment."""
    classes = [n for n in extra if isinstance(n, ast.ClassDef) and any(
        (isinstance(b, ast.Attribute) and b.attr == "ModuleType") or (isinstance(b, ast.Name) and b.id == "ModuleType")
        for b in n.bases)]
    tables = [n for n in extra if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict)]
    swaps = [n for n in extra if isinstance(n, ast.Assign) and len(n.targets) == 1
             and isinstance(n.targets[0], ast.Attribute) and n.targets[0].attr == "__class__"
             and "__name__" in ast.unparse(n.targets[0]) and "modules" in ast.unparse(n.targets[0])]
    probs = []
    for label, found in (("bridge class (a ModuleType subclass)", classes), ("literal bridge table", tables),
                         ("sys.modules[__name__].__class__ assignment", swaps)):
        if len(found) != 1:
            probs.append(f"stage 1 expects exactly one {label}, found {len(found)}")
    recognized = classes + tables + swaps
    if len(tables) == 1:
        try:
            table = ast.literal_eval(tables[0].value)
        except ValueError:
            return probs + ["the bridge table is not a literal"], recognized
        norm = {}
        for k, v in table.items():
            if isinstance(v, str) and "." in v:
                v = tuple(v.split(".", 1))
            norm[k] = tuple(v) if isinstance(v, (list, tuple)) else v
        if norm != table_expected:
            probs += [f"bridge table lacks: {sorted(set(table_expected) - set(norm))}",
                      f"bridge table has unexpected keys: {sorted(set(norm) - set(table_expected))}",
                      f"bridge table value mismatches: "
                      f"{sorted(k for k in set(norm) & set(table_expected) if norm[k] != table_expected[k])}"]
            probs = [p for p in probs if not p.endswith(": []")]
    return probs, recognized


def check_b_asset(R, base_tree, new_tree, stage):
    moved_nodes = [n for n in base_tree.body if node_names(n) and node_names(n)[0] in MOVED]
    staying = [n for n in base_tree.body if not is_import(n) and n not in moved_nodes]
    forbidden = set(MOVED) | {"safe_fetch", "media_policy"}
    new_nodes = [n for n in new_tree.body if not is_import(n)]
    probs = _binder_problems("baseline", staying, forbidden) + _binder_problems("new file", new_nodes, forbidden)

    name_map = {old: (owner, new) for old, (owner, new, _k) in MOVED.items()}
    inverse = Inverse(name_map, {"_monotonic"})
    expected = [inverse.visit(copy.deepcopy(n)) for n in staying]
    expected_cmp = [strip_doc(n) if (node_names(n) or [None])[0] in EDIT_ALLOWLIST else n for n in expected]
    actual_cmp = [strip_doc(n) if (node_names(n) or [None])[0] in EDIT_ALLOWLIST else n for n in new_nodes]
    miss, extra = _compare_multiset(expected_cmp, actual_cmp)
    if stage == 1:
        table_expected = {old: (owner, new) for old, (owner, new, _k) in MOVED.items()}
        table_expected.update({x: ("safe_fetch", x) for x in BRIDGE_EXTRAS})
        bridge_probs, recognized = _bridge_problems(extra, table_expected)
        probs += bridge_probs
        extra = [e for e in extra if e not in recognized]
    probs += _describe_diffs(miss, extra)
    R.check(f"B {ASSET_PATH}: staying nodes equal the inverse-transformed baseline", probs,
            f"{len(staying)} nodes" + ("; bridge present" if stage == 1 else ""))

    # imports
    base_keys = collections.Counter(import_keys(base_tree.body))
    removed = _derive_unused_imports(base_tree, staying)
    probs = []
    if removed != set(PLANNED_REMOVALS):
        probs += [f"derived as unused by staying code but not in the planned removals: {sorted(removed - PLANNED_REMOVALS)}",
                  f"in the planned removals but still used by staying code: {sorted(PLANNED_REMOVALS - removed)}"]
        probs = [p for p in probs if not p.endswith(": []")]
    R.check("B planned import removals == baseline imports with no staying-code user", probs,
            f"{len(removed)} imports")

    expected_keys = collections.Counter(k for k in base_keys.elements() if k not in PLANNED_REMOVALS)
    expected_keys.update(PLANNED_ADDITIONS)
    actual_keys = collections.Counter(import_keys([n for n in new_tree.body if is_import(n)]))
    probs = []
    extra_keys = actual_keys - expected_keys
    if stage == 1:
        for k in list(extra_keys):
            if k in BRIDGE_IMPORTS:
                del extra_keys[k]
    probs += [f"missing import: {k}" for k in (expected_keys - actual_keys)]
    probs += [f"unexpected import: {k}" for k in extra_keys]
    R.check(f"B {ASSET_PATH}: imports are the baseline's minus the planned removals plus the planned additions", probs)


def _derive_unused_imports(base_tree, staying):
    names, chains = set(), set()
    for n in staying:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                parts, cur = [], sub
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    full = [cur.id] + parts[::-1]
                    chains |= {".".join(full[:k]) for k in range(1, len(full) + 1)}
    unused = set()
    for kind, module, name, asname in import_keys(base_tree.body):
        if kind == "import":
            used = (module in chains) if asname is None else (asname in names)
        else:
            used = (asname or name) in names
        if not used:
            unused.add((kind, module, name, asname))
    return unused


def check_b_render(R, base_tree, new_tree):
    staying = [n for n in base_tree.body if not is_import(n)]
    forbidden = set(MOVED) | {"safe_fetch", "media_policy", "_asset_clock"}
    new_nodes = [n for n in new_tree.body if not is_import(n)]
    probs = _binder_problems("baseline", staying, forbidden) + _binder_problems("new file", new_nodes, forbidden)
    name_map = {"asset_budget": ("safe_fetch", "asset_budget"),
                "RENDER_ASSET_BUDGET_S": ("safe_fetch", "RENDER_ASSET_BUDGET_S")}
    inverse = Inverse(name_map, {"_asset_clock"})
    expected = [inverse.visit(copy.deepcopy(n)) for n in staying]
    miss, extra = _compare_multiset(expected, new_nodes)
    probs += _describe_diffs(miss, extra)
    R.check(f"B {RENDER_PATH}: nodes equal the inverse-transformed baseline", probs, f"{len(staying)} nodes")

    expected_keys = collections.Counter(k for k in import_keys(base_tree.body) if k not in RENDER_REMOVALS)
    expected_keys.update(RENDER_ADDITIONS)
    actual_keys = collections.Counter(import_keys([n for n in new_tree.body if is_import(n)]))
    probs = [f"missing import: {k}" for k in (expected_keys - actual_keys)]
    probs += [f"unexpected import: {k}" for k in (actual_keys - expected_keys)]
    R.check(f"B {RENDER_PATH}: imports are the baseline's minus the planned removals plus the planned additions", probs)


# --------------------------------------------------------------------------------------------------- dependency check
def outside_uses(tree_root):
    """(owner, name) -> set of production files outside the owner that use it through the module / the bridge."""
    uses, bridge_uses = collections.defaultdict(set), collections.defaultdict(set)
    for sub in PRODUCTION_DIRS:
        for path in sorted((tree_root / sub).rglob("*.py")):
            rel = path.relative_to(tree_root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            alias = {}
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.module == "engine.render":
                    for a in n.names:
                        if a.name in OWNERS:
                            alias[a.asname or a.name] = a.name
                elif isinstance(n, ast.ImportFrom) and n.module in {f"engine.render.{o}" for o in OWNERS}:
                    for a in n.names:
                        uses[(n.module.rsplit(".", 1)[1], a.name)].add(rel)
                elif isinstance(n, ast.ImportFrom) and n.module == "engine.render.asset_sourcer":
                    for a in n.names:
                        if a.name in MOVED:
                            bridge_uses[(OWNER_OF[a.name], NEW_NAME[a.name])].add(rel)
                elif isinstance(n, ast.Import):
                    for a in n.names:
                        if a.name in {f"engine.render.{o}" for o in OWNERS} and a.asname:
                            alias[a.asname] = a.name.rsplit(".", 1)[1]
            for n in ast.walk(tree):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in alias:
                    uses[(alias[n.value.id], n.attr)].add(rel)
    for key in list(uses):
        uses[key] = {f for f in uses[key] if f != OWNER_PATH[key[0]]}
    return uses, bridge_uses


def dependency_checks(R, base_tree, trees, asset_tree, tree_root):
    asset_defs = set(top_level_defs(base_tree)[0]) - set(MOVED)
    asset_defs |= set(top_level_defs(asset_tree)[0])
    probs = []
    for owner in OWNERS:
        tree = trees[owner]
        own = set(top_level_defs(tree)[0]) | import_bound_names(tree)
        other = [o for o in OWNERS if o != owner][0]
        other_names = {new for _x, (ow, new, _k) in MOVED.items() if ow == other}
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        probs += [f"{owner} references {n}, defined in asset_sourcer" for n in sorted((used & asset_defs) - own)]
        probs += [f"{owner} references {n}, defined in {other}" for n in sorted((used & other_names) - own)]
        for n in tree.body:
            mods = []
            if isinstance(n, ast.ImportFrom):
                mods = [n.module or ""] + [f"{n.module}.{a.name}" for a in n.names]
            elif isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            probs += [f"{owner} imports {m}" for m in mods if "asset_sourcer" in m or m.endswith(
                ("safe_fetch", "media_policy"))]
    R.check("DEP safe_fetch and media_policy reference nothing from asset_sourcer or from each other", probs)

    uses, bridge_uses = outside_uses(tree_root)
    probs = []
    public = [(ow, new) for _o, (ow, new, _k) in MOVED.items() if not new.startswith("_")]
    public += [(ow, n) for n, (ow, _k) in NEW.items()]
    for key in sorted(public):
        if not uses.get(key) and not bridge_uses.get(key):
            probs.append(f"public name {key[0]}.{key[1]} has no caller outside {key[0]}")
    R.check("DEP every public name has an outside caller", probs, f"{len(public)} names")
    probs = [f"private name {owner}.{name} is used from {sorted(files)}"
             for (owner, name), files in sorted(uses.items()) if name.startswith("_") and files]
    R.check("DEP no private name has an outside caller", probs)


# --------------------------------------------------------------------------------------------------- check C (tests)
ASSET_MOD = "engine.render.asset_sourcer"
ASSET_PREFIX = ASSET_MOD + "."
STDLIB_BARE = frozenset(BRIDGE_EXTRAS)
SEL_MODULE = "tests.test_sourcer_selection"
CONFTEST = "tests/conftest.py"
RENDER_TEST = "tests/test_render_task.py"
SEL_TEST = "tests/test_sourcer_selection.py"
CLOCK_TARGET = "worker.tasks.render._asset_clock"
TEST_EXTRA_FILES = {
    1: {"tests/_seam_oracle.py", "tests/test_bridge_forwarding.py"},
    2: {"tests/_seam_oracle.py"},
    3: {"tests/_seam_oracle.py", "tests/_seam_ast.py", "tests/test_module_seams.py"},
}
SETATTR_FUNCS = frozenset({"setattr", "delattr", "getattr"})


def _is_str(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def asset_aliases(tree):
    """Local names bound to the asset_sourcer module (`from engine.render import asset_sourcer as AS`)."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "engine.render" and not n.level:
            out |= {a.asname or a.name for a in n.names if a.name == "asset_sourcer"}
    return out


def retarget_string(value):
    """The expected form of a plain string patch target, or None when it is not a moved/extra target."""
    if not value.startswith(ASSET_PREFIX):
        return None
    rest = value[len(ASSET_PREFIX):]
    comp = rest.split(".")[0]
    if comp in MOVED:
        owner, new, _k = MOVED[comp]
        return f"engine.render.{owner}.{new}" + rest[len(comp):]
    if comp in BRIDGE_EXTRAS:
        return rest
    return None


class TestRewriter(ast.NodeTransformer):
    """Check C: the deterministic transform of a baseline test tree into its expected retargeted form.

    One rule per shape (spec section 6, check C), all driven by the oracle: `AS.x` attributes (Load/Store/Del), the
    name string of setattr/delattr/getattr/patch.object on AS, plain and f-string patch targets, `AS.__name__` in an
    f-string target, the conftest `raising` flag, the added `_FETCH_MOD` assignment and the two `_asset_clock`
    with-items. Imports, docstrings and `logger=` values are NOT rewritten here: the Normalizer ignores them.
    """

    def __init__(self, rel, aliases):
        self.rel, self.aliases = rel, aliases
        self.removed_clock_items = 0
        self.added_fetch_mod = False

    # --- helpers
    def _is_as(self, node):
        return isinstance(node, ast.Name) and node.id in self.aliases

    def _is_as_dunder_name(self, node):
        return isinstance(node, ast.Attribute) and node.attr == "__name__" and self._is_as(node.value)

    # --- attributes: AS.x
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if self._is_as(node.value):
            if node.attr in MOVED:
                owner, new, _k = MOVED[node.attr]
                return ast.copy_location(ast.Attribute(value=ast.Name(id=owner, ctx=ast.Load()), attr=new, ctx=node.ctx), node)
            if node.attr in BRIDGE_EXTRAS:
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
        return node

    # --- calls: setattr/delattr/getattr/patch.object(AS, "name", ...), conftest raising flag
    def visit_Call(self, node):
        f = node.func
        by_name = (isinstance(f, ast.Attribute) and f.attr in SETATTR_FUNCS) or (isinstance(f, ast.Name) and f.id in SETATTR_FUNCS)
        patch_object = isinstance(f, ast.Attribute) and f.attr == "object" and isinstance(f.value, ast.Name) and f.value.id == "patch"
        if (by_name or patch_object) and len(node.args) >= 2 and self._is_as(node.args[0]) and _is_str(node.args[1]) \
                and node.args[1].value in MOVED:
            owner, new, _k = MOVED[node.args[1].value]
            node.args[0] = ast.Name(id=owner, ctx=ast.Load())
            node.args[1] = ast.Constant(value=new)
        conftest_target = self.rel == CONFTEST and isinstance(f, ast.Attribute) and f.attr == "setattr" and bool(node.args) \
            and _is_str(node.args[0]) and node.args[0].value.startswith(ASSET_PREFIX)
        self.generic_visit(node)
        if conftest_target:
            node.keywords = [k for k in node.keywords if not (
                k.arg == "raising" and isinstance(k.value, ast.Constant) and k.value.value is False)]
        return node

    # --- plain string targets
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            new = retarget_string(node.value)
            if new is not None:
                return ast.copy_location(ast.Constant(value=new), node)
        return node

    # --- f-string targets
    def visit_JoinedStr(self, node):
        vals = node.values
        if len(vals) >= 2 and isinstance(vals[0], ast.FormattedValue) and vals[0].conversion == -1 \
                and vals[0].format_spec is None and _is_str(vals[1]) and vals[1].value.startswith("."):
            head = vals[0].value
            kind = None
            if isinstance(head, ast.Name) and head.id in ("_MOD", "_LOG"):
                kind = head.id
            elif self._is_as_dunder_name(head):
                kind = "AS.__name__"
            if kind is not None:
                tail = vals[1].value
                comp = tail[1:].split(".")[0]
                rest = vals[2:]
                if comp in BRIDGE_EXTRAS:
                    text = tail[1:]
                    return ast.copy_location(
                        ast.Constant(value=text) if not rest else ast.JoinedStr(values=[ast.Constant(value=text)] + rest), node)
                if comp in MOVED:
                    owner, new, _k = MOVED[comp]
                    if owner != "safe_fetch" or kind == "_LOG":
                        raise ValueError(f"{self.rel}: no expected form for an f-string target {kind}.{comp}")
                    fv = ast.FormattedValue(value=ast.Name(id="_FETCH_MOD", ctx=ast.Load()), conversion=-1, format_spec=None)
                    return ast.copy_location(ast.JoinedStr(values=[fv, ast.Constant(value="." + new + tail[1 + len(comp):])] + rest), node)
                if kind == "AS.__name__":
                    fv = ast.FormattedValue(value=ast.Name(id="_MOD", ctx=ast.Load()), conversion=-1, format_spec=None)
                    return ast.copy_location(ast.JoinedStr(values=[fv] + vals[1:]), node)
        self.generic_visit(node)
        return node

    # --- the two `worker.tasks.render._asset_clock` with-items (matched by shape)
    def visit_With(self, node):
        self.generic_visit(node)
        if self.rel == RENDER_TEST:
            keep = []
            for item in node.items:
                e = item.context_expr
                if isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id == "patch" and e.args \
                        and _is_str(e.args[0]) and e.args[0].value == CLOCK_TARGET and item.optional_vars is None:
                    self.removed_clock_items += 1
                else:
                    keep.append(item)
            node.items = keep
        return node

    # --- the one added `_FETCH_MOD` assignment, right after `_MOD = ...`
    def visit_Module(self, node):
        self.generic_visit(node)
        if self.rel == SEL_TEST:
            body = []
            for n in node.body:
                body.append(n)
                if isinstance(n, ast.Assign) and [t.id for t in n.targets if isinstance(t, ast.Name)] == ["_MOD"]:
                    body.append(ast.Assign(targets=[ast.Name(id="_FETCH_MOD", ctx=ast.Store())],
                                           value=ast.Constant(value="engine.render.safe_fetch"), lineno=0))
                    self.added_fetch_mod = True
            node.body = body
        return node


class Normalizer(ast.NodeTransformer):
    """Check C, applied to BOTH trees: erase exactly the residual differences the spec lists.

    docstring text (presence is kept); `logger=<AS|safe_fetch|media_policy>.__name__` values; the whitelisted
    `from engine.render import ...` / bare stdlib imports; the name list of the module-level
    `from tests.test_sourcer_selection import ...`. Everything else is compared exactly.
    """

    def __init__(self, aliases):
        self.aliases = set(aliases)
        self.logger_names = []   # (lineno, module name) of every normalized logger value
        self.dropped = []
        self.sel_names = []      # (lineno, [names]) of module-level selection imports

    @staticmethod
    def _whitelisted_import(n):
        if isinstance(n, ast.ImportFrom) and n.module == "engine.render" and not n.level:
            return all(a.name in ("asset_sourcer", "safe_fetch", "media_policy") for a in n.names)
        if isinstance(n, ast.Import):
            return all(a.name in STDLIB_BARE and a.asname is None for a in n.names)
        return False

    def _strip_body(self, body):
        out = []
        for n in body:
            if self._whitelisted_import(n):
                self.dropped.append(n)
                continue
            out.append(n)
        return out or [ast.Pass()]

    def _docstring(self, node):
        b = node.body
        if b and isinstance(b[0], ast.Expr) and _is_str(b[0].value):
            b[0] = ast.Expr(value=ast.Constant(value=""))

    def _visit_scope(self, node, module_level=False):
        self._docstring(node)
        self.generic_visit(node)
        node.body = self._strip_body(node.body) if node.body else node.body
        if module_level:
            for n in node.body:
                if isinstance(n, ast.ImportFrom) and n.module == SEL_MODULE:
                    self.sel_names.append((n.lineno, sorted(a.name for a in n.names)))
                    n.names = []
        return node

    def visit_Module(self, node):
        return self._visit_scope(node, module_level=True)

    def visit_FunctionDef(self, node):
        return self._visit_scope(node)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def _strip_other_bodies(self, node):
        for field in ("body", "orelse", "finalbody"):
            if isinstance(getattr(node, field, None), list):
                setattr(node, field, self._strip_body(getattr(node, field)))
        return node

    def visit_With(self, node):
        self.generic_visit(node)
        return self._strip_other_bodies(node)

    visit_If = visit_Try = visit_For = visit_While = visit_With

    def visit_keyword(self, node):
        self.generic_visit(node)
        v = node.value
        if node.arg == "logger" and isinstance(v, ast.Attribute) and v.attr == "__name__" and isinstance(v.value, ast.Name) \
                and (v.value.id in self.aliases or v.value.id in ("safe_fetch", "media_policy")):
            self.logger_names.append((getattr(v, "lineno", 0), "asset_sourcer" if v.value.id in self.aliases else v.value.id))
            node.value = ast.Constant(value="<module __name__>")
        return node


def _count(tree, pred):
    return sum(1 for n in ast.walk(tree) if pred(n))


def _is_call_named(n, names):
    if not isinstance(n, ast.Call):
        return False
    f = n.func
    return (isinstance(f, ast.Attribute) and f.attr in names) or (isinstance(f, ast.Name) and f.id in names)


def test_function_names(tree):
    out = []

    def walk(body, prefix):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
                out.append(prefix + n.name)
            elif isinstance(n, ast.ClassDef):
                walk(n.body, prefix + n.name + ".")
    walk(tree.body, "")
    return sorted(out)


def _name_uses(tree, name):
    return any(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load) for n in ast.walk(tree))


def _top_level_diff(exp, act, limit=3):
    lines = []
    ea, aa = exp.body, act.body
    if len(ea) != len(aa):
        lines.append(f"top-level statement count: expected {len(ea)}, found {len(aa)}")
    shown = 0
    for i, (a, b) in enumerate(zip(ea, aa)):
        if dump(a) != dump(b):
            lines.append(f"statement {i} ({node_key(a)}) differs:")
            lines += ["  " + ln for ln in unparse_diff(a, b)]
            shown += 1
            if shown >= limit:
                break
    if not lines:
        lines.append("trees differ outside the top-level statements (module docstring / type_ignores)")
    return lines


def test_python():
    """The interpreter that has the project's dependencies: the repo venv when present, else the running one."""
    venv = ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def collect_node_ids(root):
    proc = subprocess.run([test_python(), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                          cwd=root, capture_output=True, text=True)
    ids = sorted(ln.strip() for ln in proc.stdout.splitlines() if "::" in ln and not ln.startswith(" "))
    return ids, proc.returncode, proc.stdout[-400:] + proc.stderr[-400:]


def export_revision(rev, dest):
    arch = subprocess.run(["git", "archive", rev], cwd=ROOT, capture_output=True)
    if arch.returncode != 0:
        raise SystemExit(f"git archive {rev} failed: {arch.stderr.decode()[:200]}")
    subprocess.run(["tar", "-x", "-C", str(dest)], input=arch.stdout, check=True)


def check_c(R, baseline, tree_root, stage, collect):
    ls = subprocess.run(["git", "ls-tree", "-r", "--name-only", baseline, "--", "tests"], cwd=ROOT, capture_output=True, text=True)
    base_files = sorted(f for f in ls.stdout.split() if f.endswith(".py"))
    new_files = sorted(p.relative_to(tree_root).as_posix() for p in (tree_root / "tests").rglob("*.py")
                       if "__pycache__" not in p.parts)
    extra_ok = TEST_EXTRA_FILES.get(stage, TEST_EXTRA_FILES[3])

    # --- the set of test files
    probs = [f"baseline test file missing: {f}" for f in sorted(set(base_files) - set(new_files))]
    probs += [f"unexpected new test file: {f}" for f in sorted(set(new_files) - set(base_files) - extra_ok)]
    R.check("C the set of test files is the baseline's (plus the allowed new ones)", probs,
            f"{len(base_files)} baseline files; allowed additions: {sorted(extra_ok)}")

    # --- per-file AST equality after the deterministic rewrite
    probs = []
    totals = collections.Counter()
    loggers = collections.Counter()
    clock_removed = 0
    added_fetch = False
    for rel in base_files:
        if rel not in new_files:
            continue
        base_src = git_show(baseline, rel)
        new_src = (tree_root / rel).read_text(encoding="utf-8")
        base_tree, new_tree = ast.parse(base_src), ast.parse(new_src)
        aliases = asset_aliases(base_tree)
        rewriter = TestRewriter(rel, aliases)
        try:
            expected = rewriter.visit(copy.deepcopy(base_tree))
        except ValueError as exc:
            probs.append(str(exc))
            continue
        clock_removed += rewriter.removed_clock_items
        added_fetch = added_fetch or rewriter.added_fetch_mod
        norm_exp, norm_act = Normalizer(aliases), Normalizer(aliases)
        exp_n, act_n = norm_exp.visit(expected), norm_act.visit(copy.deepcopy(new_tree))
        if dump(exp_n) != dump(act_n):
            probs.append(f"{rel} differs from the rewritten baseline:")
            probs += ["  " + ln for ln in _top_level_diff(exp_n, act_n)]
        # residual checks that the normalizer erased
        n_pre = [n for n in ast.walk(new_tree) if Normalizer._whitelisted_import(n)]
        for n in n_pre:
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name == "asset_sourcer" and a.asname != "AS":
                        probs.append(f"{rel}:{n.lineno}: asset_sourcer must be imported as AS")
                    if a.name != "asset_sourcer" and a.asname is not None:
                        probs.append(f"{rel}:{n.lineno}: {a.name} must be imported without an alias")
        if norm_exp.sel_names or norm_act.sel_names:
            old_names = set(sum((n for _l, n in norm_exp.sel_names), []))
            new_names = set(sum((n for _l, n in norm_act.sel_names), []))
            added, removed = new_names - old_names, old_names - new_names
            if not added <= {"_FETCH_MOD"}:
                probs.append(f"{rel}: names added to the {SEL_MODULE} import: {sorted(added - {'_FETCH_MOD'})}")
            if not removed <= {"_MOD"}:
                probs.append(f"{rel}: names dropped from the {SEL_MODULE} import: {sorted(removed - {'_MOD'})}")
            if "_MOD" in removed and _name_uses(new_tree, "_MOD"):
                probs.append(f"{rel}: _MOD is dropped from the import but still used")
            if "_FETCH_MOD" in added and not _name_uses(new_tree, "_FETCH_MOD"):
                probs.append(f"{rel}: _FETCH_MOD is imported but not used")
            if "_MOD" not in removed and "_MOD" in old_names and not _name_uses(new_tree, "_MOD"):
                probs.append(f"{rel}: _MOD is imported and no longer used (F401)")
        for _ln, mod in norm_act.logger_names:
            loggers[mod] += 1
        if len(norm_exp.logger_names) != len(norm_act.logger_names):
            probs.append(f"{rel}: logger= keyword count changed: {len(norm_exp.logger_names)} -> {len(norm_act.logger_names)}")
        # counts
        for label, pred in (("assert", lambda n: isinstance(n, ast.Assert)),
                            ("raises", lambda n: _is_call_named(n, {"raises"})),
                            ("approx", lambda n: _is_call_named(n, {"approx"}))):
            before, after = _count(base_tree, pred), _count(new_tree, pred)
            totals[label] += after
            if before != after:
                probs.append(f"{rel}: number of `{label}` changed: {before} -> {after}")
        if test_function_names(base_tree) != test_function_names(new_tree):
            probs.append(f"{rel}: the set of test function names changed")
        totals["tests"] += len(test_function_names(new_tree))
    if clock_removed != 2:
        probs.append(f"expected exactly the two `{CLOCK_TARGET}` with-items to be removed in {RENDER_TEST}, found {clock_removed}")
    if not added_fetch:
        probs.append(f"{SEL_TEST}: no `_FETCH_MOD` assignment was expected/added")
    if sum(loggers.values()) != 22:
        probs.append(f"expected 22 `logger=<module>.__name__` keywords in the tests, found {sum(loggers.values())}")
    R.check("C every baseline test file == its deterministic rewrite (residuals only: imports, docstrings, logger=, "
            "name lists, conftest raising, _FETCH_MOD, 2 clock items)", probs,
            f"{len(base_files)} files; loggers {dict(sorted(loggers.items()))}; asserts {totals['assert']}, "
            f"raises {totals['raises']}, approx {totals['approx']}, test functions {totals['tests']}")

    # --- pytest node ids
    if not collect:
        R.skip("C pytest node ids", "--no-collect")
        return
    with tempfile.TemporaryDirectory(prefix="fidelity-baseline-") as tmp:
        tmp_path = Path(tmp)
        export_revision(baseline, tmp_path)
        where = subprocess.run([test_python(), "-c", "import engine; print(engine.__file__)"], cwd=tmp_path,
                               capture_output=True, text=True).stdout.strip()
        if not where.startswith(str(tmp_path.resolve())) and not where.startswith(str(tmp_path)):
            R.fail("C pytest node ids", f"the exported baseline would import engine from {where}, not from the export")
            return
        base_ids, rc_b, tail_b = collect_node_ids(tmp_path)
    new_ids, rc_n, tail_n = collect_node_ids(tree_root)
    extra_prefixes = tuple(f + "::" for f in extra_ok)
    new_core = [i for i in new_ids if not i.startswith(extra_prefixes)]
    probs = []
    if rc_b not in (0,) or rc_n not in (0,):
        probs.append(f"collection failed (baseline rc {rc_b}, new rc {rc_n}): {tail_b!r} {tail_n!r}")
    if base_ids != new_core:
        sb, sn = set(base_ids), set(new_core)
        probs += [f"missing after the change: {i}" for i in sorted(sb - sn)[:10]]
        probs += [f"new after the change: {i}" for i in sorted(sn - sb)[:10]]
        if sb == sn:
            probs.append("same ids but different multiplicity (duplicate ids)")
    R.check("C sorted pytest node ids are identical before and after (new allowed files excluded)", probs,
            f"{len(base_ids)} ids, baseline collected from a git archive export")


# --------------------------------------------------------------------------------------------------- G1 resolver mode
COUNTS_SCRIPT = "docs/specs/2026-11-safe-fetch-media-policy-counts.py"


def load_seam_ast(tree_root):
    """tests/_seam_ast.py of `tree_root`, loaded by path (it needs only the standard library and the oracle)."""
    path = Path(tree_root) / "tests" / "_seam_ast.py"
    spec = importlib.util.spec_from_file_location("seam_ast_for_fidelity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module               # dataclasses looks its own module up in sys.modules
    spec.loader.exec_module(module)
    return module


def current_test_sources(tree_root):
    return {p.relative_to(tree_root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted((Path(tree_root) / "tests").glob("**/*.py"))}


def baseline_test_sources(rev):
    listing = subprocess.run(["git", "ls-tree", "-r", "--name-only", rev, "tests/"], cwd=ROOT,
                             capture_output=True, text=True)
    if listing.returncode != 0:
        raise SystemExit(f"git ls-tree {rev} tests/ failed: {listing.stderr.strip()}")
    return {name: git_show(rev, name) for name in listing.stdout.split() if name.endswith(".py")}


def recorded_g1_counts(tree_root):
    """G1_EXPECTED_COUNTS as written in tests/test_module_seams.py (read from its AST, nothing is imported)."""
    path = Path(tree_root) / "tests" / "test_module_seams.py"
    if not path.exists():
        return None
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "G1_EXPECTED_COUNTS" for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def print_g1_figures(label, counts):
    print(f"  {label}: resolved patch targets per owning module and form")
    by_owner = collections.defaultdict(list)
    for (owner, form), n in sorted(counts.items()):
        by_owner[owner].append((form, n))
    for owner in sorted(by_owner):
        total = sum(n for _f, n in by_owner[owner])
        print(f"    {owner:14s} {total:4d}   " + "  ".join(f"{form}={n}" for form, n in by_owner[owner]))
    print(f"    {'all':14s} {sum(counts.values()):4d}")


def check_g1_resolver(R, tree_root):
    seam = load_seam_ast(tree_root)
    sources = current_test_sources(tree_root)
    problems = [str(p) for p in seam.g1_problems(sources)]
    R.check("G1 resolver: no stale access through asset_sourcer, nothing unresolved, safe_fetch.now never patched", problems,
            f"{len(sources)} test files")
    counts = seam.g1_counts(sources)
    print_g1_figures("real tree", counts)
    recorded = recorded_g1_counts(tree_root)
    if recorded is None:
        R.skip("G1 recorded figures", "tests/test_module_seams.py not found or has no G1_EXPECTED_COUNTS")
        return
    probs = [f"{k}: resolver {counts.get(k, 0)} != recorded {recorded.get(k, 0)}"
             for k in sorted(set(counts) | set(recorded)) if counts.get(k, 0) != recorded.get(k, 0)]
    R.check("G1 resolver figures equal G1_EXPECTED_COUNTS in tests/test_module_seams.py", probs)


def check_g1_baseline(R, baseline, tree_root):
    """The resolver on the baseline test tree must agree with the counting script's independent regex totals."""
    seam = load_seam_ast(tree_root)
    sources = baseline_test_sources(baseline)
    counts = seam.g1_counts(sources, exclude=())
    _targets, unresolved = seam.collect_targets(seam.Project(sources))
    print_g1_figures(f"baseline {baseline}", counts)
    with tempfile.TemporaryDirectory() as tmp:
        export_revision(baseline, Path(tmp))
        proc = subprocess.run([sys.executable, str(ROOT / COUNTS_SCRIPT)], cwd=tmp, capture_output=True, text=True)
    if proc.returncode != 0:
        R.fail("G1 baseline cross-check: the counting script ran", proc.stderr[-300:] or proc.stdout[-300:])
        return
    regex = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^(.*): (\d+)$", line.strip())
        if m:
            regex[m.group(1)] = int(m.group(2))
        m = re.match(r"^setattr/patch.object\(AS, '<literal>'\) calls: (\d+);", line.strip())
        if m:
            regex["setattr/patch.object(AS, '<literal>') calls"] = int(m.group(1))
    pairs = [
        ("literal 'engine.render.asset_sourcer.<x>' strings", ("asset_sourcer", "literal")),
        ("f'{_MOD}.<x>' targets", ("asset_sourcer", "f{_MOD}")),
        ("f'{_LOG}.<x>' targets", ("asset_sourcer", "f{_LOG}")),
        ("f'{AS.__name__}.<x>' targets", ("asset_sourcer", "f{AS.__name__}")),
        ("setattr/patch.object(AS, '<literal>') calls", ("asset_sourcer", "object:literal")),
    ]
    probs = [str(p) for p in unresolved]
    for key, form in pairs:
        if key not in regex:
            probs.append(f"the counting script printed no line for {key!r}")
        elif regex[key] != counts.get(form, 0):
            probs.append(f"{key}: regex {regex[key]} != resolver {counts.get(form, 0)} ({form[1]})")
    other = sorted(set(counts) - {f for _k, f in pairs} - {("asset_sourcer", "object:name")})
    if other:
        probs.append(f"the resolver found forms the counting script does not know: {other}")
    R.check("G1 resolver on the baseline tree equals the counting script's regex totals", probs,
            "; ".join(f"{k.split(' ')[0]}={regex.get(k)}" for k, _f in pairs))


# --------------------------------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", default="d096524")
    ap.add_argument("--stage", type=int, choices=(0, 1, 2, 3), default=0)
    ap.add_argument("--no-collect", action="store_true")
    ap.add_argument("--cross-check-baseline", action="store_true")
    ap.add_argument("--tree", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    R = Report()

    base_asset = ast.parse(git_show(args.baseline, ASSET_PATH))
    oracle_checks(R, base_asset)
    if args.stage == 0:
        for name in ("A", "B", "DEP", "C", "G1 resolver"):
            R.skip(name, "stage 0: oracle self-checks only")
        return 1 if R.fails else 0

    tree_root = args.tree.resolve()
    trees = {}
    for owner in OWNERS:
        path = tree_root / OWNER_PATH[owner]
        if path.exists():
            trees[owner] = ast.parse(path.read_text(encoding="utf-8"))
        else:
            R.fail(f"A {owner}: module exists", f"{OWNER_PATH[owner]} not found under {tree_root}")
    asset_path = tree_root / ASSET_PATH
    asset_tree = ast.parse(asset_path.read_text(encoding="utf-8")) if asset_path.exists() else None
    if asset_tree is None:
        R.fail(f"B {ASSET_PATH}: exists", f"not found under {tree_root}")

    if len(trees) == len(OWNERS):
        oracle_owner_checks(R, trees)
        check_a(R, base_asset, trees, args.stage)
    if asset_tree is not None:
        check_b_asset(R, base_asset, asset_tree, args.stage)
    if args.stage >= 2:
        base_render = ast.parse(git_show(args.baseline, RENDER_PATH))
        render_path = tree_root / RENDER_PATH
        if render_path.exists():
            check_b_render(R, base_render, ast.parse(render_path.read_text(encoding="utf-8")))
        else:
            R.fail(f"B {RENDER_PATH}: exists", f"not found under {tree_root}")
    if len(trees) == len(OWNERS) and asset_tree is not None:
        dependency_checks(R, base_asset, trees, asset_tree, tree_root)

    if args.stage >= 2:
        check_c(R, args.baseline, tree_root, args.stage, not args.no_collect)
    if args.stage >= 3:
        check_g1_resolver(R, tree_root)
        if args.cross_check_baseline:
            check_g1_baseline(R, args.baseline, tree_root)
        else:
            R.skip("G1 baseline cross-check", "pass --cross-check-baseline")
    return 1 if R.fails else 0


if __name__ == "__main__":
    sys.exit(main())
