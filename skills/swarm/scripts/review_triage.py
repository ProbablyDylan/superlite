"""Incident triage for the review packet: which open rows a later event resolved, which may be stale, and the
normalised message each row collapses on. classify() is pure over the rows and a Facts; the loaders at the end
(load_facts, changed_files) read the run folder's checkpoints.json and git.

Resolved (left out of the open list; the packet lists them briefly so the reviewer can spot-check):
  - a later `verified` row (log order: the log is append-only, `incident resolve` rewrites in place) for the same
    file from the same writer: run_tests for a test failure (all the file's tests pass) or probe_run (the same
    probe category); for a seam mismatch, a later `verified: seam Ta↔Tb` row for the same seam;
  - a run_tests failure the newest finished test summary provably re-ran green: tests ran (passed > 0, no hang,
    every failure a `file::test` line), mode `full` or the file in `selected`, and the test not among its failures;
  - a checkpoint row for task Tn when Tn's newest checkpoint commit is later than the row's date and time.
Possibly stale (kept and counted, marked): a checkpoint row whose order against Tn's checkpoint cannot be told
(no date on the row, no commit time, or the same second), a later `verified` row from another agent for the same
file, or a row whose file no longer exists.
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import base  # noqa: E402
import state  # noqa: E402

RESOLVED = {"fixed", "resolved", "closed", "wontfix", "accepted"}
_TASK = re.compile(r"^(T\d+\w*):\s+")
_PROBE = re.compile(r"^probe (\w+) (\S+) (.*?)(?: \(size .*\))?(?:; test \S+)?$")
_TEST = re.compile(r"^(\S+?): (.+)$")
_SEAM = re.compile(r"^(?:verified: )?(seam [^\s:]+)")


@dataclass
class Facts:
    summary: dict | None = None  # the newest finished test summary, None when running or missing
    checkpoints: dict[str, float | None] = field(default_factory=dict)  # task -> newest checkpoint epoch
    exists: Callable[[str], bool] = lambda _f: True


def message(e: dict) -> tuple[str, str]:  # O(len(context))
    """(normalised message, who): `who` is what repeats of one problem differ by (task, test or probe category)."""
    ctx = " ".join(str(e.get("context", "")).split())
    agent = e.get("agent")
    m = _PROBE.match(ctx) if agent == "probe_run" else None
    if m:
        return f"probe {m[2]} {m[3]}", m[1]
    m = _TEST.match(ctx) if agent == "run_tests" and e.get("kind") == "test-failure" else None
    if m:
        return m[2], m[1]
    m = _TASK.match(ctx)
    return (ctx[m.end():], m[1]) if m else (ctx, "")


def is_unresolved(e: dict) -> bool:
    return e.get("kind") != "verified" and str(e.get("resolution", "")).lower() not in RESOLVED


def _probe_cat(e: dict) -> str:
    m = re.match(r"^probe (\w+)", str(e.get("context", "")))
    return m[1] if m else ""


def _writer(e: dict) -> tuple[str, str]:
    """What a `verified` row proves: run_tests per file, probe_run per file and category."""
    agent = str(e.get("agent") or "")
    return agent, _probe_cat(e) if agent == "probe_run" else ""


def _logged_at(e: dict) -> float | None:  # local date + time of the row; None without a date
    try:
        return time.mktime(time.strptime(f"{e['date']} {e['time']}", "%Y-%m-%d %H:%M:%S"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _summary_passes(e: dict, s: dict | None) -> bool:
    """The newest test run covered this failure's file and the test is not failing there."""
    if not s or e.get("agent") != "run_tests" or e.get("kind") != "test-failure":
        return False
    fails = [str(f) for f in s.get("failures") or []]
    if not int(s.get("passed") or 0) or s.get("suspect_hang") or any("::" not in f for f in fails):
        return False  # a crashed, hung or empty run proves nothing
    covered = s.get("mode") == "full" or e["file"] in (s.get("selected") or [])
    name = message(e)[1]
    failing = any(f.startswith(f"{e['file']}::{name}") for f in fails)
    return bool(covered and name and not failing)


def _checkpoint_note(e: dict, facts: Facts) -> str:
    task = message(e)[1]
    if e.get("agent") != "checkpoint" or task not in facts.checkpoints:
        return ""
    saved, logged = facts.checkpoints[task], _logged_at(e)
    if saved is None or logged is None or saved == logged:
        return f"stale: {task} has a checkpoint; cannot tell if it came after this"
    return f"resolved: {task} checkpointed after this" if saved > logged else ""


def classify(entries: list[dict], facts: Facts | None = None) -> list[str]:  # O(K) total
    """One note per row: "" (open, or not an open incident), "resolved: why" or "stale: why"."""
    facts = facts or Facts()
    same: dict[tuple, int] = {}
    other: dict[str, tuple[int, str]] = {}
    for i, e in enumerate(entries):
        m = _SEAM.match(str(e.get("context", ""))) if e.get("kind") == "verified" else None
        if m:
            same[("", "seam", m[1])] = i
        if e.get("kind") == "verified" and e.get("file"):
            same[(e["file"], *_writer(e))] = i
            other[e["file"]] = (i, str(e.get("agent") or "an agent"))
    notes = []
    for i, e in enumerate(entries):
        notes.append(_note(i, e, facts, same, other) if is_unresolved(e) else "")
    return notes


def _note(i: int, e: dict, facts: Facts, same: dict, other: dict) -> str:
    agent = str(e.get("agent") or "")
    proves = agent == "probe_run" or (agent == "run_tests" and e.get("kind") == "test-failure")
    if proves and same.get((e["file"], agent, _probe_cat(e) if agent == "probe_run" else ""), -1) > i:
        return f"resolved: a later `verified` row from {agent} for this file"
    seam = _SEAM.match(str(e.get("context", "")))
    if seam and same.get(("", "seam", seam[1]), -1) > i:
        return f"resolved: {seam[1]} verified later"
    if _summary_passes(e, facts.summary):
        return f"resolved: the newest test run ({facts.summary.get('mode')}) passes it"
    note = _checkpoint_note(e, facts)
    if note:
        return note
    j, who = other.get(e["file"], (-1, ""))
    if j > i:
        return f"stale: {who} logged `verified` for this file later"
    f = e["file"]
    if f and f != "-" and not f.endswith("/") and not facts.exists(f):
        return "stale: the file no longer exists"
    return ""


# --------------------------------------------------------------------------- loaders (run folder and git)

def checkpoint_entries(sl: Path) -> list[dict]:  # O(file size)
    try:
        st = json.loads((sl / "checkpoints.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = st.get("entries") if isinstance(st, dict) else None
    return [e for e in rows or [] if isinstance(e, dict) and e.get("task") and e.get("sha")]


def checkpoint_times(root: Path, entries: list[dict]) -> dict[str, float | None]:  # one git spawn
    """Task -> commit time of its newest checkpoint; None when git cannot tell."""
    shas = [str(e["sha"]) for e in entries]
    times: dict[str, float] = {}
    if shas:
        try:
            r = base.git("show", "-s", "--format=%H %ct", *shas, cwd=root, check=False)
            times = {h: float(t) for h, t in (ln.split() for ln in r.stdout.splitlines() if len(ln.split()) == 2)}
        except (base.GitError, ValueError):
            times = {}
    out: dict[str, float | None] = {}
    for e in entries:
        t, prev = times.get(str(e["sha"])), out.get(e["task"])
        out[e["task"]] = None if t is None else max(t, prev or t)
    return out


def load_facts(root: Path, sl: Path, summary: dict | None) -> Facts:
    def exists(f: str) -> bool:
        return (root / f).exists() or (root / "src" / f).exists()
    return Facts(summary, checkpoint_times(root, checkpoint_entries(sl)), exists)


def changed_files(root: Path, sl: Path, ref: str | None) -> list[str]:  # O(files) + one git spawn
    """Files the run changed: every checkpoint's files plus `git diff --name-only <ref>` (new files are untracked,
    so the checkpoints are what name them). Run-folder paths are left out."""
    names = {str(f) for e in checkpoint_entries(sl) for f in e.get("files") or []}
    if ref and (root / ".git").exists():
        try:
            r = base.git("diff", "--name-only", ref, cwd=root, check=False)
            names.update(r.stdout.splitlines() if r.returncode == 0 else [])
        except base.GitError:
            pass
    return sorted(n for n in names if n.strip() and not n.startswith(state.DIR_NAME))
