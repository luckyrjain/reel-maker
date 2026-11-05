"""AST checkers behind the module-seam guards of tests/test_module_seams.py (spec 4.5, G1-G6).

Standard library plus tests/_seam_oracle.py only, so that the fidelity script
(docs/specs/2026-11-safe-fetch-media-policy-fidelity.py, stage 3 resolver mode) can load this file by path with a
bare `python3`. Nothing here imports the project. Every checker takes SOURCE TEXT (and a path used only for
messages and for resolving relative imports), so the guard tests run them on the real tree and, with the very same
functions, on small synthetic snippets.

The pieces
  parse / annotate        parse a source and give every node `_parent` and `_field` (the field it hangs off).
  import_bindings / dotted  a name -> set of dotted paths map built from every import (any scope) and simple
                          aliases, and the resolution of a Name / Attribute chain to dotted paths.
  Project / eval_str      string constants, including ones imported from another test file, f-strings, `X.__name__`
                          of a module, `@pytest.mark.parametrize` literals and `for x in (...)` literals.
  collect_targets         every patch position in a test tree (patch, patch.object, monkeypatch.setattr/delattr,
                          setattr/delattr/getattr) as a `Target`; unresolvable ones become problems.
  g1_*, g2_*              the test-tree guards.   g3_*, g4_*, g5_*, g6_*   the production-tree guards.
"""
from __future__ import annotations

import ast
import collections
import dataclasses
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_oracle():
    path = Path(__file__).with_name("_seam_oracle.py")
    spec = importlib.util.spec_from_file_location("_seam_oracle_for_guards", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


O = _load_oracle()

MODULE_PATHS = {
    "asset_sourcer": "engine.render.asset_sourcer",
    "safe_fetch": "engine.render.safe_fetch",
    "media_policy": "engine.render.media_policy",
}
PATH_OWNER = {path: owner for owner, path in MODULE_PATHS.items()}
POLICY = ("safe_fetch", "media_policy")
STALE_NAMES = frozenset(O.MOVED) | frozenset(O.BRIDGE_EXTRAS)

# (owner, new name) -> kind, for every moved name and for the new ones (`now`)
KIND = {(owner, new): kind for owner, new, kind in O.MOVED.values()}
KIND.update({(owner, name): kind for name, (owner, kind) in O.NEW.items()})
PUBLIC = {owner: sorted(new for (o, new) in KIND if o == owner and not new.startswith("_")) for owner in POLICY}

# G1: an unresolvable name in a patch position is a failure, except these (file, name) entries, each justified.
UNRESOLVED_ALLOWLIST = {
    ("tests/test_cuts_publish_router.py", "patch_target"):
        "a helper parameter; its two call sites pass router task paths (api.routers.cuts.render_cut / publish_cut), "
        "never a sourcer name",
}
G1_EXCLUDED = ("tests/test_module_seams.py", "tests/_seam_oracle.py")

REFLECTIVE = frozenset({"getattr", "setattr", "delattr", "hasattr", "vars", "dir"})
TRANSPORT_MODULES = ("httpx", "socket", "ssl", "http.client", "requests", "urllib.request")


@dataclasses.dataclass(frozen=True)
class Problem:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self):
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


@dataclasses.dataclass(frozen=True)
class Target:
    path: str
    line: int
    api: str                     # patch | patch.object | patch.multiple | monkeypatch.setattr | monkeypatch.delattr | builtin.*
    kind: str                    # "string" (a dotted target) | "object" (a module object and an attribute name)
    form: str                    # literal | f{...} | name | concat | attr | object:literal | object:name
    owner: "str | None"          # asset_sourcer | safe_fetch | media_policy | None
    target: str                  # the resolved dotted string (string kind) or the module path (object kind)
    attr: "str | None"           # object kind: the attribute name
    raising: "ast.expr | None" = None
    create: "ast.expr | None" = None
    extra_positional: bool = False


# --------------------------------------------------------------------------------------------------- parsing
def annotate(tree):
    tree._parent, tree._field = None, None
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            if isinstance(value, ast.AST):
                value._parent, value._field = parent, field
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        item._parent, item._field = parent, field
    return tree


def parse(source, path="<snippet>"):
    return annotate(ast.parse(source, filename=path))


def module_name(path):
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def package_of(path):
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        return ".".join(parts[:-1])
    return ".".join(parts[:-1])


def _from_base(node, package):
    """The dotted module an ImportFrom reads from (relative imports resolved against `package`)."""
    if node.level == 0:
        return node.module or ""
    parts = package.split(".") if package else []
    keep = len(parts) - (node.level - 1)
    if keep < 0:
        return None
    base = ".".join(parts[:keep])
    if node.module:
        base = f"{base}.{node.module}" if base else node.module
    return base


def import_bindings(tree, path="<snippet>"):
    """name -> set of dotted paths, from every import in the tree (any scope) and then simple `x = <dotted>` aliases."""
    bindings = collections.defaultdict(set)
    package = package_of(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname].add(alias.name)
                else:
                    head = alias.name.split(".")[0]
                    bindings[head].add(head)
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, package)
            if base is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                bindings[alias.asname or alias.name].add(f"{base}.{alias.name}" if base else alias.name)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
                paths = dotted(node.value, bindings)
                name = node.targets[0].id
                if paths and not paths <= bindings[name]:
                    bindings[name] |= paths
                    changed = True
    return bindings


def dotted(node, bindings):
    """The set of dotted paths a Name / Attribute chain can stand for (empty when it is not a bound chain)."""
    if isinstance(node, ast.Name):
        return set(bindings.get(node.id, ()))
    if isinstance(node, ast.Attribute):
        return {f"{p}.{node.attr}" for p in dotted(node.value, bindings)}
    return set()


def owner_of_path(path):
    """The module (asset_sourcer / safe_fetch / media_policy) a dotted string lives under, else None."""
    for module_path, owner in PATH_OWNER.items():
        if path == module_path or path.startswith(module_path + "."):
            return owner
    return None


def module_owner(node, bindings, only=None):
    """The owner module if `node` IS one of the three modules (a bound name or a dotted chain), else None."""
    for p in dotted(node, bindings):
        owner = PATH_OWNER.get(p)
        if owner is not None and (only is None or owner in only):
            return owner
    return None


# --------------------------------------------------------------------------------------------------- string constants
class FileCtx:
    def __init__(self, path, source):
        self.path = path
        self.tree = parse(source, path)
        self.bindings = import_bindings(self.tree, path)
        self.assigns = collections.defaultdict(list)
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self.assigns[node.targets[0].id].append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                self.assigns[node.target.id].append(node.value)


class Project:
    """A set of source files keyed by repo-relative path; cross-file names resolve through `module_name`."""

    def __init__(self, sources):
        self.files = {path: FileCtx(path, text) for path, text in sources.items()}
        self.by_module = {module_name(path): ctx for path, ctx in self.files.items()}


def _dedupe(values):
    seen, out = set(), []
    for v in values:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def eval_str(node, ctx, project, scope=(), depth=0):
    """The possible string values of `node`, or None when it cannot be resolved statically."""
    if depth > 12:
        return None
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        results = [""]
        for part in node.values:
            if isinstance(part, ast.Constant):
                pieces = [part.value]
            elif isinstance(part, ast.FormattedValue):
                if part.format_spec is not None:
                    return None
                pieces = eval_str(part.value, ctx, project, scope, depth + 1)
                if pieces is None:
                    return None
            else:
                return None
            results = [r + p for r in results for p in pieces]
        return _dedupe(results)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = eval_str(node.left, ctx, project, scope, depth + 1)
        right = eval_str(node.right, ctx, project, scope, depth + 1)
        if left is None or right is None:
            return None
        return _dedupe([a + b for a in left for b in right])
    if isinstance(node, ast.Name):
        for env in reversed(list(scope)):
            if node.id in env:
                return env[node.id]
        values = ctx.assigns.get(node.id)
        if values:
            out = []
            for value in values:
                r = eval_str(value, ctx, project, (), depth + 1)
                if r is None:
                    return None
                out += r
            return _dedupe(out)
        for p in sorted(ctx.bindings.get(node.id, ())):
            r = _eval_imported(p, project, depth)
            if r is not None:
                return r
        return None
    if isinstance(node, ast.Attribute):
        if node.attr == "__name__":
            owners = [p for p in dotted(node.value, ctx.bindings) if p in PATH_OWNER]
            return sorted(owners) or None
        for p in sorted(dotted(node, ctx.bindings)):
            r = _eval_imported(p, project, depth)
            if r is not None:
                return r
        return None
    return None


def _eval_imported(path, project, depth):
    module, _, name = path.rpartition(".")
    other = project.by_module.get(module)
    if other is None:
        return None
    return eval_str(ast.Name(id=name, ctx=ast.Load()), other, project, (), depth + 1)


def _argnames(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [n.strip() for n in node.value.split(",") if n.strip()]
    if isinstance(node, (ast.Tuple, ast.List)) and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                                                       for e in node.elts):
        return [e.value for e in node.elts]
    return None


def _is_param_call(node):
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Attribute) and node.func.attr == "param")
        or (isinstance(node.func, ast.Name) and node.func.id == "param"))


def parametrize_env(func, ctx, project, scope):
    """name -> list of string values (or None) for the `@pytest.mark.parametrize` decorators of `func`."""
    env = {}
    for deco in func.decorator_list:
        if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "parametrize" and len(deco.args) >= 2):
            continue
        names = _argnames(deco.args[0])
        if names is None:
            continue
        values = deco.args[1]
        if not isinstance(values, (ast.List, ast.Tuple)):
            env.update({n: None for n in names})
            continue
        columns = {n: [] for n in names}
        bad = set()
        for elt in values.elts:
            if _is_param_call(elt):
                elt = elt.args[0] if len(names) == 1 and elt.args else ast.Tuple(elts=list(elt.args), ctx=ast.Load())
            if len(names) == 1:
                items = [elt]
            elif isinstance(elt, (ast.Tuple, ast.List)) and len(elt.elts) == len(names):
                items = elt.elts
            else:
                bad.update(names)
                continue
            for name, item in zip(names, items):
                r = eval_str(item, ctx, project, scope)
                if r is None:
                    bad.add(name)
                else:
                    columns[name] += r
        for name in names:
            env[name] = None if name in bad else _dedupe(columns[name])
    return env


# --------------------------------------------------------------------------------------------------- patch positions
def _kw(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _form(node):
    if isinstance(node, ast.Constant):
        return "literal"
    if isinstance(node, ast.JoinedStr):
        return "f{" + ",".join(ast.unparse(p.value) for p in node.values if isinstance(p, ast.FormattedValue)) + "}"
    if isinstance(node, ast.Name):
        return "name"
    if isinstance(node, ast.BinOp):
        return "concat"
    return "attr"


def _call_kind(call, ctx):
    f = call.func
    paths = dotted(f, ctx.bindings)
    if "unittest.mock.patch" in paths:
        return "patch"
    if "unittest.mock.patch.object" in paths:
        return "patch.object"
    if "unittest.mock.patch.multiple" in paths:
        return "patch.multiple"
    if isinstance(f, ast.Attribute) and f.attr in ("setattr", "delattr"):
        return f"monkeypatch.{f.attr}"
    if isinstance(f, ast.Name) and f.id in ("setattr", "delattr", "getattr") and f.id not in ctx.bindings:
        return f"builtin.{f.id}"
    return None


_MULTIPLE_OPTIONS = ("spec", "create", "spec_set", "autospec", "new_callable")
# positional arguments up to and including the last one before raising / create can be given positionally
_STRING_POSITIONAL = {"patch": 3, "patch.multiple": 1, "monkeypatch.setattr": 2, "monkeypatch.delattr": 1}
# patch.object(obj, name, new, spec, create) / monkeypatch.setattr(obj, name, value, raising) / .delattr(obj, name, raising)
_OBJECT_POSITIONAL = {"patch.object": 4, "patch.multiple": 1, "monkeypatch.setattr": 3, "monkeypatch.delattr": 2}


class _Collector(ast.NodeVisitor):
    def __init__(self, ctx, project):
        self.ctx, self.project = ctx, project
        self.scope = [{}]
        self.targets: list[Target] = []
        self.problems: list[Problem] = []

    # -- scopes
    def visit_FunctionDef(self, node):
        for deco in node.decorator_list:
            self.visit(deco)
        self.visit(node.args)
        env = parametrize_env(node, self.ctx, self.project, self.scope)
        self.scope.append(env)
        for stmt in node.body:
            self.visit(stmt)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_For(self, node):
        self.visit(node.iter)
        env = {}
        targets = node.target.elts if isinstance(node.target, ast.Tuple) else [node.target]
        names = [t.id if isinstance(t, ast.Name) else None for t in targets]
        for name in names:
            if name:
                env[name] = None
        if isinstance(node.iter, (ast.Tuple, ast.List, ast.Set)):
            columns = {n: [] for n in names if n}
            ok = {n: True for n in names if n}
            for elt in node.iter.elts:
                items = elt.elts if len(names) > 1 and isinstance(elt, (ast.Tuple, ast.List)) else [elt]
                if len(items) != len(names):
                    ok = {n: False for n in ok}
                    break
                for name, item in zip(names, items):
                    if not name:
                        continue
                    r = eval_str(item, self.ctx, self.project, self.scope)
                    if r is None:
                        ok[name] = False
                    else:
                        columns[name] += r
            for name in columns:
                env[name] = _dedupe(columns[name]) if ok[name] else None
        self.scope.append(env)
        for stmt in node.body + node.orelse:
            self.visit(stmt)
        self.scope.pop()

    # -- the patch positions
    def visit_Call(self, node):
        kind = _call_kind(node, self.ctx)
        if kind:
            self._handle(node, kind)
        self.generic_visit(node)

    def _ev(self, node):
        return eval_str(node, self.ctx, self.project, self.scope)

    def _handle(self, call, api):
        path, line = self.ctx.path, call.lineno
        b = self.ctx.bindings
        args = call.args
        if api == "patch.multiple" and args and module_owner(args[0], b) is not None:
            owner = module_owner(args[0], b)
            for kw in call.keywords:
                if kw.arg and kw.arg not in _MULTIPLE_OPTIONS:
                    self.targets.append(Target(path, line, api, "object", "object:literal", owner, MODULE_PATHS[owner],
                                               kw.arg, _kw(call, "raising"), _kw(call, "create"),
                                               extra_positional=len(args) > 1))
            return
        if api in ("patch", "patch.multiple") or (api.startswith("monkeypatch.") and args
                                                    and module_owner(args[0], b) is None
                                                    and self._looks_like_string_target(args[0])):
            target = args[0] if args else _kw(call, "target")
            if target is None:
                return
            values = self._ev(target)
            if values is None:
                if isinstance(target, ast.Name) and (path, target.id) in UNRESOLVED_ALLOWLIST:
                    return
                self.problems.append(Problem(path, line, "unresolved", f"cannot resolve the {api} target "
                                             f"`{ast.unparse(target)}`: use a literal, a constant or a parametrize literal"))
                return
            extra = len(args) > _STRING_POSITIONAL.get(api, 99)
            for value in values:
                if api == "patch.multiple":
                    names = [kw.arg for kw in call.keywords if kw.arg and kw.arg not in _MULTIPLE_OPTIONS]
                    for name in names or [None]:
                        self._add_string(call, api, target, value if name is None else f"{value}.{name}", extra=extra)
                else:
                    self._add_string(call, api, target, value, extra=extra)
            return
        # object forms: patch.object(obj, name) / setattr(obj, name, ...) / getattr(obj, name)
        obj = args[0] if args else _kw(call, "target")
        name_node = args[1] if len(args) > 1 else (_kw(call, "name") or _kw(call, "attribute"))
        if obj is None or name_node is None:
            return
        owner = module_owner(obj, b)
        if owner is None:
            return
        names = self._ev(name_node)
        if names is None:
            self.problems.append(Problem(path, line, "unresolved", f"cannot resolve the attribute name "
                                         f"`{ast.unparse(name_node)}` patched on {MODULE_PATHS[owner]}"))
            return
        for name in names:
            self.targets.append(Target(
                path, line, api, "object", "object:literal" if isinstance(name_node, ast.Constant) else "object:name",
                owner, MODULE_PATHS[owner], name, _kw(call, "raising"), _kw(call, "create"),
                extra_positional=len(args) > _OBJECT_POSITIONAL.get(api, 99)))

    def _looks_like_string_target(self, node):
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            return True
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._ev(node) is not None
        return False

    def _add_string(self, call, api, node, value, extra=False):
        self.targets.append(Target(
            self.ctx.path, call.lineno, api, "string", _form(node), owner_of_path(value), value, None,
            _kw(call, "raising"), _kw(call, "create"), extra_positional=extra))


def collect_targets(project, only=None):
    """Every patch position in the project's files: (targets, unresolved-problems)."""
    targets, problems = [], []
    for path, ctx in sorted(project.files.items()):
        if only is not None and path not in only:
            continue
        collector = _Collector(ctx, project)
        collector.visit(ctx.tree)
        targets += collector.targets
        problems += collector.problems
    return targets, problems


def project_from(sources, exclude=()):
    return Project({p: s for p, s in sources.items() if p not in exclude})


# --------------------------------------------------------------------------------------------------- G1
def g1_problems(sources, exclude=G1_EXCLUDED):
    """Stale or moved-name access through asset_sourcer, and patching `safe_fetch.now`, in the given test sources.

    `exclude` files are not scanned but stay available for cross-file constants (they are in the project)."""
    project = Project(sources)
    scanned = {p for p in sources if p not in exclude}
    targets, problems = collect_targets(project, only=scanned)
    asset = MODULE_PATHS["asset_sourcer"]
    for t in targets:
        if t.kind == "string":
            if t.target.startswith(asset + "."):
                first = t.target[len(asset) + 1:].split(".")[0]
                if first in STALE_NAMES:
                    problems.append(Problem(t.path, t.line, "G1", f"patch target {t.target!r}: `{first}` no longer lives "
                                            "on asset_sourcer"))
            if t.target == MODULE_PATHS["safe_fetch"] + ".now" or t.target.startswith(MODULE_PATHS["safe_fetch"] + ".now."):
                problems.append(Problem(t.path, t.line, "G1", f"patch target {t.target!r}: patch safe_fetch._monotonic, "
                                        "never safe_fetch.now (D2)"))
        else:
            if t.owner == "asset_sourcer" and t.attr in STALE_NAMES:
                problems.append(Problem(t.path, t.line, "G1", f"{t.api} of asset_sourcer.{t.attr}: it no longer lives there"))
            if t.owner == "safe_fetch" and t.attr == "now":
                problems.append(Problem(t.path, t.line, "G1", f"{t.api} of safe_fetch.now: patch safe_fetch._monotonic (D2)"))
    for path in sorted(scanned):
        ctx = project.files[path]
        for node in ast.walk(ctx.tree):
            if isinstance(node, ast.Attribute) and node.attr in STALE_NAMES:
                if any(p == asset for p in dotted(node.value, ctx.bindings)):
                    problems.append(Problem(path, node.lineno, "G1", f"`{ast.unparse(node)}`: a moved name read, written "
                                            "or deleted through asset_sourcer"))
            elif isinstance(node, ast.ImportFrom):
                if _from_base(node, package_of(path)) == asset:
                    for alias in node.names:
                        if alias.name in STALE_NAMES:
                            problems.append(Problem(path, node.lineno, "G1", f"`from {asset} import {alias.name}`: a moved "
                                                    "name"))
    return problems


def g1_counts(sources, exclude=G1_EXCLUDED):
    """{(owner, form): n} over the resolved targets that live under asset_sourcer / safe_fetch / media_policy."""
    project = Project(sources)
    scanned = {p for p in sources if p not in exclude}
    targets, _ = collect_targets(project, only=scanned)
    counts = collections.Counter()
    for t in targets:
        if t.owner:
            counts[(t.owner, t.form)] += 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------------------------------- G2
def _walk_attrs(obj, parts):
    for part in parts:
        if not hasattr(obj, part):
            return False
        obj = getattr(obj, part)
    return True


def g2_problems(sources, importer, exclude=()):
    """No raising=False / create=True against the three modules, and every target exists (hasattr)."""
    project = Project(sources)
    targets, _ = collect_targets(project, only={p for p in sources if p not in exclude})
    problems = []
    for t in targets:
        if not t.owner:
            continue
        if t.raising is not None and not (isinstance(t.raising, ast.Constant) and t.raising.value is True):
            problems.append(Problem(t.path, t.line, "G2", f"{t.api} of {t.target}{'.' + t.attr if t.attr else ''} passes "
                                    f"raising={ast.unparse(t.raising)}: patches must be strict"))
        if t.create is not None and not (isinstance(t.create, ast.Constant) and t.create.value is False):
            problems.append(Problem(t.path, t.line, "G2", f"{t.api} of {t.target}{'.' + t.attr if t.attr else ''} passes "
                                    f"create={ast.unparse(t.create)}: patches must be strict"))
        if t.extra_positional:
            problems.append(Problem(t.path, t.line, "G2", f"{t.api} passes raising/create positionally: use keywords"))
        module_path = MODULE_PATHS[t.owner]
        module = importer(module_path)
        if t.kind == "string":
            rest = t.target[len(module_path) + 1:].split(".") if t.target != module_path else []
            if not _walk_attrs(module, rest):
                problems.append(Problem(t.path, t.line, "G2", f"patch target {t.target!r} does not resolve (hasattr)"))
        elif not hasattr(module, t.attr):
            problems.append(Problem(t.path, t.line, "G2", f"{module_path} has no attribute {t.attr!r}"))
    return problems


# --------------------------------------------------------------------------------------------------- G3
@dataclasses.dataclass
class G3Result:
    problems: list
    constant_reads: list      # (path, line, name) of every read of a `constant` seam
    accesses: int             # attribute accesses on the two policy modules


def _in_function_body(node):
    cur = node
    while getattr(cur, "_parent", None) is not None:
        parent, field = cur._parent, cur._field
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return field == "body"
        if isinstance(parent, (ast.ClassDef, ast.Module)):
            return False
        cur = parent
    return False


def _is_partial(call):
    f = call.func
    return (isinstance(f, ast.Name) and f.id == "partial") or (isinstance(f, ast.Attribute) and f.attr == "partial")


def _callable_position_ok(acc):
    parent = acc._parent
    if isinstance(parent, ast.Call) and parent.func is acc:
        return True
    if isinstance(parent, ast.ExceptHandler) and parent.type is acc:
        return True
    return (isinstance(parent, ast.Tuple) and isinstance(parent._parent, ast.ExceptHandler)
            and parent._parent.type is parent)


def _constant_position_ok(acc):
    if not _in_function_body(acc):
        return False
    parent = acc._parent
    call = None
    if isinstance(parent, ast.keyword):
        call = parent._parent
    elif isinstance(parent, ast.Call) and parent.func is not acc:
        call = parent
    if call is not None:
        return not _is_partial(call)
    return isinstance(parent, (ast.BinOp, ast.Compare, ast.UnaryOp, ast.BoolOp))


def g3_check(source, path="<snippet>", oracle_kinds=None):
    """Seam-capture guard for one production file (no from-imports, no aliases, call-time constant reads only)."""
    kinds = KIND if oracle_kinds is None else oracle_kinds
    tree = parse(source, path)
    b = import_bindings(tree, path)
    package = package_of(path)
    problems, reads, accesses = [], [], 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = _from_base(node, package)
            if PATH_OWNER.get(base) in POLICY:
                names = ", ".join(a.name for a in node.names)
                problems.append(Problem(path, node.lineno, "G3", f"`from {base} import {names}`: import the module and use "
                                        "module-attribute access (D3)"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(getattr(node, "ctx", None), ast.Load):
            continue
        owner = module_owner(node, b, only=POLICY)
        if owner is None:
            continue
        parent = node._parent
        if not (isinstance(parent, ast.Attribute) and parent.value is node):
            reflective = (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                          and parent.func.id in REFLECTIVE)
            problems.append(Problem(path, node.lineno, "G3", (
                f"reflective access to {MODULE_PATHS[owner]}" if reflective else
                f"the module object {MODULE_PATHS[owner]} is used as a value (alias, argument, container, partial)")))
            continue
        acc, name = parent, parent.attr
        accesses += 1
        if name.startswith("_"):
            problems.append(Problem(path, acc.lineno, "G3", f"`{owner}.{name}`: production code uses only the public names"))
            continue
        kind = kinds.get((owner, name))
        if kind is None:
            problems.append(Problem(path, acc.lineno, "G3", f"`{owner}.{name}` is not a public name of {owner} in the oracle"))
            continue
        if not isinstance(acc.ctx, ast.Load):
            problems.append(Problem(path, acc.lineno, "G3", f"`{owner}.{name}` is written or deleted"))
            continue
        if kind == "constant":
            reads.append((path, acc.lineno, f"{owner}.{name}"))
            if not _constant_position_ok(acc):
                problems.append(Problem(path, acc.lineno, "G3", f"`{owner}.{name}` is a constant seam: read it only inside a "
                                        "function body, as a call argument or an operand"))
        elif not _callable_position_ok(acc):
            problems.append(Problem(path, acc.lineno, "G3", f"`{owner}.{name}` is a callable seam: it may only be called "
                                    "or named in an `except` clause"))
    return G3Result(problems, reads, accesses)


def production_sources(root=ROOT, dirs=("api", "engine", "worker")):
    out = {}
    for d in dirs:
        for p in sorted((Path(root) / d).rglob("*.py")):
            out[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8")
    return out


def g3_tree(sources, oracle_kinds=None):
    result = G3Result([], [], 0)
    for path, text in sorted(sources.items()):
        r = g3_check(text, path, oracle_kinds)
        result.problems += r.problems
        result.constant_reads += r.constant_reads
        result.accesses += r.accesses
    return result


# --------------------------------------------------------------------------------------------------- G4
_G4_NAMES = ("_asset_deadline_at", "_monotonic")


def _binding_sites(tree, names):
    sites = collections.Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in names:
            sites[node.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            sites[node.name] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) in names or alias.name in names:
                    sites[alias.asname or alias.name] += 1
        elif isinstance(node, ast.arg) and node.arg in names:
            sites[node.arg] += 1
    return sites


def g4_problems(safe_fetch_source, others, names=_G4_NAMES):
    """safe_fetch binds each clock name exactly once; no other module binds (or imports) either."""
    problems = []
    sites = _binding_sites(parse(safe_fetch_source, "engine/render/safe_fetch.py"), names)
    for name in names:
        if sites[name] != 1:
            problems.append(Problem("engine/render/safe_fetch.py", 0, "G4", f"`{name}` is bound {sites[name]} times, expected 1"))
    for path, text in sorted(others.items()):
        for name, n in _binding_sites(parse(text, path), names).items():
            problems.append(Problem(path, 0, "G4", f"`{name}` is bound {n} time(s) outside safe_fetch: one clock, one ContextVar"))
    return problems


# --------------------------------------------------------------------------------------------------- G5
def g5_import_problems(source, path, forbidden):
    """Imports (any scope, relative resolved) of anything under the `forbidden` module paths."""
    tree = parse(source, path)
    package = package_of(path)
    problems = []
    for node in ast.walk(tree):
        found = []
        if isinstance(node, ast.Import):
            found = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, package)
            if base is not None:
                found = [base] + [f"{base}.{a.name}" for a in node.names if a.name != "*"]
        for name in found:
            if any(name == f or name.startswith(f + ".") for f in forbidden):
                problems.append(Problem(path, node.lineno, "G5", f"import of {name}: the dependency direction is "
                                        "asset_sourcer -> {safe_fetch, media_policy}, never the reverse or sideways"))
    return problems


def g5_bridge_problems(source, path="engine/render/asset_sourcer.py"):
    """No module-level __getattr__/__setattr__/__delattr__, no __class__ assignment, no sys.modules rebinding."""
    tree = parse(source, path)
    problems = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ("__getattr__", "__setattr__", "__delattr__"):
            problems.append(Problem(path, node.lineno, "G5", f"module-level {node.name}: the bridge must be gone"))
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if ast.unparse(base) in ("ModuleType", "types.ModuleType"):
                    problems.append(Problem(path, node.lineno, "G5", "a ModuleType subclass: the bridge must be gone"))
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for t in targets:
            for sub in ast.walk(t):
                if (isinstance(sub, ast.Attribute) and sub.attr == "__class__") or (isinstance(sub, ast.Name) and sub.id == "__class__"):
                    problems.append(Problem(path, node.lineno, "G5", "assignment to __class__: the bridge must be gone"))
                if isinstance(sub, ast.Subscript) and ast.unparse(sub.value) == "sys.modules":
                    problems.append(Problem(path, node.lineno, "G5", "assignment into sys.modules: the bridge must be gone"))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr"
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "__class__"):
            problems.append(Problem(path, node.lineno, "G5", "setattr(..., '__class__', ...): the bridge must be gone"))
    return problems


# --------------------------------------------------------------------------------------------------- G6
def g6a_problems(source, path="engine/render/asset_sourcer.py"):
    """asset_sourcer imports no transport library."""
    tree = parse(source, path)
    package = package_of(path)
    problems = []
    for node in ast.walk(tree):
        found = []
        if isinstance(node, ast.Import):
            found = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, package)
            if base is not None:
                found = [base] + [f"{base}.{a.name}" for a in node.names if a.name != "*"]
        for name in found:
            if any(name == m or name.startswith(m + ".") for m in TRANSPORT_MODULES):
                problems.append(Problem(path, node.lineno, "G6", f"import of {name}: asset_sourcer has no transport "
                                        "(it goes through safe_fetch)"))
    return problems


ALLOWED_URL_OK = ("_pexels_url_ok", "_wikimedia_url_ok")


def _safe_fetch_calls(tree, bindings, attr_names):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in attr_names:
            if module_owner(node.func.value, bindings, only=("safe_fetch",)):
                yield node


def g6b_problems(source, path):
    """Every safe_fetch.open_download call passes url_ok as exactly one of the two adapter predicates."""
    tree = parse(source, path)
    b = import_bindings(tree, path)
    calls = list(_safe_fetch_calls(tree, b, ("open_download",)))
    problems = []
    is_asset = path.endswith("engine/render/asset_sourcer.py")
    for call in calls:
        url_ok = call.args[1] if len(call.args) >= 2 else _kw(call, "url_ok")
        if not (isinstance(url_ok, ast.Name) and url_ok.id in ALLOWED_URL_OK):
            shown = ast.unparse(url_ok) if url_ok is not None else "<missing>"
            problems.append(Problem(path, call.lineno, "G6", f"open_download url_ok is `{shown}`, expected exactly "
                                    f"{' or '.join(ALLOWED_URL_OK)}"))
        elif not is_asset:
            problems.append(Problem(path, call.lineno, "G6", "open_download is called outside asset_sourcer: the host "
                                    "allowlists live with the adapters"))
    if calls and is_asset:
        defined = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in ALLOWED_URL_OK:
            fn = defined.get(name)
            if fn is None:
                problems.append(Problem(path, 0, "G6", f"{name} is not a module-level function of asset_sourcer"))
            elif not any(True for _ in _safe_fetch_calls(fn, b, ("https_host",))):
                problems.append(Problem(path, fn.lineno, "G6", f"{name} does not call safe_fetch.https_host"))
    return problems


def _class_of(node):
    cur = node
    while getattr(cur, "_parent", None) is not None:
        cur = cur._parent
        if isinstance(cur, ast.ClassDef):
            return cur
    return None


def _str_assigns(body):
    return {t.targets[0].id for t in body if isinstance(t, ast.Assign) and len(t.targets) == 1
            and isinstance(t.targets[0], ast.Name) and isinstance(t.value, ast.Constant) and isinstance(t.value.value, str)}


def _constant_url_part(node, tree, call):
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.Name):
        return node.id in _str_assigns(tree.body)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        cls = _class_of(call)
        if cls is not None and node.value.id in ("self", "cls", cls.name):
            return node.attr in _str_assigns(cls.body)
        return False
    return False


def g6c_problems(source, path):
    """Every safe_fetch.api_get / api_post call's URL is rooted in a literal or a class/module string constant."""
    tree = parse(source, path)
    b = import_bindings(tree, path)
    problems = []
    for call in _safe_fetch_calls(tree, b, ("api_get", "api_post")):
        url = call.args[0] if call.args else _kw(call, "url")
        if url is None:
            ok = False
        elif isinstance(url, ast.JoinedStr):
            first = url.values[0] if url.values else None
            ok = (isinstance(first, ast.Constant) and bool(first.value)) or (
                isinstance(first, ast.FormattedValue) and first.format_spec is None
                and _constant_url_part(first.value, tree, call))
        else:
            ok = _constant_url_part(url, tree, call)
        if not ok:
            shown = ast.unparse(url) if url is not None else "<missing>"
            problems.append(Problem(path, call.lineno, "G6", f"{call.func.attr} URL `{shown}` is not rooted in a literal or a "
                                    "class/module string constant (the default url_ok is not an allowlist)"))
    return problems
