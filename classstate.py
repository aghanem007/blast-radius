"""What does a class-level edit change once sqlglot has been imported?

A statement in a class body runs once, at import. Its effect on any test goes through
the state it leaves behind: the class attribute it assigns, and whatever other class
attributes import-time code derives from it (a subclass copying a table, a metaclass
building a trie or an inverse mapping from it).

This module imports every sqlglot module, with the patched source for the changed
files, and fingerprints every attribute of every sqlglot class. Comparing with the
pinned commit's fingerprints gives the exact set of class attributes whose value the
patch changes. The selector then runs only the tests that READ one of them
(maps/attr.json.gz, recorded per test by tools/build_map/attrrec.py).

  python classstate.py baseline <src> <out.json>     fingerprints of the pinned commit
  python classstate.py check <src> <base.json> -     child: job on stdin, verdict on stdout
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import sys
import time
import types

import earlycut

# Bookkeeping of Python's abc module (a cache object whose repr is a memory address):
# not data any sqlglot code or test reads.
IGNORED_NAMES = frozenset(("_abc_impl",))
# Module globals that are machinery, not data derived from sqlglot's classes: typing
# helpers (TypeVar, ParamSpec, aliases), loggers, locks.
SKIP_GLOBAL_TYPES_FROM = frozenset(("typing", "typing_extensions", "logging", "_thread", "threading", "abc"))
SKIP_GLOBAL_TYPES = (types.GenericAlias, types.UnionType)

VERSION = 4


class StateFingerprinter(earlycut.Fingerprinter):
    """earlycut's fingerprint, plus the code of functions: a lambda inside a class-level
    table is part of the table's value, and two lambdas differ only in their code."""

    def code_text(self, co) -> str:
        # the same function sits in many tables (subclasses copy their parent's): hash it once
        cache = self.__dict__.setdefault("_code_cache", {})
        hit = cache.get(id(co))
        if hit is not None and hit[0] is co:
            return hit[1]
        text = self._code_text(co)
        cache[id(co)] = (co, text)
        return text

    def _code_text(self, co) -> str:
        consts = []
        for c in co.co_consts:
            if isinstance(c, types.CodeType):
                consts.append(self.code_text(c))
            else:
                consts.append(f"{type(c).__name__}:{c!r}")
        return hashlib.sha1(
            repr((co.co_code, consts, co.co_names, co.co_varnames, co.co_freevars, co.co_argcount, co.co_kwonlyargcount, co.co_flags)).encode()
        ).hexdigest()

    def dump(self, obj, path, out) -> None:
        code = getattr(obj, "__code__", None)
        if isinstance(code, types.CodeType):
            out.append(f"<code {self.code_text(code)}>")
        elif isinstance(obj, (classmethod, staticmethod)):
            out.append(f"<{type(obj).__name__} ")
            self.dump(obj.__func__, path, out)
            out.append(">")
            return
        elif isinstance(obj, property):
            out.append("<property ")
            for f in (obj.fget, obj.fset, obj.fdel):
                self.dump(f, path, out)
            out.append(">")
            return
        elif isinstance(obj, types.ModuleType):
            out.append(f"<module {obj.__name__}>")
            return
        elif isinstance(obj, functools.partial):
            # its repr shows the wrapped function's memory address
            out.append("<partial ")
            self.dump(obj.func, path, out)
            self.dump(obj.args, path, out)
            self.dump(obj.keywords, path, out)
            out.append(">")
            return
        elif isinstance(obj, types.MethodType):
            out.append("<bound method ")
            self.dump(obj.__func__, path, out)
            self.dump(obj.__self__, path, out)
            out.append(">")
            return
        elif isinstance(obj, (tuple, list)) and obj and all(isinstance(x, type) for x in obj):
            # A group of classes (for isinstance). sqlglot builds some from a set, so the
            # order is already random at the pinned commit; membership is what matters.
            names = sorted(f"{x.__module__}.{x.__qualname__}" for x in obj)
            out.append(f"{type(obj).__name__}[classes {names!r}]")
            return
        elif isinstance(obj, dict) and obj and any(isinstance(k, type) for k in obj):
            # A table keyed by classes. sqlglot builds some of these from sets of classes,
            # so their order already differs from run to run at the pinned commit (it
            # follows memory addresses): order cannot be what a test depends on.
            entries = []
            for k, v in obj.items():
                key_text: list[str] = []
                self.dump(k, path, key_text)
                value_text: list[str] = []
                self.dump(v, path, value_text)
                entries.append("".join(key_text) + "=>" + "".join(value_text))
            out.append(f"{type(obj).__name__}{{" + "|".join(sorted(entries)) + "}")
            return
        super().dump(obj, path, out)


_FUNC_CACHE: dict = {}  # id(function) -> (function, its entry digest); keeping the function keeps the id its own


def entry_digests(fp: "StateFingerprinter", table: dict) -> dict[str, str] | None:
    """For a table that holds plain functions (FUNCTIONS, TRANSFORMS, ...): one digest per
    entry. A function is "F:<its code>:<everything else about it>", so the checker can
    tell "only the body of this entry's function changed" from any other change."""
    if not any(isinstance(v, types.FunctionType) for v in table.values()):
        return None
    out = {}
    for k, v in table.items():
        key_text: list[str] = []
        fp.dump(k, set(), key_text)
        key = hashlib.sha1("".join(key_text).encode("utf-8", "surrogatepass")).hexdigest()[:16]
        if isinstance(v, types.FunctionType):
            hit = _FUNC_CACHE.get(id(v))
            if hit is not None and hit[0] is v:
                out[key] = hit[1]
                continue
            rest: list[str] = []
            rest.append(f"{v.__module__}.{v.__qualname__}")
            fp.dump(v.__defaults__, set(), rest)
            fp.dump(v.__kwdefaults__, set(), rest)
            for cell in v.__closure__ or ():
                try:
                    fp.dump(cell.cell_contents, set(), rest)
                except ValueError:
                    rest.append("<empty cell>")
            out[key] = "F:" + fp.code_text(v.__code__) + ":" + hashlib.sha1("".join(rest).encode("utf-8", "surrogatepass")).hexdigest()[:16]
            _FUNC_CACHE[id(v)] = (v, out[key])
        else:
            out[key] = "V:" + fp.digest(v)
    return out


def import_everything() -> list[str]:
    """Every sqlglot module, as the recorder imports them. Returns import errors."""
    import importlib
    import pkgutil

    import sqlglot

    errors = []
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except BaseException as e:  # noqa: BLE001
            errors.append(f"{info.name}: {type(e).__name__}: {e}"[:200])
    return errors


def function_digest(fp: "StateFingerprinter", v) -> str:
    """"F:<code>:<name, defaults, closure>" for a plain function (cached per object)."""
    hit = _FUNC_CACHE.get(id(v))
    if hit is not None and hit[0] is v:
        return hit[1]
    rest: list[str] = [f"{v.__module__}.{v.__qualname__}"]
    fp.dump(v.__defaults__, set(), rest)
    fp.dump(v.__kwdefaults__, set(), rest)
    for cell in v.__closure__ or ():
        try:
            fp.dump(cell.cell_contents, set(), rest)
        except ValueError:
            rest.append("<empty cell>")
    text = "F:" + fp.code_text(v.__code__) + ":" + hashlib.sha1("".join(rest).encode("utf-8", "surrogatepass")).hexdigest()[:16]
    _FUNC_CACHE[id(v)] = (v, text)
    return text


def code_objects(value) -> list:
    """The code objects that run when this attribute is used as a function: the function
    itself, the functions inside a classmethod / staticmethod / property /
    cached_property, and whatever a decorator's wrapper wraps (`__wrapped__`)."""
    found, todo, seen = [], [value], set()
    while todo and len(seen) < 12:
        v = todo.pop()
        if id(v) in seen or v is None:
            continue
        seen.add(id(v))
        if isinstance(v, (classmethod, staticmethod)):
            todo.append(v.__func__)
        elif isinstance(v, property):
            todo.extend((v.fget, v.fset, v.fdel))
        elif isinstance(v, functools.cached_property):
            todo.append(v.func)
        elif isinstance(v, types.FunctionType):
            found.append(v.__code__)
            todo.append(getattr(v, "__wrapped__", None))
        elif hasattr(v, "__wrapped__"):
            todo.append(v.__wrapped__)
    return found


def class_state(src: str) -> tuple[dict[str, str], list[str], dict[str, dict[str, str]], dict]:
    """{"module.Qual:NAME": digest} for every attribute in the body of every sqlglot
    class (dunders aside), and {"module::NAME": digest} for every module global, after
    importing everything. Also the per-entry digests of tables of functions, and the
    code object of every function-valued key (to say which lines a changed one covers)."""
    if src not in sys.path:
        sys.path.insert(0, src)
    errors = import_everything()
    fp = StateFingerprinter()
    state: dict[str, str] = {}
    entries: dict[str, dict[str, str]] = {}
    codes: dict[str, list] = {}
    digests: dict[int, tuple] = {}  # id(object) -> (object, digest): one dump per object
    seen: set[int] = set()

    def digest_of(value) -> str:
        hit = digests.get(id(value))
        if hit is not None and hit[0] is value:
            return hit[1]
        text = fp.digest(value)
        digests[id(value)] = (value, text)
        return text

    def visit(cls: type) -> None:
        if id(cls) in seen or not getattr(cls, "__module__", "").startswith("sqlglot"):
            return
        seen.add(id(cls))
        owner = f"{cls.__module__}.{cls.__qualname__}"
        for name, value in list(vars(cls).items()):
            if isinstance(value, type):
                visit(value)
                state[f"{owner}:{name}"] = f"<class {value.__module__}.{value.__qualname__}>"
                continue
            if (name.startswith("__") and name.endswith("__")) or name in IGNORED_NAMES:
                continue
            try:
                found = code_objects(value)
                if found:
                    codes[f"{owner}:{name}"] = found
                state[f"{owner}:{name}"] = fp.digest(value)
                if type(value) is dict:
                    detail = entry_digests(fp, value)
                    if detail is not None:
                        entries[f"{owner}:{name}"] = detail
            except BaseException as e:  # noqa: BLE001 - an attribute we cannot dump never matches
                state[f"{owner}:{name}"] = f"!{type(e).__name__}"

    modules = [
        (mod_name, mod) for mod_name, mod in sorted(sys.modules.items())
        if mod is not None and (mod_name == "sqlglot" or mod_name.startswith("sqlglot."))
    ]
    for mod_name, mod in modules:
        for value in list(vars(mod).values()):
            if isinstance(value, type):
                visit(value)
    # Module globals ("module::NAME"). Data under EVERY name that holds it (another
    # module may import it under a different name, and tests find it by name);
    # functions under the module that defines them; classes and modules are not data.
    for mod_name, mod in modules:
        for name, value in sorted(vars(mod).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(value, (type, types.ModuleType, types.BuiltinFunctionType)):
                continue
            if type(value).__module__ in SKIP_GLOBAL_TYPES_FROM or isinstance(value, SKIP_GLOBAL_TYPES):
                continue
            key = f"{mod_name}::{name}"
            try:
                if isinstance(value, types.FunctionType):
                    if value.__module__ == mod_name:
                        state[key] = function_digest(fp, value)
                        codes[key] = code_objects(value)
                    continue
                state[key] = digest_of(value)
                if type(value) is dict:
                    detail = entry_digests(fp, value)
                    if detail is not None:
                        entries[key] = detail
            except BaseException as e:  # noqa: BLE001
                state[key] = f"!{type(e).__name__}"
    return state, errors, entries, codes


def cmd_baseline(src: str, out_path: str) -> int:
    cpu0 = time.process_time()
    state, errors, entries, _codes = class_state(src)
    if errors:
        print(f"class state: import errors at the pinned commit: {errors[:3]}", file=sys.stderr)
        return 1
    payload = {
        "version": VERSION, "python": sys.version, "hashseed": os.environ.get("PYTHONHASHSEED"),
        "cpu_s": round(time.process_time() - cpu0, 3), "state": state, "entries": entries,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True)
    os.replace(tmp, out_path)
    print(f"class state: {len(state)} attributes, {payload['cpu_s']} s cpu", file=sys.stderr)
    return 0


def cmd_check(src: str, base_path: str) -> int:
    """Child. stdin: {"overrides": {path: source}, "cpu_limit_s": n}. stdout: one JSON line
    {"ok": bool, "changed": [keys], ...}; ok=False means "cannot tell" (keep everything)."""
    real_stdout = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    verdict: dict = {"ok": False, "why": "crash"}
    try:
        with open(base_path, encoding="utf-8") as f:
            raw = json.load(f)
        base, unstable = raw["state"], set(raw.get("unstable", ()))
        job = json.loads(sys.stdin.read())
        if job.get("cpu_limit_s"):
            try:
                import resource

                limit = int(job["cpu_limit_s"])
                resource.setrlimit(resource.RLIMIT_CPU, (limit + 1, limit + 2))
            except Exception:  # noqa: BLE001
                pass
        earlycut.install_overrides(src, job["overrides"])
        state, errors, entries, codes = class_state(src)
        if errors:
            verdict = {"ok": False, "why": f"import error: {errors[0]}"}
        else:
            changed = sorted(k for k in set(base) | set(state) if base.get(k) != state.get(k))
            # "code only": every changed attribute is a table of functions with the same
            # keys, and each entry that differs is a function whose body changed and
            # nothing else (same name, defaults and closure). Then the only way to see
            # the change is to CALL one of those functions.
            base_entries = raw.get("entries", {})

            def only_bodies(k: str) -> bool:
                old_e, new_e = base_entries.get(k), entries.get(k)
                if old_e is None or new_e is None or set(old_e) != set(new_e):
                    return False
                for ek in old_e:
                    a, b = old_e[ek], new_e[ek]
                    if a != b and not (a.startswith("F:") and b.startswith("F:") and a.rsplit(":", 1)[1] == b.rsplit(":", 1)[1]):
                        return False
                return True

            # What each changed key is: "code" = a table of functions where only bodies
            # changed; "func" = a function (with the file and lines of its code);
            # "data" = anything else.
            kinds, ranges = {}, {}
            root_dir = os.path.abspath(src) + os.sep
            for k in changed:
                if only_bodies(k):
                    kinds[k] = "code"
                elif k in codes and k in state:
                    spans = []
                    for co in codes[k]:
                        filename = os.path.abspath(co.co_filename)
                        lines = [ln for _, _, ln in co.co_lines() if ln is not None] + [co.co_firstlineno]
                        if not filename.startswith(root_dir):
                            spans = None  # code outside the sqlglot tree: nothing to look up
                            break
                        spans.append([filename[len(root_dir):].replace(os.sep, "/"), min(lines), max(lines)])
                    if spans:
                        kinds[k] = "func"
                        ranges[k] = spans
                    else:
                        kinds[k] = "data"
                else:
                    kinds[k] = "data"
            code_only = bool(changed) and all(v == "code" for v in kinds.values())
            verdict = {
                "ok": True, "changed": changed, "code_only": code_only, "kinds": kinds, "ranges": ranges,
                "unstable_changed": sorted(k for k in changed if k in unstable),
                "added": sorted(k for k in state if k not in base), "removed": sorted(k for k in base if k not in state),
            }
    except BaseException as e:  # noqa: BLE001
        verdict = {"ok": False, "why": f"crash {type(e).__name__}: {e}"[:200]}
    verdict["cpu_s"] = round(time.process_time(), 3)
    real_stdout.write(json.dumps(verdict) + "\n")
    real_stdout.flush()
    return 0


def run_check(src: str, base_path: str, overrides: dict[str, str], wall_limit_s: float, cpu_limit_s: float, log) -> dict | None:
    import subprocess

    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "check", src, base_path, "-"],
            input=json.dumps({"overrides": overrides, "cpu_limit_s": cpu_limit_s}),
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=wall_limit_s,
        )
        verdict = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as e:  # noqa: BLE001 - timeout, crash, no verdict
        log(f"  class state: no verdict ({type(e).__name__})")
        return None
    return verdict


def load_baseline(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    if raw.get("version") != VERSION or raw.get("python") != sys.version or str(raw.get("hashseed")) != "0":
        return None
    return raw


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "baseline":
        sys.exit(cmd_baseline(sys.argv[2], sys.argv[3]))
    if len(sys.argv) == 5 and sys.argv[1] == "check":
        sys.exit(cmd_check(sys.argv[2], sys.argv[3]))
    print(__doc__, file=sys.stderr)
    sys.exit(2)
