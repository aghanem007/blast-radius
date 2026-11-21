"""Run a few cheap sqlglot tests against a patched sqlglot, fast, to see which fail.

The selector's map says which tests *could* notice a change. For a change to
function bodies only, we can find out which of the cheap ones *do*: when none
fail we print nothing for them (cost 0) instead of scheduling them (2 s or more).

How it stays cheap (the mutation-testing playbook: mutmut's fork-per-mutant,
mutant hot-swapping):
  1. Warm once: import sqlglot and the needed test modules into this process.
  2. Per patch: compile the old and new source and pair their code objects. Only
     changes to function bodies qualify; anything that runs at import (module or
     class body) does not, and the caller schedules those tests normally.
  3. Fork. In the child, swap the new code into the live function objects
     (f.__code__ = new), run the tests with unittest, and send back which failed.
     The parent never sees the patched code, so every patch starts clean.

Safety: a child that crashes, hangs past its deadline, or cannot run a test
reports every requested test as failing, so a problem here can only cost time,
never a miss. Tests that are not plain unittest methods are never run here.
"""

from __future__ import annotations

import gc
import importlib
import json
import os
import select
import signal
import sys
import time
import types
import unittest

CO_OPTIMIZED = 0x0001  # set on function code; module and class bodies lack it


class InProcessExecutor:
    """Stand-in for ProcessPoolExecutor: runs tasks in this process.

    The optimizer and executor tests hand work to spawned processes, which would
    re-import the *unpatched* sqlglot from disk. Running in-process keeps the
    swapped code in effect. Arguments and results are pickled, as a real pool
    would (sqlglot expressions pickle through sqlglot.serde).
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, /, *args, **kwargs):
        import concurrent.futures as cf

        future = cf.Future()
        try:
            fn, args, kwargs = _roundtrip((fn, args, kwargs))
            future.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as e:
            future.set_exception(e)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _roundtrip(value):
    import pickle

    return pickle.loads(pickle.dumps(value))


# ------------------------------------------------------------------ code pairing


def _walk(co: types.CodeType, out: list) -> None:
    out.append(co)
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            _walk(c, out)


def _const_key(c):
    """Exact type and value, recursively (see selector.const_key: (0,) == (False,))."""
    if isinstance(c, types.CodeType):
        return "<code>"
    t = type(c)
    if t is tuple:
        return ("tuple", tuple(_const_key(x) for x in c))
    if t is frozenset:
        return ("frozenset", frozenset(_const_key(x) for x in c))
    if t in (float, complex):
        return (t.__name__, repr(c))
    return (t.__name__, repr(c) if t not in (int, str, bytes, bool, type(None), type(...)) else c)


def _own(co: types.CodeType):
    """What a code object does by itself, ignoring nested code and line numbers
    (including a class body's __firstlineno__ constant, which moves with comments)."""
    class_line = co.co_firstlineno if "__firstlineno__" in co.co_names else None
    consts = tuple(
        "<firstlineno>" if type(c) is int and c == class_line
        else _const_key(c)
        for c in co.co_consts
    )
    return (
        co.co_code, co.co_names, co.co_varnames, co.co_freevars, co.co_cellvars, consts,
        co.co_flags, co.co_argcount, co.co_kwonlyargcount, co.co_posonlyargcount,
    )


def _walk_parents(co: types.CodeType, parent, out: list) -> None:
    out.append((co, parent))
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            _walk_parents(c, co, out)


def plan_swaps(filename: str, old_src: str, new_src: str):
    """Swaps for a function-body-only change, else None.

    Returns [(qualname, firstlineno, new code, ancestor keys)]: every *function* whose
    code changed (its own instructions, or code nested in it). Class and module bodies
    are never swapped: they ran at import, and a change to their own instructions
    disqualifies the patch. `ancestor keys` are the enclosing changed functions: a
    nested function with no live object (a generator expression, a closure made per
    call) is covered when an ancestor is swapped, since new instances come from it.
    """
    try:
        old = compile(old_src, filename, "exec", dont_inherit=True)
        new = compile(new_src, filename, "exec", dont_inherit=True)
    except SyntaxError:
        return None
    a, b = [], []
    _walk_parents(old, None, a)
    _walk_parents(new, None, b)
    if len(a) != len(b):
        return None
    key_of = {}
    parents = {id(c): cp for c, cp in a}
    swaps, seen = [], {}
    for (x, xp), (y, _) in zip(a, b):
        if x.co_qualname != y.co_qualname or len(x.co_freevars) != len(y.co_freevars):
            return None
        is_function = bool(x.co_flags & CO_OPTIMIZED)
        if _own(x) != _own(y) and not is_function:
            return None  # module or class body: runs at import
        if not is_function or (x == y and _own(x) == _own(y)):
            continue
        key = (x.co_qualname, x.co_firstlineno)
        if key in seen and seen[key] is not y:
            return None  # two code objects we cannot tell apart
        seen[key] = y
        key_of[id(x)] = key
        ancestors, p = [], xp
        while p is not None:
            if id(p) in key_of:
                ancestors.append(key_of[id(p)])
            p = parents.get(id(p))
        swaps.append((x.co_qualname, x.co_firstlineno, y, ancestors))
    return swaps


# ------------------------------------------------------------------- the runner


class Runner:
    def __init__(self, src_root: str, log=lambda *a: None) -> None:
        self.src_root = os.path.realpath(src_root)
        self.log = log
        self.ready = False
        self.index: dict[tuple, list] | None = None
        self.indexed_modules = 0
        self.cpu_start = time.process_time()

    def warm(self) -> None:
        if self.ready:
            return
        if self.src_root not in sys.path:
            sys.path.insert(0, self.src_root)
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        import concurrent.futures as cf
        import concurrent.futures.process as cfp

        cf.ProcessPoolExecutor = InProcessExecutor
        cfp.ProcessPoolExecutor = InProcessExecutor
        import sqlglot  # noqa: F401

        self.ready = True

    def import_tests(self, nodeids) -> dict[str, type]:
        """Import the test modules once (in the parent) and return their classes."""
        classes = {}
        for nodeid in nodeids:
            path, cls, _ = nodeid.split("::")
            mod = importlib.import_module(path[:-3].replace("/", "."))
            classes[nodeid] = getattr(mod, cls)
        return classes

    def function_index(self) -> dict[tuple, list]:
        """Live sqlglot function objects by (file, qualname, first line)."""
        if self.index is not None and self.indexed_modules == len(sys.modules):
            return self.index
        root = os.path.join(self.src_root, "sqlglot") + os.sep
        index: dict[tuple, list] = {}
        for obj in gc.get_objects():
            if type(obj) is types.FunctionType:
                co = obj.__code__
                if co.co_filename.startswith(root):
                    index.setdefault((co.co_filename, co.co_qualname, co.co_firstlineno), []).append(obj)
        self.index, self.indexed_modules = index, len(sys.modules)
        return index

    @staticmethod
    def runnable(nodeid: str) -> bool:
        parts = nodeid.split("::")
        return len(parts) == 3 and "[" not in nodeid and parts[0].endswith(".py")

    def run(self, file_swaps: dict[str, list], nodeids: list[str], deadline_s: float) -> set[str]:
        """Return the nodeids that fail with the swapped code. On any trouble: all of them."""
        everything = set(nodeids)
        try:
            self.warm()
            # Import every patched module here, in the parent, before swapping. sqlglot
            # imports some modules lazily (serde loads on first pickle); left alone, the
            # child would load the *unpatched* file from disk mid-test and pass.
            for rel in file_swaps:
                importlib.import_module(rel[:-3].replace("/", ".").removesuffix(".__init__"))
            classes = self.import_tests(nodeids)
            index = self.function_index()
        except BaseException as e:
            self.log(f"    prerun setup failed: {e!r}")
            return everything
        targets, missing = [], []
        for rel, swaps in file_swaps.items():
            filename = os.path.join(self.src_root, rel)
            live = {(q, l) for q, l, _, _ in swaps if index.get((filename, q, l))}
            for qual, line, new, ancestors in swaps:
                found = index.get((filename, qual, line), ())
                if not found and not any(anc in live for anc in ancestors):
                    missing.append(f"{rel}:{qual}")
                targets.extend((fn, new) for fn in found)
        if missing:
            # A changed function we cannot find alive (created on the fly, or under a
            # name we do not see) could still run during a test: we cannot try it.
            self.log(f"    prerun: no live function for {missing[:3]}; assuming all fail")
            return everything

        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            try:
                os.close(r)
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)  # stdout is the answer channel: keep tests off it
                sys.stdout = open(os.devnull, "w")
                for fn, new in targets:
                    fn.__code__ = new
                failed = _run_unittests(nodeids, classes)
                os.write(w, json.dumps(sorted(failed)).encode())
            except BaseException:
                import traceback

                traceback.print_exc(file=sys.stderr)
                os.write(w, json.dumps(sorted(everything)).encode())
            finally:
                os._exit(0)
        os.close(w)
        chunks, end = [], time.monotonic() + deadline_s
        try:
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                ready, _, _ = select.select([r], [], [], left)
                if not ready:
                    raise TimeoutError
                data = os.read(r, 65536)
                if not data:
                    break
                chunks.append(data)
            os.waitpid(pid, 0)
            raw = b"".join(chunks).decode()
            if not raw:
                raise RuntimeError("child sent no answer")
            return set(json.loads(raw))  # an empty list means: nothing failed
        except Exception as e:
            self.log(f"    prerun child failed: {e!r}")
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except OSError:
                pass
            return everything
        finally:
            os.close(r)


def probe_run(runner: "Runner", rel: str, plan, nodeids: list[str], deadline_s: float, cpu_budget_s: float = 1e9) -> dict | None:
    """Run tests with an infection probe installed (see probe.py).

    Returns {"reached": n, "infected": n, "failed": [...], "complete": bool} or None
    on any trouble. It stops early at the first infection, or once the child has used
    cpu_budget_s of CPU; "complete" says whether every test ran.
    The instrumented function behaves exactly like the original, so "failed" should
    be empty; anything else means the probe environment is off and is not trusted.
    """
    import probe as probe_mod

    qual, line, code = plan
    try:
        runner.warm()
        importlib.import_module(rel[:-3].replace("/", ".").removesuffix(".__init__"))
        classes = runner.import_tests(nodeids)
        fns = runner.function_index().get((os.path.join(runner.src_root, rel), qual, line), [])
    except BaseException as e:
        runner.log(f"    probe setup failed: {e!r}")
        return None
    if not fns:
        return None
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            sys.stdout = open(os.devnull, "w")
            rec = probe_mod.Recorder()
            for fn in fns:
                fn.__globals__["__br_probe__"] = rec
                fn.__code__ = code
            # One infection settles it: stop at the first one instead of running on.
            # The CPU budget protects against a cost estimate that came in low.
            import resource

            def used() -> float:
                r = resource.getrusage(resource.RUSAGE_SELF)
                return r.ru_utime + r.ru_stime

            start, ran = used(), {"n": 0}

            def stop() -> bool:
                ran["n"] += 1
                return rec.infected > 0 or used() - start > cpu_budget_s

            failed = _run_unittests(nodeids, classes, stop=stop)
            os.write(w, json.dumps({
                "reached": rec.reached, "infected": rec.infected, "failed": sorted(failed),
                "complete": ran["n"] >= len(nodeids),
            }).encode())
        except BaseException:
            import traceback

            traceback.print_exc(file=sys.stderr)
        finally:
            os._exit(0)
    os.close(w)
    chunks, end = [], time.monotonic() + deadline_s
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError
            ready, _, _ = select.select([r], [], [], left)
            if not ready:
                raise TimeoutError
            data = os.read(r, 65536)
            if not data:
                break
            chunks.append(data)
        os.waitpid(pid, 0)
        raw = b"".join(chunks).decode()
        return json.loads(raw) if raw else None
    except Exception as e:
        runner.log(f"    probe child failed: {e!r}")
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass
        return None
    finally:
        os.close(r)


class _Result(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.bad: set[str] = set()
        self.bad_classes: set[str] = set()

    def _mark(self, test, err=None) -> None:
        if err is not None and os.environ.get("BR_PRERUN_DEBUG"):
            import traceback

            print(f"    [prerun] {test}: {''.join(traceback.format_exception(*err))[-800:]}", file=sys.stderr)
        holder = getattr(test, "description", None)
        if isinstance(test, unittest.case._SubTest):
            test = test.test_case
        if holder is not None and not isinstance(test, unittest.TestCase):
            self.bad_classes.add(holder)  # setUpClass / tearDownClass / module fixtures
            return
        self.bad.add(test.id())

    def addError(self, test, err):
        self._mark(test, err)

    def addFailure(self, test, err):
        self._mark(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test, err)

    def addUnexpectedSuccess(self, test):
        self._mark(test)

    stop_check = None

    def stopTest(self, test):
        super().stopTest(test)
        if self.stop_check is not None and self.stop_check():
            self.stop()  # unittest checks shouldStop before each next test


def _run_unittests(nodeids: list[str], classes: dict[str, type], stop=None) -> set[str]:
    suite = unittest.TestSuite()
    by_uid = {}
    for nodeid in nodeids:
        test = classes[nodeid](nodeid.split("::")[2])
        by_uid[test.id()] = nodeid
        suite.addTest(test)
    result = _Result()
    if stop is not None:
        result.stop_check = stop
    suite.run(result)
    failed = {by_uid[u] for u in result.bad if u in by_uid}
    for holder in result.bad_classes:
        # "setUpClass (tests.test_x.TestX)": every test of that class failed
        name = holder[holder.find("(") + 1 : holder.rfind(")")] if "(" in holder else holder
        failed |= {n for u, n in by_uid.items() if u.startswith(name + ".") or name in u}
    return failed
