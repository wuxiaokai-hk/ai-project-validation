# mydbg user guide

`mydbg` is one Python file that lets you review a project through its own code only: pandas, numpy and
other libraries are skipped. It has three modes.

| Mode | Command | Use it to |
|---|---|---|
| Step | `python mydbg.py step my_model.py` | Walk through the code line by line, like pdb, where `s` only enters the project's own functions |
| Record | `python mydbg.py record my_model.py` | Run the whole project and get a clickable HTML report of every call, with its inputs and outputs, plus each call saved to disk |
| Replay | `load_call(24)` in Python or Jupyter | Load one saved call, inspect its inputs and output, rerun it, check it, or step through it |

"The project's code" means any `.py` file under the project folder (by default, the folder of the script
you run). Files in site-packages, virtual environments and Python's own internals are never shown.

## Setup

There is nothing to install: mydbg needs only Python 3.10 or later. pandas and numpy are optional; they are
used when present to preview DataFrames and arrays.

Where to put `mydbg.py`:

- **Simplest:** copy it into the project's main folder. It leaves itself out of every recording, so it never
  appears in your traces.
- **One copy for all projects:** keep it in a tools folder, such as `C:\tools`, and run it by path
  (`python C:\tools\mydbg.py record my_model.py`). To import it in a notebook, add the folder first:

  ```python
  import sys; sys.path.insert(0, r"C:\tools")
  from mydbg import load_call, list_calls
  ```
- **Importable everywhere:** `pip install` this repository (it adds a `mydbg` command), or set the
  `PYTHONPATH` environment variable to the tools folder once.

Always run the commands from the folder where you want the report and the saved calls to go. Usually that
is the project folder.

## What you can run

After `step` or `record` you can give a script, a module, or an installed command. Everything after the
target is passed to it unchanged.

| Target | Example |
|---|---|
| A script | `python mydbg.py record my_model.py --year 2026` |
| An installed command (a CLI) | `python mydbg.py record mytool run --config config/base.yaml` |
| A module | `python mydbg.py record -m mytool.cli run --config config/base.yaml` |

For an installed command:

- Activate the same environment where the command is installed, and run from the project folder. That
  folder counts as the project's code.
- Install the package in editable mode (`pip install -e .`). Otherwise Python runs a copy in
  site-packages, and mydbg warns that no code will be shown.
- A normal exit (`sys.exit(0)`) is not an error. Only a real failure shows the red crash banner.

## Step mode

Step mode is pdb that only stops in the project's own files. Pressing `s` on a line that calls pandas
skips pandas entirely. If pandas then calls one of the project's functions (for example
`df.apply(my_func)`), you land on the first line of `my_func`.

```
python mydbg.py step my_model.py [script arguments]
```

To start at a particular line instead of the top, put this line in the code and run the script normally:

```python
from mydbg import set_trace; set_trace()
```

| Command | What it does |
|---|---|
| `n` | Run the next line (does not go into functions) |
| `s` | Step into the function on this line (project code only) |
| `r` | Run to the end of the current function and show its return value |
| `c` | Continue until the next breakpoint or the end |
| `l` / `ll` | Show code around the current line / the whole function |
| `p x` / `pp x` | Print a variable / print it formatted |
| `b file.py:42` | Set a breakpoint at line 42 of file.py |
| `u` / `d` | Move up / down the call stack |
| `w` | Show where you are in the call stack |
| `q` | Quit |

Any Python expression also works at the prompt, for example `df.head()` or `df.shape`.

## Record mode

Record mode runs the whole project once and produces a report you open in a browser, plus a folder with
every call saved.

```
python mydbg.py record my_model.py [script arguments]
```

Outputs, in the folder you run it from:

- `trace.html`: the report.
- `trace_calls/`: one `.pkl` file per recorded call, plus `index.csv` listing them all. Each new run clears
  this folder first.

Options go before the script name, for example
`python mydbg.py record --max-calls 50 --out baseline.html my_model.py`.

| Option | Default | What it controls |
|---|---|---|
| `--out FILE` | `trace.html` | Report name; the data folder is named after it (`baseline.html` gives `baseline_calls/`) |
| `--root DIR` | script's folder | Which folder counts as the project's code; use the parent folder if helper modules sit above the script |
| `--max-calls N` | 20 | Calls per function recorded and saved; later calls are counted only. 0 = all |
| `--max-lines N` | 2000 | Code lines shown per call in the report |
| `--calls-only` | off | Record only calls with their inputs and outputs, no code lines: the fastest mode |
| `--skip A,B` | none | Leave out files or functions (e.g. `src/utils/trace.py,log`); calls made inside them are still recorded |
| `--max-mb N` | 200 | A single object larger than this is not saved |
| `--rows N` | 5 | DataFrame rows shown in previews |

### Speed

Recording is slower than a normal run, and the report is written only at the end. A progress line every 5
seconds shows the elapsed time and the function currently running.

`--calls-only` records only the function calls with their inputs and outputs, not the code lines. It's the
fastest mode, and enough for most reviews: `python mydbg.py record --calls-only run_pipeline.py`.

Identical objects are saved once. A DataFrame passed unchanged to 20 calls takes disk space once, not 20
times. `--max-mb` applies to each single object: an object larger than the limit is shown in the report but
replaced by "not saved" in the saved call.

### Reading the report

- Each function call is one row, such as `#22 add_features(df=DataFrame(100, 3), lags=3) → DataFrame(100, 7)`,
  with its file, line and run time. Click to open or close it. Calls made inside it are nested.
- **INPUTS** lists the arguments as they were when the call started. Globals the function read are tagged
  *global*. Click a DataFrame to see its first rows and column types.
- **Code lines** run in order. Under each line, the variables it changed are shown in amber.
- **OUTPUT** shows the return value, or the error in red. The replay line has a copy button that copies the
  `load_call` line for that call.
- **Toolbar:** Expand all, Collapse all, Hide code lines (calls with inputs and outputs only), and a box to
  find a function by name.
- If the script crashes, the report is still written. A red banner shows the error, and the calls that led
  to it have red borders.

Run times include the slowdown from recording. Use them to compare calls, not as real timings.

## Replay saved calls

Every call recorded in full can be loaded in Python, Jupyter or Spyder with its real input objects and
output, then rerun, checked or stepped through.

```python
from mydbg import load_call, list_calls

list_calls()                          # table of all saved calls
list_calls(function="add_features")   # only one function

c = load_call(22)                     # by call number (the # in the report)
c = load_call("trace_calls/00022_add_features.pkl")   # or by file (the copy button)
c                                     # prints a summary: inputs, globals, output
```

| Use | What you get |
|---|---|
| `c.inputs` | Dict of the arguments, as they were when the call started |
| `c.globals` | Global variables the function read, such as a `SCENARIO` setting |
| `c.output` | The value it returned |
| `c.raised` | The error text, if the call failed |
| `c.rerun()` | Run the function again on the saved inputs and globals; returns the new result |
| `c.rerun(lags=5)` | Rerun with some inputs changed, by argument name |
| `c.rerun(_globals={"SCENARIO": {"shock": 0.10}})` | Rerun with a different global value |
| `c.check()` | Rerun and compare with the saved output; prints the difference and returns True or False |
| `c.debug()` | Open step mode at the function's first line, with the saved inputs |
| `c.func` | The function itself, imported from the current code |

The current code is used for every rerun. After you edit a function, `c.check()` shows at once whether its
result changed.

`load_call(22)` looks for `trace_calls` in the current folder. From anywhere else, use
`load_call(22, folder="path/to/trace_calls")`, and the same `folder=` for `list_calls`.

## Typical workflows

### Review a project someone (or an AI) built

1. Run `python mydbg.py record my_model.py`.
2. Open `trace.html` and click **Hide code lines** to see only the calls, each with its inputs and output.
3. Open the calls whose outputs look wrong, and show the code lines again to see which line changed what.
4. For a call you want to probe, copy its replay line into Jupyter and try other inputs with `c.rerun(...)`.

### Find out why it crashed

1. Record it. The report is written even when the script fails.
2. Follow the red calls from the top down to the innermost one; that is where the error happened.
3. Load that call and run `c.debug()` to step through it with the exact inputs that failed, without
   rerunning the whole project.

### Check that an edit changed nothing else

1. Record a baseline before editing: `python mydbg.py record --out baseline.html my_model.py`.
2. Edit the code (or let an AI assistant edit it).
3. Load the calls of the functions that were touched, from `baseline_calls`, and run `c.check()` on each.
   "Same output as recorded" means the edit didn't change that result.

## Limits and troubleshooting

- Only the main script's imports, functions and classes are loaded on replay. Its top-level code (data
  loading, plotting) does not run again.
- Saved files contain your real data. Delete the `*_calls` folder when you're done if the data is
  sensitive, and don't load `.pkl` files from someone you don't trust: loading one can run code.

| Situation | What to do |
|---|---|
| Recording is slow, or the report is huge | Record a smaller sample of the data, or use `--calls-only`. Long loops in the project's code log every pass; lower `--max-lines` or `--max-calls` |
| A function shows "… 80 more calls not recorded" | Only the first 20 calls per function are kept. Use `--max-calls 0` to keep all of them, but watch the disk space |
| `check()` says DIFFERENT although nothing was edited | The function depends on something not saved: files it reads, command-line arguments, unseeded random numbers, today's date |
| An input shows `<not saved: …>` | The object can't be saved, such as a database connection or an open file. Pass it yourself: `c.rerun(conn=my_conn)` |
| "nested function - cannot be re-imported" | Functions defined inside another function can be recorded but not replayed. Move it to the top level of a module to replay it |
| A helper module doesn't appear in the report | It sits outside the script's folder. Add `--root` with the folder that contains all the project's code |
| `load_call(22)` can't find the call | Run it from the folder with `trace_calls`, add `folder=`, or paste the full line from the report's copy button |
| The `trace_calls` folder was moved | Use `load_call(22, folder="new/path")`; the paths printed in the report are absolute |
