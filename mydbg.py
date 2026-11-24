"""
mydbg.py - validate a Python project you did not write line by line (for example one an AI wrote):
           step through, record and replay only the project's own code, skipping libraries (pandas, numpy, ...).

1) STEP (like pdb, but `s` never enters library code)
      python mydbg.py step my_model.py [script args...]
   or start from a point inside your code:   from mydbg import set_trace; set_trace()

   The target can be a script, a module, or an installed command (e.g. a CLI):
      python mydbg.py step my_model.py --year 2026
      python mydbg.py step -m mypkg.cli run --config cfg.yaml
      python mydbg.py step creditloss run --config cfg.yaml      (run from your project folder)

2) RECORD (clickable HTML report + every call's real inputs/outputs saved to disk)
      python mydbg.py record my_model.py [script args...]
      python mydbg.py record creditloss run --config config/eqb_public.yaml
         -> trace.html         open in a browser
         -> trace_calls/       one .pkl per call of your functions + index.csv
      options (put them BEFORE the script name):
        --out FILE        report file                          (default trace.html)
        --root DIR        what counts as "your code"           (default: script's folder)
        --max-calls N     calls per function recorded+saved    (default 20; 0 = all)
        --max-lines N     code lines shown per call            (default 2000)
        --calls-only      FAST: record only calls + inputs/outputs, no code lines
        --skip A,B        leave out files/functions (e.g. src/utils/trace.py,log);
                          calls made inside them are still recorded
        --max-mb N        don't save a single object bigger than N MB (default 200)
        --rows N          DataFrame preview rows in report     (default 5)

3) REPLAY a saved call (in Python / Jupyter / Spyder):
      from mydbg import load_call, list_calls
      list_calls()                          # table of all saved calls
      c = load_call(7)                      # or load_call("trace_calls/00007_add_features.pkl")
      c.inputs      c.globals      c.output      c.raised
      c.rerun()                             # run the function again on the saved inputs
      c.rerun(lags=5)                       # ... with some inputs changed
      c.check()                             # rerun and compare with the saved output
      c.debug()                             # step through it in pdb with the saved inputs

pdb commands:  n next | s step in | r run to return | c continue | l / ll list
               p x / pp x print | b file:line | u/d up/down | w where | q quit
"""
import os
import sys
import pdb
import csv
import dis
import html
import time
import types
import pickle
import reprlib
import datetime
import importlib
import importlib.util
from collections import Counter

_THIS = os.path.abspath(__file__)
_EXCLUDE = ("site-packages", "dist-packages", f"{os.sep}.venv{os.sep}",
            f"{os.sep}venv{os.sep}", f"{os.sep}env{os.sep}")
_CO_VARARGS, _CO_VARKW = 0x04, 0x08
_CO_GENLIKE = 0x20 | 0x80 | 0x200          # generator / coroutine / async generator


class _Filter:
    def __init__(self, root):
        self.root = os.path.abspath(root) + os.sep
        self._cache = {}

    def __call__(self, filename):
        r = self._cache.get(filename)
        if r is None:
            f = os.path.abspath(filename)
            if filename.startswith("<") or f == _THIS or not f.startswith(self.root):
                r = False                          # <frozen ...>, <string>, outside root
            else:
                rel = os.sep + f[len(self.root):]
                r = not any(x in rel for x in _EXCLUDE)
            self._cache[filename] = r
        return r


# ---------------------------------------------------------------- step mode
class ProjectPdb(pdb.Pdb):
    """pdb that only ever stops in files under `root`."""

    def __init__(self, root, **kw):
        super().__init__(**kw)
        self._mine = _Filter(root)

    def stop_here(self, frame):
        if not self._mine(frame.f_code.co_filename):
            return False
        return super().stop_here(frame)


def set_trace(root=None):
    frame = sys._getframe().f_back
    root = root or os.path.dirname(os.path.abspath(frame.f_code.co_filename))
    ProjectPdb(root).set_trace(frame)


# ------------------------------------------------------------ value display
def _short(v, maxlen=120):
    """One-line summary of any value (never prints a whole DataFrame)."""
    try:
        if isinstance(v, Unsaved):
            return repr(v)
        mod = type(v).__module__
        if mod.startswith("pandas"):
            if hasattr(v, "columns"):
                cols = list(v.columns)
                more = f" +{len(cols) - 8}" if len(cols) > 8 else ""
                return f"DataFrame{v.shape} cols={cols[:8]}{more}"
            if hasattr(v, "dtype") and hasattr(v, "shape"):
                return f"{type(v).__name__}{v.shape} dtype={v.dtype} head={list(getattr(v, "iloc", v)[:3])}"
        if mod.startswith("numpy") and getattr(v, "shape", ()) != ():
            return f"ndarray{v.shape} dtype={v.dtype}"
        r = reprlib.Repr()
        r.maxstring = r.maxother = maxlen
        r.maxlist = r.maxdict = r.maxtuple = r.maxset = 8
        s = r.repr(v)
    except Exception as e:
        s = f"<repr failed: {type(e).__name__}>"
    return s if len(s) <= maxlen else s[:maxlen] + "..."


def _value_html(v, rows):
    """Summary + (for tables/arrays) a click-to-open preview, captured NOW."""
    summary = html.escape(_short(v, 200))
    preview = None
    try:
        mod = type(v).__module__
        if mod.startswith("pandas") and hasattr(v, "head"):
            obj = v.to_frame() if not hasattr(v, "columns") else v
            obj = obj.iloc[:rows, :15]
            preview = obj.to_html(border=0, classes="df", max_cols=15)
            if hasattr(v, "dtypes") and hasattr(v, "columns"):
                dt = ", ".join(f"{c}: {t}" for c, t in list(v.dtypes.items())[:15])
                preview += f'<div class="dtypes">dtypes: {html.escape(dt)}</div>'
        elif mod.startswith("numpy") and getattr(v, "ndim", 0) >= 1:
            import numpy as np
            preview = "<pre>" + html.escape(np.array2string(v[:rows], threshold=60, max_line_width=110)) + "</pre>"
        elif isinstance(v, (dict, list, tuple, set)) and len(v) > 8:
            import pprint
            txt = pprint.pformat(v if not isinstance(v, dict) else dict(list(v.items())[:30]),
                                 width=100, compact=True)
            preview = "<pre>" + html.escape(txt[:3000]) + "</pre>"
    except Exception:
        preview = None
    if preview:
        return f'<details class="val"><summary><code>{summary}</code></summary>{preview}</details>'
    return f"<code>{summary}</code>"


# ---------------------------------------------------------- saving values
class Unsaved:
    """Placeholder for a value that could not be pickled (open file, DB connection, lambda, ...).
    For a function, `code` = (file, first line, qualname), so replay can stand in for it."""

    def __init__(self, text, code=None):
        self.text = text
        self.code = code

    def __repr__(self):
        return f"<not saved: {self.text}>"


def _dumps_dict(d):
    """Pickle a {name: value} dict NOW (snapshot); unpicklable items become Unsaved."""
    try:
        return pickle.dumps(d, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        safe = {}
        for k, v in d.items():
            try:
                pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL)
                safe[k] = v
            except Exception as e:
                safe[k] = Unsaved(f"{_short(v, 80)} ({type(e).__name__})")
        return pickle.dumps(safe, protocol=pickle.HIGHEST_PROTOCOL)


def _dump_value(v):
    try:
        return pickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:
        err = e
    try:                                   # lambdas / nested functions: cloudpickle can save them, if installed
        import cloudpickle
        return cloudpickle.dumps(v, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        pass
    code = getattr(v, "__code__", None) or getattr(getattr(v, "__func__", None), "__code__", None)
    ref = None
    if code is not None:
        ref = (os.path.abspath(code.co_filename), code.co_firstlineno, getattr(code, "co_qualname", code.co_name))
    what = f"function {ref[2]} ({os.path.basename(ref[0])}:{ref[1]})" if ref else _short(v, 80)
    return pickle.dumps(Unsaved(f"{what} - {type(err).__name__}", code=ref), protocol=pickle.HIGHEST_PROTOCOL)


_GLOBAL_CACHE = {}


def _global_names(code):
    """Names a function reads as globals (incl. inside its comprehensions/lambdas)."""
    r = _GLOBAL_CACHE.get(code)
    if r is None:
        names = set()
        stack = [code]
        while stack:
            c = stack.pop()
            for ins in dis.get_instructions(c):
                if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME") and isinstance(ins.argval, str):
                    names.add(ins.argval)
            stack.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
        r = _GLOBAL_CACHE[code] = tuple(sorted(names))
    return r


def _is_data(v):
    return not (isinstance(v, (types.ModuleType, type)) or callable(v))


# -------------------------------------------------------------- record mode
class _Node:
    __slots__ = ("name", "file", "lineno", "hidden", "args", "globs", "ret", "items",
                 "t0", "t1", "pending_exc", "raised", "snap", "last_line",
                 "nlines", "skipped_lines", "skipped_calls",
                 "cid", "parent_cid", "meta", "in_bytes", "pkl")

    def __init__(self, name, file, lineno, hidden):
        self.name, self.file, self.lineno, self.hidden = name, file, lineno, hidden
        self.args, self.globs, self.ret, self.items = [], [], None, []
        self.t0, self.t1 = time.perf_counter(), None
        self.pending_exc, self.raised = None, None
        self.snap, self.last_line = {}, None
        self.nlines, self.skipped_lines = 0, 0
        self.skipped_calls = Counter()
        self.cid = self.parent_cid = self.meta = self.in_bytes = self.pkl = None


_IMMUTABLE = {int, float, complex, bool, str, bytes, type(None), tuple, frozenset, range}


def _interesting(k, v):
    return (not (k.startswith("__") and k.endswith("__"))
            and type(v).__name__ not in ("module", "function", "type", "builtin_function_or_method"))


class Recorder:
    def __init__(self, root, script, data_dir, max_calls=20, max_lines=2000, rows=5, max_mb=200,
                 title=None, main_module=None):
        self.mine = _Filter(root)
        self.root = os.path.abspath(root)
        self.script = os.path.abspath(script) if script else None
        self.title = title or os.path.basename(script or "")
        self.main_module = main_module
        self.data_dir = os.path.abspath(data_dir)
        self.max_calls, self.max_lines, self.rows = max_calls, max_lines, rows
        self.max_bytes = max_mb * 1024 * 1024
        self.obj_dir = os.path.join(self.data_dir, "objects")
        os.makedirs(self.obj_dir, exist_ok=True)
        self.stored, self.bytes_written = set(), 0
        self.stack, self.roots = [], []
        self.counts = Counter()
        self.src = {}
        self.next_id = 1
        self.calls_only = False
        self.skip, self._skip_cache = [], {}
        self.t_start = self.last_progress = time.perf_counter()
        self.nevents = 0
        self.index = []

    # -- helpers
    def _line(self, fn, n):
        if fn not in self.src:
            try:
                with open(fn, encoding="utf-8", errors="replace") as f:
                    self.src[fn] = f.read().splitlines()
            except OSError:
                self.src[fn] = []
        L = self.src[fn]
        return L[n - 1].rstrip() if 0 < n <= len(L) else ""

    def _snap(self, frame, old=None):
        """{name: (id, summary)}; an unchanged immutable value is not summarised again."""
        new = {}
        for k, v in frame.f_locals.items():
            if not _interesting(k, v):
                continue
            o = old.get(k) if old else None
            if o is not None and o[0] == id(v) and type(v) in _IMMUTABLE:
                new[k] = o
            else:
                new[k] = (id(v), _short(v))
        return new

    def _diff(self, node, frame):
        new = self._snap(frame, node.snap)
        if node.last_line is not None:
            for k, v in new.items():
                o = node.snap.get(k)
                if o is None or o[1] != v[1]:
                    node.last_line[3].append((k, v[1]))
        node.snap = new

    def _progress(self, frame, force=False):
        now = time.perf_counter()
        if force or now - self.last_progress >= 5:
            self.last_progress = now
            code = frame.f_code
            msg = (f"[mydbg] {now - self.t_start:6.0f}s | {self.next_id - 1:,} calls recorded | "
                   f"now in {code.co_name} ({os.path.basename(code.co_filename)}:{frame.f_lineno})")
            if sys.stderr.isatty():
                sys.stderr.write("\r" + msg[:150].ljust(150))
            else:
                sys.stderr.write(msg + "\n")
            sys.stderr.flush()

    def _capture_call(self, node, frame, parent):
        """Snapshot inputs (and globals the function reads) at call time."""
        co = frame.f_code
        n = (co.co_argcount + co.co_kwonlyargcount
             + bool(co.co_flags & _CO_VARARGS) + bool(co.co_flags & _CO_VARKW))
        names = co.co_varnames[:n]
        loc = frame.f_locals
        inputs = {k: loc[k] for k in names if k in loc}
        g = frame.f_globals
        globs = {k: g[k] for k in _global_names(co)
                 if k in g and not k.startswith("__") and _is_data(g[k])}
        node.args = [(k, _short(v, 40), _value_html(v, self.rows)) for k, v in inputs.items()]
        node.globs = [(k, _value_html(v, self.rows)) for k, v in globs.items()]
        node.cid = self.next_id
        self.next_id += 1
        node.parent_cid = parent.cid if parent else None
        if co.co_flags & _CO_GENLIKE:
            return                                    # generator resumes are not real calls
        node.meta = {
            "id": node.cid, "name": co.co_name, "qualname": getattr(co, "co_qualname", co.co_name),
            "module": g.get("__name__"), "file": os.path.abspath(co.co_filename),
            "line": co.co_firstlineno, "root": self.root, "script": self.script, "main_module": self.main_module,
            "argcount": co.co_argcount, "posonly": co.co_posonlyargcount,
            "kwonly": co.co_kwonlyargcount,
            "varargs": bool(co.co_flags & _CO_VARARGS), "varkw": bool(co.co_flags & _CO_VARKW),
            "names": list(names), "parent": node.parent_cid,
        }
        node.in_bytes = ({k: self._store(v) for k, v in inputs.items()},
                         {k: self._store(v) for k, v in globs.items()})

    def _store(self, v):
        """Save one value under objects/<content hash>.pkl (identical objects are stored once)."""
        import hashlib
        b = _dump_value(v)
        if len(b) > self.max_bytes:
            b = pickle.dumps(Unsaved(f"{_short(v, 80)} ({len(b) / 1e6:.0f} MB > --max-mb)"))
        h = hashlib.blake2b(b, digest_size=16).hexdigest()
        if h not in self.stored:
            with open(os.path.join(self.obj_dir, h + ".pkl"), "wb") as f:
                f.write(b)
            self.stored.add(h)
            self.bytes_written += len(b)
        return h

    def _save_call(self, node, ret):
        if node.meta is None:
            return
        status = "raised" if node.raised else "ok"
        out_h = None if node.raised else self._store(ret)
        inp_h, glob_h = node.in_bytes
        node.in_bytes = None
        meta = dict(node.meta, status=status, raised=node.raised,
                    ms=round((node.t1 - node.t0) * 1000, 3))
        safe = "".join(ch if ch.isalnum() or ch == "_" else "" for ch in node.name) or "fn"
        fname = f"{node.cid:05d}_{safe}.pkl"
        with open(os.path.join(self.data_dir, fname), "wb") as f:
            pickle.dump({"meta": meta, "format": 2, "inputs": inp_h, "globals": glob_h, "output": out_h},
                        f, protocol=pickle.HIGHEST_PROTOCOL)
        node.pkl = os.path.join(self.data_dir, fname)
        self.index.append({"id": node.cid, "function": meta["qualname"],
                           "file": os.path.relpath(meta["file"], self.root), "line": meta["line"],
                           "parent": node.parent_cid or "", "status": status, "ms": meta["ms"],
                           "pkl": fname if node.pkl else ""})

    # -- tracing
    def global_trace(self, frame, event, arg):
        if event != "call" or not self.mine(frame.f_code.co_filename):
            return None
        code = frame.f_code
        name = code.co_name
        if name.startswith("<") and name not in ("<module>", "<lambda>"):
            return None                 # genexpr / comprehension: transparent
        if name != "<module>" and not (code.co_flags & 0x01):
            return None                 # class body (not a real function call)
        if self.skip and self._skipped(code):
            return None                 # --skip: not shown, but calls made inside it still are
        # attach to the nearest call that is shown (a call hidden by --max-calls
        # does NOT hide the calls made inside it)
        parent = next((n for n in reversed(self.stack) if not n.hidden), None)
        hidden = False
        if name != "<module>":
            key = (code.co_filename, code.co_firstlineno, name)
            self.counts[key] += 1
            if self.max_calls and self.counts[key] > self.max_calls:
                hidden = True
                if parent:
                    parent.skipped_calls[name] += 1
        node = _Node(name, code.co_filename, code.co_firstlineno, hidden)
        if not hidden:
            if name != "<module>":
                self._capture_call(node, frame, parent)
            node.snap = {} if self.calls_only else self._snap(frame)
            (parent.items if parent else self.roots).append(("call", node))
        self.stack.append(node)
        if hidden or self.calls_only:
            frame.f_trace_lines = False        # no per-line events: much faster
        self._progress(frame)
        node.t0 = time.perf_counter()          # don't count our own pickling time
        return self.local_trace

    def _skipped(self, code):
        r = self._skip_cache.get(code)
        if r is None:
            rel = os.path.relpath(code.co_filename, self.root).replace(os.sep, "/")
            qn = getattr(code, "co_qualname", code.co_name)
            r = self._skip_cache[code] = any(p in rel or p == qn or p == code.co_name for p in self.skip)
        return r

    def local_trace(self, frame, event, arg):
        node = self.stack[-1]
        if node.hidden:
            if event == "return":
                self.stack.pop()
            return self.local_trace
        if event == "line":
            node.pending_exc = None
            self.nevents += 1
            if self.nevents % 2000 == 0:
                self._progress(frame)
            self._diff(node, frame)
            if node.nlines >= self.max_lines:
                node.skipped_lines += 1
                node.last_line = None
                frame.f_trace_lines = False    # stop line events for the rest of this call
            else:
                item = ["line", frame.f_lineno, self._line(frame.f_code.co_filename, frame.f_lineno), []]
                node.items.append(item)
                node.last_line = item
                node.nlines += 1
        elif event == "exception":
            if issubclass(arg[0], (StopIteration, StopAsyncIteration, GeneratorExit)):
                return self.local_trace        # normal end of a loop / generator, not an error
            node.pending_exc = arg
            node.items.append(("exc", f"{arg[0].__name__}: {_short(arg[1], 200)}"))
        elif event == "return":
            node.t1 = time.perf_counter()
            if not self.calls_only:
                self._diff(node, frame)
            pe = node.pending_exc
            if pe is not None and arg is not None:
                pe = node.pending_exc = None             # it returned a value, so the error was handled
            if pe is not None and issubclass(pe[0], SystemExit) and getattr(pe[1], "code", None) in (None, 0):
                pe = node.pending_exc = None             # normal sys.exit(0) of a CLI, not an error
                node.items.pop() if node.items and node.items[-1][0] == "exc" else None
            if pe is not None:
                node.raised = f"{node.pending_exc[0].__name__}: {_short(node.pending_exc[1], 200)}"
            elif node.name != "<module>":
                node.ret = (_short(arg, 40), _value_html(arg, self.rows))
            self._save_call(node, arg)
            self.stack.pop()
        return self.local_trace

    # -- outputs
    def write_index(self):
        with open(os.path.join(self.data_dir, "index.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["id", "function", "file", "line", "parent", "status", "ms", "pkl"])
            w.writeheader()
            w.writerows(sorted(self.index, key=lambda r: r["id"]))

    def _rel(self, fn):
        return os.path.relpath(fn, self.root)

    def _render(self, n, out, depth):
        e = html.escape
        dur = ""
        if n.t1 is not None:
            ms = (n.t1 - n.t0) * 1000
            dur = f"{ms:.1f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"
        tag = f'<span class="cid">#{n.cid}</span> ' if n.cid else ""
        if n.name == "<module>":
            title = f'<span class="fn">{e(self._rel(n.file))}</span>'
        else:
            args = ", ".join(f'{e(k)}=<span class="a">{e(s)}</span>' for k, s, _ in n.args)
            if n.raised:
                res = f'<span class="raise">raised {e(n.raised.split(":")[0])}</span>'
            elif n.ret:
                res = f'<span class="ret">{e(n.ret[0])}</span>'
            else:
                res = '<span class="ret">(still running)</span>'
            title = f'{tag}<span class="fn">{e(n.name)}</span>({args}) <span class="arrow">→</span> {res}'
        cls = "call err" if n.raised else "call"
        is_open = " open" if depth == 0 else ""
        anchor = f' id="c{n.cid}"' if n.cid else ""
        out.append(f'<details class="{cls}"{anchor}{is_open}><summary>{title}'
                   f'<span class="meta">{e(self._rel(n.file))}:{n.lineno} · {dur}</span></summary><div class="body">')

        # 1) what went in and what came out -- first, so you don't have to read the whole function
        if n.args or n.globs:
            out.append('<table class="io"><tr><th colspan=2>INPUTS</th></tr>')
            for k, _, h in n.args:
                out.append(f"<tr><td class=k>{e(k)}</td><td>{h}</td></tr>")
            for k, h in n.globs:
                out.append(f'<tr><td class=k>{e(k)} <span class="gl">global</span></td><td>{h}</td></tr>')
            out.append("</table>")
        if n.name != "<module>":
            out.append('<table class="io out"><tr><th colspan=2>OUTPUT</th></tr>')
            if n.raised:
                out.append(f'<tr><td class=k>raised</td><td><code class="raise">{e(n.raised)}</code></td></tr>')
            elif n.ret:
                out.append(f"<tr><td class=k>return</td><td>{n.ret[1]}</td></tr>")
            if n.pkl:
                snippet = f'c = load_call(r"{n.pkl}")'
                out.append(f'<tr><td class=k>replay</td><td><code class="rp">{e(snippet)}</code>'
                           f'<button class="cp" onclick="cp(this)">copy</button></td></tr>')
            out.append("</table>")

        # 2) the calls it made, in order
        calls = [it[1] for it in n.items if it[0] == "call"]
        lines = [it for it in n.items if it[0] == "line"]
        excs = [it for it in n.items if it[0] == "exc"]
        if calls or n.skipped_calls:
            out.append(f'<div class="sec">CALLS MADE ({len(calls)})</div>')
            for c in calls:
                self._render(c, out, depth + 1)
            for fn_name, c in n.skipped_calls.items():
                out.append(f'<div class="note">… {c:,} more calls to {e(fn_name)}() not recorded (--max-calls)</div>')
        if excs and not lines:
            for it in excs:
                out.append(f'<div class="exc">⚠ {e(it[1])}</div>')

        # 3) the code, line by line -- folded; open it only when you need it
        if lines:
            out.append(f'<details class="code"><summary>Code, line by line ({len(lines)} lines)</summary>')
            for it in n.items:
                kind = it[0]
                if kind == "call":
                    c = it[1]
                    out.append(f'<div class="callref"><a href="#c{c.cid}" onclick="return go({c.cid})">'
                               f'↳ #{c.cid} {e(c.name)}()</a> <span class="mut">(shown above)</span></div>')
                elif kind == "line":
                    _, no, src, ch = it
                    chs = "".join(f'<div class="chg">{e(k)} = {e(v)}</div>' for k, v in ch)
                    out.append(f'<div class="ln"><span class="no">{no}</span><code>{e(src)}</code>{chs}</div>')
                elif kind == "exc":
                    out.append(f'<div class="exc">⚠ {e(it[1])}</div>')
            if n.skipped_lines:
                out.append('<div class="note">… later lines not shown (--max-lines)</div>')
            out.append("</details>")
        out.append("</div></details>")

    def write_html(self, path, elapsed, crash=None):
        sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))
        body = []
        for _, n in self.roots:
            self._render(n, body, 0)
        crash_html = (f'<div class="crash">Script stopped with {html.escape(crash)}. '
                      f'The failing call is marked in red.</div>') if crash else ""
        with open(path, "w", encoding="utf-8") as f:
            f.write(_TEMPLATE.format(
                title=html.escape(self.title),
                when=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                elapsed=f"{elapsed:.2f}", ncalls=len(self.index),
                data=html.escape(self.data_dir), crash=crash_html, body="\n".join(body)))


_TEMPLATE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>trace · {title}</title><style>
:root{{--bg:#fff;--fg:#1d1d1f;--mut:#86868b;--line:#e5e5ea;--fn:#0b5cad;--a:#6e3fb0;--ret:#1a7f37;--err:#c62828;--chg:#9a6700;--hl:#f5f5f7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1c1e;--fg:#f2f2f7;--mut:#8e8e93;--line:#3a3a3c;--fn:#64a8ff;--a:#c79bff;--ret:#56d364;--err:#ff6b6b;--chg:#e3b341;--hl:#2c2c2e}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:16px 20px 60px}}
code,pre,.ln,summary{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}}
header{{position:sticky;top:0;background:var(--bg);padding:8px 0 10px;border-bottom:1px solid var(--line);margin-bottom:10px;z-index:2}}
h1{{font-size:17px;margin:0 0 4px}} .sub{{color:var(--mut);font-size:12px}}
button{{font:inherit;font-size:12px;padding:4px 10px;margin:6px 6px 0 0;border:1px solid var(--line);background:var(--hl);color:var(--fg);border-radius:6px;cursor:pointer}}
input{{font:inherit;font-size:12px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);width:180px}}
details.call{{border-left:2px solid var(--line);margin:3px 0 3px 4px;padding-left:10px}}
details.call>summary{{cursor:pointer;padding:3px 4px;border-radius:4px;list-style:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
details.call>summary::before{{content:"▸ ";color:var(--mut)}} details.call[open]>summary::before{{content:"▾ "}}
details.call>summary:hover{{background:var(--hl)}}
details.err{{border-left-color:var(--err)}}
.fn{{color:var(--fn);font-weight:600}} .a{{color:var(--a)}} .ret{{color:var(--ret)}} .arrow{{color:var(--mut)}}
.cid{{color:var(--mut);font-size:11px}} .gl{{color:var(--mut);font-size:10px;font-family:-apple-system,sans-serif}}
.raise{{color:var(--err);font-weight:600}} .meta{{color:var(--mut);font-size:11px;margin-left:12px}}
.body{{padding:2px 0 6px 6px}}
.ln{{padding:1px 0;white-space:pre-wrap}} .no{{display:inline-block;width:40px;color:var(--mut);text-align:right;margin-right:12px;user-select:none}}
.chg{{color:var(--chg);margin-left:52px;font-size:12px}}
.exc{{color:var(--err);margin-left:52px;font-size:12px}}
.note{{color:var(--mut);font-style:italic;margin:3px 0 3px 52px;font-size:12px}}
table.io{{border-collapse:collapse;margin:4px 0 6px 0;background:var(--hl);border-radius:6px;min-width:300px}}
table.io th{{text-align:left;font-size:10px;letter-spacing:.08em;color:var(--mut);padding:4px 8px 0}}
table.io td{{padding:2px 8px;vertical-align:top}} table.io td.k{{color:var(--a);font-family:ui-monospace,Menlo,monospace;font-size:13px}}
table.out th{{color:var(--ret)}}
code.rp{{font-size:11px;color:var(--mut);word-break:break-all}} button.cp{{margin:0 0 0 8px;padding:1px 8px;font-size:11px}}
details.val>summary{{cursor:pointer}} details.val>summary::marker{{color:var(--mut)}}
table.df{{border-collapse:collapse;margin:4px 0;font-size:12px}} table.df th,table.df td{{border:1px solid var(--line);padding:2px 6px;text-align:right}}
.dtypes{{color:var(--mut);font-size:11px}}
.crash{{background:var(--err);color:#fff;padding:8px 12px;border-radius:6px;margin:8px 0}}
body.compact details.code{{display:none}}
.hit>summary{{outline:2px solid var(--chg)}}
.flash>summary{{animation:fl 2s ease-out}} @keyframes fl{{0%{{background:var(--chg);color:var(--bg)}}100%{{background:transparent}}}}
.sec{{font-size:10px;letter-spacing:.08em;color:var(--mut);margin:8px 0 2px 2px}}
details.code{{margin:6px 0 4px;border:1px dashed var(--line);border-radius:6px;padding:2px 8px}}
details.code>summary{{cursor:pointer;color:var(--mut);font-size:12px;padding:2px 0}}
.callref{{margin:1px 0 1px 52px;font-size:12px}} .callref a{{color:var(--fn);text-decoration:none}} .mut{{color:var(--mut)}}
.sw{{position:relative;display:inline-block;vertical-align:top}}
#q{{width:340px}}
#res{{display:none;position:absolute;left:0;top:30px;width:min(760px,92vw);max-height:60vh;overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.18);z-index:10}}
#res .r{{padding:5px 10px;cursor:pointer;font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid var(--line)}}
#res .r.sel,#res .r:hover{{background:var(--hl)}} #res .r b{{color:var(--fn)}} #res .r .m{{color:var(--mut);margin-left:8px}}
#res .hd{{padding:4px 10px;font-size:11px;color:var(--mut)}}
</style></head><body>
<header><h1>Trace · {title}</h1>
<div class="sub">recorded {when} · {elapsed} s (with tracing overhead) · {ncalls} calls saved to {data} · click a call to open it</div>
<button onclick="setAll(true)">Expand all</button><button onclick="setAll(false)">Collapse all</button>
<button id="cb" onclick="toggleCompact()">Hide code lines (calls only)</button>
<span class="sw"><input id="q" placeholder="search: function, #123, file, value…  (press / )" autocomplete="off"
 oninput="search(this.value)" onkeydown="keys(event)" onfocus="search(this.value)"><div id="res"></div></span>
</header>{crash}
<main>{body}</main>
<script>
function setAll(o){{document.querySelectorAll('details.call').forEach(d=>d.open=o)}}
function toggleCompact(){{const c=document.body.classList.toggle('compact');document.getElementById('cb').textContent=c?'Show code lines':'Hide code lines (calls only)'}}
let IDX=null,SEL=0,HITS=[];
function buildIdx(){{IDX=[];document.querySelectorAll('details.call').forEach((d,i)=>{{const sm=d.querySelector(':scope>summary');if(!sm)return;
 const fn=(sm.querySelector('.fn')||{{}}).textContent||'';const cid=(sm.querySelector('.cid')||{{}}).textContent||'';const meta=(sm.querySelector('.meta')||{{}}).textContent||'';
 const txt=sm.textContent;IDX.push({{d,fn:fn.toLowerCase(),fnRaw:fn,cid:cid.replace('#',''),meta,txt,low:(txt+' '+meta).toLowerCase()}})}})}}
function esc(t){{return t.replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}})[c])}}
function search(q){{if(!IDX)buildIdx();const box=document.getElementById('res');q=q.trim().toLowerCase();
 document.querySelectorAll('.hit').forEach(d=>d.classList.remove('hit'));
 if(!q){{box.style.display='none';HITS=[];return}}
 let hits;
 if(/^#?\\d+$/.test(q)){{const n=q.replace('#','');hits=IDX.filter(x=>x.cid===n)}}
 else{{const toks=q.split(/\\s+/);hits=IDX.filter(x=>toks.every(t=>x.low.includes(t)));
  const score=x=>x.fn===toks[0]?0:x.fn.startsWith(toks[0])?1:x.fn.includes(toks[0])?2:3;
  hits.sort((a,b)=>score(a)-score(b))}}
 HITS=hits;SEL=0;
 let h='<div class="hd">'+hits.length+' match'+(hits.length===1?'':'es')+(hits.length>200?' (first 200 shown)':'')+' · ↑↓ to move, Enter to open'+'</div>';
 hits.slice(0,200).forEach((x,i)=>{{let t=esc(x.txt.replace(x.meta,'').trim());if(x.fnRaw)t=t.replace(esc(x.fnRaw),'<b>'+esc(x.fnRaw)+'</b>');
  h+='<div class="r'+(i===0?' sel':'')+'" onmousedown="pick('+i+')">'+t+'<span class="m">'+esc(x.meta)+'</span></div>'}});
 if(!hits.length)h='<div class="hd">no match</div>';
 box.innerHTML=h;box.style.display='block';hits.forEach(x=>x.d.classList.add('hit'))}}
function keys(ev){{const box=document.getElementById('res');const rs=box.querySelectorAll('.r');
 if(ev.key==='ArrowDown'||ev.key==='ArrowUp'){{ev.preventDefault();if(!rs.length)return;rs[SEL].classList.remove('sel');
  SEL=(SEL+(ev.key==='ArrowDown'?1:-1)+rs.length)%rs.length;rs[SEL].classList.add('sel');rs[SEL].scrollIntoView({{block:'nearest'}})}}
 else if(ev.key==='Enter'){{if(HITS.length)pick(SEL)}}
 else if(ev.key==='Escape'){{box.style.display='none';ev.target.blur()}}}}
function show(d){{let p=d.parentElement;while(p){{if(p.tagName==='DETAILS')p.open=true;p=p.parentElement}}d.open=true;
 d.scrollIntoView({{block:'start'}});window.scrollBy(0,-110);d.classList.remove('flash');void d.offsetWidth;d.classList.add('flash')}}
function pick(i){{const x=HITS[i];if(!x)return;document.getElementById('res').style.display='none';show(x.d)}}
function go(cid){{const d=document.getElementById('c'+cid);if(d)show(d);return false}}
document.addEventListener('keydown',ev=>{{if(ev.key==='/'&&document.activeElement.tagName!=='INPUT'){{ev.preventDefault();document.getElementById('q').focus()}}}});
document.addEventListener('click',ev=>{{if(!ev.target.closest('.sw'))document.getElementById('res').style.display='none'}});
function cp(b){{const t=b.previousElementSibling.textContent;const done=()=>{{b.textContent='copied';setTimeout(()=>b.textContent='copy',1200)}};
 if(navigator.clipboard)navigator.clipboard.writeText(t).then(done,()=>sel(b));else sel(b)}}
function sel(b){{const r=document.createRange();r.selectNodeContents(b.previousElementSibling);const s=getSelection();s.removeAllRanges();s.addRange(r)}}
</script></body></html>"""


# --------------------------------------------------------------- replay API
_SCRIPT_MODS = {}


def _script_module(script):
    """The main script as an importable module, WITHOUT running its top-level code:
    only imports, function defs and class defs are executed."""
    import ast
    if script in _SCRIPT_MODS:
        return _SCRIPT_MODS[script]
    with open(script, encoding="utf-8") as f:
        tree = ast.parse(f.read(), script)
    keep = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    tree.body = [s for s in tree.body if isinstance(s, keep)]
    mod = types.ModuleType("_mydbg_script")
    mod.__file__ = script
    sys.modules["_mydbg_script"] = mod
    exec(compile(tree, script, "exec"), mod.__dict__)
    _SCRIPT_MODS[script] = mod
    return mod


def _main_source(meta):
    """Where things that lived in __main__ during the recording can be found now."""
    if meta.get("main_module"):                       # recorded with -m pkg.module
        return importlib.import_module(meta["main_module"])
    if meta.get("script"):
        return _script_module(meta["script"])
    raise RuntimeError("cannot locate the __main__ module of this recording")


class _Unpickler(pickle.Unpickler):
    """Classes defined in the main script were pickled as __main__.X - find them there."""

    def __init__(self, f, meta):
        super().__init__(f)
        self.meta = meta

    def find_class(self, module, name):
        if module == "__main__":
            main = sys.modules.get("__main__")
            if not hasattr(main, name):
                return getattr(_main_source(self.meta), name)
        return super().find_class(module, name)


class Call:
    """One saved call of one of your functions. See module docstring."""

    def __init__(self, path):
        import io
        self.path = os.path.abspath(path)
        with open(self.path, "rb") as f:
            d = pickle.load(f)
        self.meta = m = d["meta"]
        self._raw = d
        self._io = io
        for p in (m["root"], os.path.dirname(m["file"]), m.get("script") and os.path.dirname(m["script"])):
            if p and p not in sys.path:
                sys.path.insert(0, p)
        self.id, self.name, self.qualname = m["id"], m["name"], m["qualname"]
        self.file, self.line, self.raised, self.ms = m["file"], m["line"], m["raised"], m["ms"]
        self._cache = {}

    def _cached(self, key):
        if key not in self._cache:
            if key == "output":
                self._cache[key] = None if self._raw["output"] is None else self._load("output")["output"]
            else:
                self._cache[key] = self._load(key)
        return self._cache[key]

    inputs = property(lambda self: self._cached("inputs"), doc="Inputs as they were when the call started")
    globals = property(lambda self: self._cached("globals"), doc="Global variables the function read")
    output = property(lambda self: self._cached("output"), doc="The value it returned")

    def _obj(self, h):
        with open(os.path.join(os.path.dirname(self.path), "objects", h + ".pkl"), "rb") as f:
            return _Unpickler(f, self.meta).load()

    def _load(self, key):
        """Fresh copies every time: {name: value} (for "output": {"output": value})."""
        raw = self._raw[key]
        if self._raw.get("format") != 2:                       # files from older versions
            return _Unpickler(self._io.BytesIO(raw), self.meta).load()
        if key == "output":
            return {"output": self._obj(raw)}
        return {k: self._obj(h) for k, h in raw.items()}

    @property
    def func(self):
        m = self.meta
        if "<locals>" in m["qualname"]:
            raise RuntimeError(f"{m['qualname']} is a nested function - it cannot be re-imported.")
        if m["module"] in ("__main__", None) or (m.get("script") and os.path.abspath(m["file"]) == m["script"]):
            obj = _main_source(m)
        else:
            obj = importlib.import_module(m["module"])
        for part in m["qualname"].split("."):
            obj = getattr(obj, part)
        return obj

    def _args(self, changes):
        m = self.meta
        inp = self._load("inputs")                      # fresh copy every time
        unknown = set(changes) - set(m["names"])
        if unknown:
            raise TypeError(f"{self.qualname}() has no input(s) {sorted(unknown)}; inputs are {m['names']}")
        inp.update(changes)
        names, ac, kc = m["names"], m["argcount"], m["kwonly"]
        args = [inp[n] for n in names[:ac]]
        kwargs = {n: inp[n] for n in names[ac:ac + kc] if n in inp}
        i = ac + kc
        if m["varargs"]:
            args += list(inp.get(names[i], ())); i += 1
        if m["varkw"]:
            kwargs.update(inp.get(names[i], {}))
        # An input that could not be saved: if it is a function that was called during the recording
        # (a callback, e.g. cache.run(step, fn=lambda: ...)), stand in for it with its recorded results.
        named = list(zip(names, range(len(args))))
        for k, i in named:
            if isinstance(args[i], Unsaved):
                args[i] = self._stand_in(k, args[i])
        for k in list(kwargs):
            if isinstance(kwargs[k], Unsaved):
                kwargs[k] = self._stand_in(k, kwargs[k])
        return args, kwargs

    def _descendant_calls(self):
        """Saved calls made inside this call (at any depth), in order, as (id, pkl path)."""
        folder = os.path.dirname(self.path)
        with open(os.path.join(folder, "index.csv"), newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        kids = {}
        for r in rows:
            kids.setdefault(r["parent"], []).append(r)
        out, todo = [], [str(self.id)]
        while todo:
            for r in kids.get(todo.pop(), []):
                todo.append(r["id"])
                if r["pkl"]:
                    out.append((int(r["id"]), os.path.join(folder, r["pkl"])))
        return sorted(out)

    def _stand_in(self, name, u):
        code = getattr(u, "code", None)
        hint = (f"Pass it yourself, e.g. c.rerun({name}=...)"
                + ("; or `pip install cloudpickle` and record again, so it can be saved" if code else ""))
        if not code:
            raise RuntimeError(f"input '{name}' could not be saved ({u.text}). {hint}")
        recorded = []
        for cid, path in self._descendant_calls():
            with open(path, "rb") as f:
                meta = pickle.load(f)["meta"]
            if os.path.abspath(meta["file"]) == code[0] and meta["line"] == code[1]:
                recorded.append(path)
        if not recorded:
            raise RuntimeError(f"input '{name}' could not be saved ({u.text}) and was not called during "
                               f"the recording, so there is nothing to replay. {hint}")
        print(f"[mydbg] input '{name}' ({u.text}) could not be saved; it will return its "
              f"{len(recorded)} recorded result(s) instead of running. {hint}.", file=sys.stderr)
        queue = list(recorded)

        def stand_in(*a, **k):
            if not queue:
                raise RuntimeError(f"'{name}' was called more often than during the recording "
                                   f"({len(recorded)} time(s)); pass a real function: rerun({name}=...)")
            c = Call(queue.pop(0))
            if c.raised:
                raise RuntimeError(f"recorded call #{c.id} of '{name}' raised {c.raised}")
            return c._load("output")["output"]
        stand_in.__name__ = f"recorded_{name}"
        return stand_in

    def _with_globals(self, fn, run, override=None):
        g = fn.__globals__
        saved = self._load("globals")
        saved.update(override or {})
        old = {k: g.get(k, _MISSING) for k in saved}
        g.update({k: v for k, v in saved.items() if not isinstance(v, Unsaved)})
        try:
            return run()
        finally:
            for k, v in old.items():
                if v is _MISSING:
                    g.pop(k, None)
                else:
                    g[k] = v

    def rerun(self, _globals=None, **changes):
        """Run the function again on the saved inputs (and saved globals).
        Change inputs by name: rerun(lags=5).  Change globals: rerun(_globals={"SCENARIO": {...}})."""
        fn = self.func
        args, kwargs = self._args(changes)
        return self._with_globals(fn, lambda: fn(*args, **kwargs), _globals)

    def debug(self, _globals=None, **changes):
        """Step through the function in pdb (library code skipped) with the saved inputs."""
        fn = self.func
        args, kwargs = self._args(changes)
        dbg = ProjectPdb(self.meta["root"])
        return self._with_globals(fn, lambda: dbg.runcall(fn, *args, **kwargs), _globals)

    def check(self, _globals=None, **changes):
        """Rerun and compare with the saved output. Returns True / False and prints the difference."""
        if self.raised:
            try:
                self.rerun(_globals, **changes)
                print(f"Originally raised {self.raised}, now runs without error.\n" + _NOTE)
                return False
            except Exception as e:
                print(f"Raises again: {type(e).__name__}: {e}")
                return True
        new = self.rerun(_globals, **changes)
        ok, msg = _compare(self.output, new)
        print("Same output as recorded." if ok else f"DIFFERENT from recorded output:\n{msg}\n" + _NOTE)
        return ok

    def __repr__(self):
        ins = ", ".join(f"{k}={_short(v, 50)}" for k, v in self.inputs.items())
        gl = ", ".join(f"{k}={_short(v, 50)}" for k, v in self.globals.items()) or "(none)"
        out = f"raised {self.raised}" if self.raised else _short(self.output, 100)
        return (f"Call #{self.id}  {self.qualname}  ({os.path.basename(self.file)}:{self.line}, {self.ms} ms)\n"
                f"  inputs : {ins}\n  globals: {gl}\n  output : {out}\n"
                f"  -> .inputs  .globals  .output  .rerun(**changes)  .check()  .debug()")


_MISSING = object()
_NOTE = ("(If you did not change anything: the function also depends on something that is not saved -\n"
         " e.g. files it reads, sys.argv, random numbers without a seed, the current date, or code you edited since.)")


def _compare(a, b):
    try:
        import pandas as pd
        if isinstance(a, pd.DataFrame) and isinstance(b, pd.DataFrame):
            pd.testing.assert_frame_equal(a, b); return True, ""
        if isinstance(a, pd.Series) and isinstance(b, pd.Series):
            pd.testing.assert_series_equal(a, b); return True, ""
    except ImportError:
        pass
    except AssertionError as e:
        return False, str(e)
    try:
        import numpy as np
        if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
            ok = np.array_equal(np.asarray(a), np.asarray(b), equal_nan=True)
            return bool(ok), "" if ok else f"recorded: {_short(a)}\nnow:      {_short(b)}"
    except (ImportError, TypeError):
        pass
    try:
        ok = bool(a == b)
    except Exception:
        ok = False
    return ok, "" if ok else f"recorded: {_short(a, 300)}\nnow:      {_short(b, 300)}"


def _resolve_dir(folder):
    if folder:
        return folder
    for cand in ("trace_calls",):
        if os.path.isdir(cand):
            return cand
    raise FileNotFoundError("No trace_calls folder here; pass folder=... (the *_calls folder next to the .html)")


def load_call(which, folder=None):
    """load_call(7) or load_call("trace_calls/00007_add_features.pkl")."""
    if isinstance(which, int):
        folder = _resolve_dir(folder)
        hits = [f for f in os.listdir(folder) if f.startswith(f"{which:05d}_") and f.endswith(".pkl")]
        if not hits:
            raise FileNotFoundError(f"call #{which} was not saved in {folder} (see list_calls())")
        which = os.path.join(folder, hits[0])
    return Call(which)


def list_calls(folder=None, function=None):
    """Table of saved calls (a DataFrame if pandas is installed). Filter by function name."""
    folder = _resolve_dir(folder)
    with open(os.path.join(folder, "index.csv"), newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if function:
        rows = [r for r in rows if function in r["function"]]
    try:
        import pandas as pd
        return pd.DataFrame(rows).set_index("id") if rows else pd.DataFrame(rows)
    except ImportError:
        return rows


# --------------------------------------------------------------------- main
def _console_script(name):
    from importlib.metadata import entry_points
    try:
        eps = entry_points(group="console_scripts")
    except TypeError:                                    # Python < 3.10
        eps = entry_points().get("console_scripts", [])
    for ep in eps:
        if ep.name == name:
            return ep
    sys.exit(f"[mydbg] No installed command called '{name}' in this Python ({sys.executable}).\n"
             f"        Activate the environment where '{name}' is installed, or give a .py file / -m module.")


def _main():
    a = sys.argv[1:]
    if len(a) < 2 or a[0] not in ("step", "record"):
        print(__doc__); sys.exit(1)
    mode = a.pop(0)
    opts = {"--out": "trace.html", "--root": None, "--max-calls": "20", "--skip": "",
            "--max-lines": "2000", "--rows": "5", "--max-mb": "200"}
    calls_only = False
    while a and (a[0] in opts or a[0] == "--calls-only"):
        if a[0] == "--calls-only":
            calls_only = True; a = a[1:]
        else:
            opts[a[0]] = a[1]; a = a[2:]

    script = main_module = None
    if a[0] == "-m":                                     # python mydbg.py record -m pkg.cli args
        main_module = a[1]
        kind, target_file = "module", None
        spec = importlib.util.find_spec(main_module)
        target_file = spec.origin if spec else None
        sys.argv = [target_file or main_module] + a[2:]
        title = "python -m " + " ".join([main_module] + a[2:])
    elif a[0].endswith(".py") or os.path.isfile(a[0]):  # python mydbg.py record my_model.py args
        kind = "script"
        script = target_file = os.path.abspath(a[0])
        sys.argv = a
        title = " ".join([os.path.basename(script)] + a[1:])
    else:                                                # python mydbg.py record creditloss run ...
        kind = "command"
        ep = _console_script(a[0])
        sys.argv = a
        title = " ".join(a)
    root = opts["--root"] or (os.path.dirname(script) if script else os.getcwd())
    sys.path.insert(0, os.path.dirname(script) if script else os.getcwd())

    if kind == "script":
        # run the script as a real __main__ module so its classes/functions pickle by name
        main_mod = types.ModuleType("__main__")
        main_mod.__file__ = script
        main_mod.__builtins__ = __builtins__
        sys.modules["__main__"] = main_mod
        glb = main_mod.__dict__
        with open(script, "rb") as f:
            code = compile(f.read(), script, "exec")
        run = lambda: exec(code, glb)
    elif kind == "module":
        import runpy
        run = lambda: runpy.run_module(main_module, run_name="__main__", alter_sys=True)
    else:
        func = ep.load()                                 # imports the package (not recorded)
        target_file = getattr(sys.modules.get(func.__module__), "__file__", None)
        main_module = None

        def run():
            rv = func()
            if rv not in (None, 0):
                raise SystemExit(rv)

    if target_file and not _Filter(root)(target_file):
        print(f"[mydbg] WARNING: {target_file}\n"
              f"        is outside --root {root}, so its code will not be shown.\n"
              f"        Run from your project folder, or add --root <folder that contains it>.\n"
              f"        (If the package was installed with plain 'pip install .', reinstall it with 'pip install -e .')",
              file=sys.stderr)

    if mode == "step":
        dbg = ProjectPdb(root)
        if kind == "script":
            dbg.run(code, glb)
        else:
            dbg.runcall(run)
        return

    data_dir = os.path.splitext(opts["--out"])[0] + "_calls"
    os.makedirs(data_dir, exist_ok=True)
    for fn in os.listdir(data_dir):                      # clear the previous run
        if fn.endswith(".pkl") or fn == "index.csv":
            os.remove(os.path.join(data_dir, fn))
    obj_dir = os.path.join(data_dir, "objects")
    if os.path.isdir(obj_dir):
        for fn in os.listdir(obj_dir):
            if fn.endswith(".pkl"):
                os.remove(os.path.join(obj_dir, fn))
    rec = Recorder(root, script, data_dir, int(opts["--max-calls"]), int(opts["--max-lines"]),
                   int(opts["--rows"]), float(opts["--max-mb"]), title=title, main_module=main_module)
    rec.calls_only = calls_only
    rec.skip = [p.strip().replace("\\", "/") for p in opts["--skip"].split(",") if p.strip()]
    print(f"[mydbg] recording{' (calls only)' if calls_only else ''}... progress every 5 s; "
          f"the report is written at the end", file=sys.stderr)
    t0, crash = time.perf_counter(), None
    rec.t_start = rec.last_progress = t0
    sys.settrace(rec.global_trace)
    try:
        run()
    except SystemExit as e:                              # CLIs (click, argparse) exit this way
        if e.code not in (None, 0):
            crash = f"SystemExit({e.code})"
        raise
    except BaseException as e:
        crash = f"{type(e).__name__}: {e}"
        raise
    finally:
        sys.settrace(None)
        if sys.stderr.isatty():
            sys.stderr.write("\n")
        print("[mydbg] writing report...", file=sys.stderr)
        rec.write_index()
        rec.write_html(opts["--out"], time.perf_counter() - t0, crash)
        print(f"[mydbg] report: {os.path.abspath(opts['--out'])}\n"
              f"[mydbg] {len(rec.index)} calls saved in {os.path.abspath(data_dir)} "
              f"({rec.bytes_written / 1e6:,.0f} MB)", file=sys.stderr)


if __name__ == "__main__":
    # Re-import this file as the module "mydbg", so saved objects never refer to "__main__.<our class>".
    _spec = importlib.util.spec_from_file_location("mydbg", _THIS)
    _m = importlib.util.module_from_spec(_spec)
    sys.modules["mydbg"] = _m
    _spec.loader.exec_module(_m)
    _m._main()
