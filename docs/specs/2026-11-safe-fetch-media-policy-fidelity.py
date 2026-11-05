#!/usr/bin/env python3
"""Fidelity proof for the safe_fetch / media_policy split (spec 2026-11-safe-fetch-media-policy-module-design.md, section 6).

Run from the repo root (it inserts the repo root into sys.path itself):

    python docs/specs/2026-11-safe-fetch-media-policy-fidelity.py [--baseline REV] [--stage 0|1|2|3] [--tree DIR]

    --baseline REV  the revision the move started from (default d096524 = main before the split). Baseline files are
                    read with `git show REV:path`, never from the working tree.
    --stage N       0 = oracle self-checks only (nothing but the baseline and tests/_seam_oracle.py is needed);
                    1 = after commit 1 (modules exist, bridge allowed and required, loggers use the literal old name);
                    2 = after commit 2 (bridge gone, loggers use __name__, worker/tasks/render.py is compared too);
                    3 = after commit 3 (same as 2; the G1 resolver mode is still to be added).
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
  C, G1    not implemented in T1: printed as SKIP.

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


# --------------------------------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", default="d096524")
    ap.add_argument("--stage", type=int, choices=(0, 1, 2, 3), default=0)
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
        # TODO(T3): check C (test-AST equality, node ids, assert/raises/approx counts) is added by the commit-2 task.
        R.skip("C", "not implemented in T1")
    if args.stage >= 3:
        # TODO(T4): the G1 resolver mode (per-form counts of resolved patch targets) is added by the commit-3 task.
        R.skip("G1 resolver", "not implemented in T1")
    return 1 if R.fails else 0


if __name__ == "__main__":
    sys.exit(main())
