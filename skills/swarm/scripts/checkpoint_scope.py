"""Checkpoint helpers: find the run's plan when --plan is left out, match --include paths, report out-of-scope files.

resolve_plan(given, tids, pool) -> (plan or None, source): `given` as is; else the plan the run recorded (its
checkpoints.json, the pool, the agent plan beside the approved review plan), used verbatim so the checkpoint chain
continues; else next_tasks.DEFAULT_PLAN when it exists; else the one docs/plans/*.md holding every task heading.
warn_out_of_scope(tid, files, log): print warnings and the .gitignore hint to stderr, log one noted incident per
top-level directory. Out-of-scope files never refuse a checkpoint. Stdlib only.
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import base  # noqa: E402
import next_tasks as nt  # noqa: E402
import state  # noqa: E402

OUT_OF_SCOPE_SHOWN = 20  # paths printed per checkpoint
HINT_DIR_FILES = 20  # more out-of-scope files than this under one top-level directory: print the .gitignore hint
INCIDENT_PATHS = 5  # paths named in each grouped incident
COMPANION = re.compile(r"\.(review|feature|mvp|progress)\.md$")  # never the agent plan itself


def _recorded(path, key):
    d = base.read_json(Path(path), {})
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, str) and v else None


def has_tasks(path, tids):  # O(L)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    return all(re.search(rf"^#{{2,6}}\s+{re.escape(t)}\b", text, re.M) for t in tids if t != "T0")


def guess_plan(tids):  # O(P*L)
    """The one docs/plans/*.md (not a companion) with a heading for every task; None when there are none or several."""
    found = [p for p in sorted(Path("docs/plans").glob("*.md")) if not COMPANION.search(p.name) and has_tasks(p, tids)]
    return found[0].as_posix() if len(found) == 1 else None


def resolve_plan(given, tids, pool):  # O(P*L)
    if given:
        return given, ""
    sl = Path(state.DIR_NAME)
    review = _recorded(sl / "orchestrator.json", "review_path")
    agent_plan = COMPANION.sub(".md", review) if review and COMPANION.search(review) else None
    for src, p in (("checkpoints.json", _recorded(sl / "checkpoints.json", "plan")), ("pool", _recorded(pool, "plan")),
                   ("review plan", agent_plan), ("default", nt.DEFAULT_PLAN)):
        if p and Path(p).is_file():
            return p, src
    guess = guess_plan(tids)
    return (guess, "docs/plans") if guess else (None, "")


def norm_path(p):  # O(len p)
    """A --include path as the repo-relative posix path git status prints."""
    return os.path.relpath(os.path.abspath(p)).replace("\\", "/").rstrip("/")


def include_match(includes):
    return lambda path: any(path == i or path.startswith(i + "/") for i in includes)


def group_by_top_dir(files):  # O(m)
    """{top-level directory: [paths]} in first-seen order; a file at the repo root is its own group."""
    groups = {}
    for f in files:
        groups.setdefault(f.split("/", 1)[0] if "/" in f else f, []).append(f)
    return groups


def incident_row(key, files):
    """(file, line, message) for one group: the path itself for a lone file, else `dir/` with the count and the first paths."""
    msg = "change outside Owns left out of the checkpoint"
    if len(files) == 1 and files[0] == key:
        return key, 0, msg
    first = ", ".join(files[:INCIDENT_PATHS])
    return key + "/", 0, f"{msg}: {len(files)} files under {key}/, first {min(len(files), INCIDENT_PATHS)}: {first}"


def warn_out_of_scope(tid, files, log):  # O(m) + one append
    for f in files[:OUT_OF_SCOPE_SHOWN]:
        print(f"warning: out-of-scope {f} (left out of {tid}; --include it if {tid} changed it)", file=sys.stderr)
    if len(files) > OUT_OF_SCOPE_SHOWN:
        print(f"warning: ... and {len(files) - OUT_OF_SCOPE_SHOWN} more", file=sys.stderr)
    groups = group_by_top_dir(files)
    for d, fs in groups.items():
        if len(fs) > HINT_DIR_FILES:
            print(f"hint: {len(fs)} out-of-scope files under {d}/; likely a dependency or build folder; add it to .gitignore", file=sys.stderr)
    nt.add_incidents(log, [(f, line, f"{tid}: {msg}", "exception", "checkpoint", "noted")
                           for f, line, msg in (incident_row(d, fs) for d, fs in groups.items())])
