# ai-project-validation: check what an AI-written Python project actually does

AI assistants now write whole analysis projects: models, pipelines, data tools. The code usually runs and
looks plausible, which is exactly the problem. Reading it top to bottom tells you what it *says*; it does not
tell you what it *did* on your data.

`mydbg` is a single-file tool for validating such a project from the outside:

- **Record** one run and get a clickable HTML report of every call of the project's own functions, with the
  real inputs and outputs, and the variables each line changed. pandas, numpy and other libraries are left
  out, so you see only the logic you need to check.
- **Replay** any recorded call in Python or Jupyter with its exact inputs: rerun it, change one input, or
  step through it line by line.
- **Check** an edit, whether yours or the AI's: record a baseline, change the code, and `c.check()` tells you
  which results moved and which stayed exactly the same.

It works on any Python project, not only AI-written ones: a model someone handed over, a vendor script, your
own code from last year.

## Quick start

```bash
git clone https://github.com/wuxiaokai-hk/ai-project-validation
cd ai-project-validation/examples/loan_model
python ../../mydbg.py record model.py          # -> trace.html and trace_calls/
```

Open `trace.html` in a browser. Nothing to install: Python 3.10+ and the one file `mydbg.py`. pandas and
numpy are optional (used for DataFrame and array previews). `pip install .` adds a `mydbg` command if you
prefer.

## A worked example: a bug that runs cleanly

[`examples/loan_model`](examples/loan_model) is a small loan-loss model of the kind an assistant writes
when asked. It runs without errors and prints sensible-looking numbers:

```
{'id': 'A1', 'payment': 13750.0, 'pd_stressed': 0.025, 'expected_loss': 1250.0}
...
total expected loss: 5240.0
```

**1. Record and read the calls.** In the report, call `#2 monthly_payment(balance=250000, annual_rate=0.055,
years=25) → 13750.0` stands out: a C$250,000 mortgage at 5.5% over 25 years costs about C$1,535 a month,
not C$13,750. Opening the call shows the line `r = annual_rate`: the annual rate is used as a monthly rate.

**2. Test the hypothesis on that one call**, without rerunning the project:

```python
from mydbg import load_call
c = load_call(2)
c.check()                         # True: the recorded result reproduces
c.rerun(annual_rate=0.055 / 12)   # 1535.22: the monthly rate gives the right payment
```

**3. Fix it and prove nothing else moved.** Record a baseline first
(`python ../../mydbg.py record --out baseline.html model.py`), change the line to `r = annual_rate / 12`,
then:

```python
for n in range(1, 11):
    load_call(n, folder="baseline_calls").check()
```

Only the `monthly_payment` calls (and `portfolio_report`, which contains them) report DIFFERENT; every
`stress_pd` and `expected_loss` call reports "Same output as recorded". The tests in
[`tests/`](tests/test_mydbg.py) run exactly this sequence.

## Three modes

| Mode | Command | Use it to |
|---|---|---|
| Step | `python mydbg.py step my_model.py` | Walk through line by line, like pdb, where `s` only enters the project's own functions |
| Record | `python mydbg.py record my_model.py` | Run once and get the HTML report of every call, plus each call saved to disk |
| Replay | `load_call(24)` in Python or Jupyter | Inspect one call's inputs and output, rerun it, check it, or step through it |

The target can be a script, a module (`-m pkg.cli ...`) or an installed command (`mydbg record mytool run
--config cfg.yaml`). For large projects, `--calls-only` records calls with their inputs and outputs but not
each line, which is much faster. All options, the report, replay and troubleshooting are in the
**[user guide](docs/USER_GUIDE.md)**.

## A checklist for validating an AI-written project

1. **Record a representative run** on real (or realistic) data, not the toy data the assistant made up.
2. **Read the call tree before the code.** Is every function you expect actually called? Are there calls you
   didn't expect (a fallback path, a silently caught error, a default value used instead of your input)?
3. **Sanity-check outputs at each step** against a number you know: a payment, a total, a row count, a sign.
   The first call whose output is wrong is where to look.
4. **Probe one call at a time** with `c.rerun(...)`, changing a single input, instead of rerunning the
   whole project.
5. **Freeze a baseline before every edit**, by you or the assistant, and use `c.check()` to confirm that
   only the intended results changed.
6. **Watch for hidden inputs:** if `c.check()` fails without an edit, the function depends on something
   not passed in (a file, the date, unseeded randomness, a global). That is worth knowing in itself.

## Tests

```bash
pip install pytest
pytest -q
```

## Limits

- Python only. The recording slows the run down; record a sample of the data for big jobs.
- Saved calls contain your real data, as pickle files. Delete the `*_calls` folder when done if the data is
  sensitive, and never load `.pkl` files from a source you don't trust: loading one can run code.

## License

MIT, see [LICENSE](LICENSE). Free to use, change and share.
