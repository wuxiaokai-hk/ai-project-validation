"""End-to-end tests: record the example project, replay calls, fix the planted bug, check what changed."""
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MYDBG = os.path.join(ROOT, "mydbg.py")
EXAMPLE = os.path.join(ROOT, "examples", "loan_model")


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A fresh copy of the example project, recorded once as the baseline."""
    dst = tmp_path / "loan_model"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns("trace*", "baseline*", "__pycache__"))
    r = subprocess.run([sys.executable, MYDBG, "record", "--out", "baseline.html", "model.py"],
                       cwd=dst, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    monkeypatch.chdir(dst)
    monkeypatch.syspath_prepend(str(dst))
    monkeypatch.syspath_prepend(ROOT)
    for m in ("model", "helpers", "mydbg"):
        sys.modules.pop(m, None)
    return dst


def test_record_writes_report_and_calls(project):
    html = (project / "baseline.html").read_text(encoding="utf-8")
    assert "monthly_payment" in html and "expected_loss" in html
    index = (project / "baseline_calls" / "index.csv").read_text().splitlines()
    assert len(index) == 1 + 10          # header + portfolio_report + 3 loans x 3 helpers


def test_library_code_is_not_recorded(project):
    index = (project / "baseline_calls" / "index.csv").read_text()
    assert "site-packages" not in index and "mydbg" not in index


def test_replay_reproduces_and_exposes_the_bug(project):
    from mydbg import load_call
    c = load_call(2, folder="baseline_calls")              # first monthly_payment call
    assert c.inputs == {"balance": 250_000, "annual_rate": 0.055, "years": 25}
    assert c.check()                                         # the recorded result reproduces
    # A 25-year C$250k mortgage at 5.5% costs about C$1,535 a month, not C$13,750:
    assert c.output > 10_000
    # Rerunning with the monthly rate gives the right payment, so the rate is the problem:
    assert round(c.rerun(annual_rate=0.055 / 12), 2) == 1535.22


def test_fix_changes_only_the_buggy_function(project, capsys):
    src = (project / "model.py").read_text()
    (project / "model.py").write_text(src.replace("r = annual_rate ", "r = annual_rate / 12 "))
    sys.modules.pop("model", None)
    from mydbg import load_call
    assert not load_call(2, folder="baseline_calls").check()          # payment changed: the fix
    assert round(load_call(2, folder="baseline_calls").rerun(), 2) == 1535.22
    for n in (3, 4, 6, 7, 9, 10):                                     # helpers: unchanged
        assert load_call(n, folder="baseline_calls").check()


def test_crash_still_writes_report(tmp_path):
    (tmp_path / "bad.py").write_text("def f(x):\n    return 1 / x\n\nf(0)\n")
    r = subprocess.run([sys.executable, MYDBG, "record", "bad.py"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode != 0
    html = (tmp_path / "trace.html").read_text(encoding="utf-8")
    assert "ZeroDivisionError" in html


PIPE = '''
class StepCache:
    def run(self, step, fn):
        return fn()

def evaluate(x, lgd):
    return x * lgd

def main():
    data = 100.0
    return StepCache().run("evaluate", lambda: evaluate(data, 0.45))

main()
'''


def _record(tmp_path, env=None):
    (tmp_path / "pipe.py").write_text(PIPE)
    r = subprocess.run([sys.executable, MYDBG, "record", "pipe.py"], cwd=tmp_path,
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr


def test_unsaved_callback_is_replayed_from_its_recorded_result(tmp_path, monkeypatch):
    # Simulate an environment without cloudpickle: the lambda input cannot be saved.
    (tmp_path / "cloudpickle.py").write_text("raise ImportError('not installed')\n")
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    _record(tmp_path, env)
    monkeypatch.syspath_prepend(ROOT)
    sys.modules.pop("mydbg", None)
    from mydbg import Unsaved, load_call
    c = load_call(2, folder=str(tmp_path / "trace_calls"))      # StepCache.run(step, fn)
    assert isinstance(c.inputs["fn"], Unsaved)
    assert c.rerun() == 45.0                # stand-in returns the lambda's recorded result
    assert c.check()
    assert c.rerun(fn=lambda: 1.0) == 1.0   # or pass your own function


def test_report_shows_output_before_code_and_has_search(tmp_path):
    _record(tmp_path)
    html = (tmp_path / "trace.html").read_text(encoding="utf-8")
    block = html[html.index('id="c2"'):]
    assert block.index("OUTPUT") < block.index("CALLS MADE") < block.index("Code, line by line")
    assert 'id="q"' in html and "function search(" in html
