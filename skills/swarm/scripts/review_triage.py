"""Incident triage for the review packet: which open rows a later event resolved, which may be stale, and the
normalised message each row collapses on. classify() is pure over the rows and a Facts; the loaders at the end
(load_facts, changed_files) read the run folder's checkpoints.json and git.

Resolved (left out of the open list; the packet lists them briefly so the reviewer can spot-check):
  - a later `verified` row (log order: the log is append-only, `incident resolve` rewrites in place) for the same
    file from the same writer: run_tests for a test failure (all the file's tests pass) or probe_run (the same
    probe category); for a seam mismatch, a later `verified: seam Ta↔Tb` row for the same seam;
  - a run_tests failure the newest finished test summary provably re-ran green: the run started (its run_id time)
    after the row's date and time, tests ran (passed > 0, no hang), nothing failed (failed 0, no failures, so no
    crash and no -x stop), nothing skipped without a name and the test not skipped, mode `full` or the file in
    `selected`. The summary has no per-test pass list, so this is as close as it gets; when unsure, it stays open;
  - a failed-checkpoint row (`...; nothing saved`) for task Tn when Tn's newest checkpoint commit is later than the
    row's date and time. Out-of-scope notes (`change outside Owns left out...`) are never resolved by a checkpoint.
Possibly stale (kept and counted, marked): a failed-checkpoint row whose order against Tn's checkpoint cannot be told
(no date on the row, no commit time, or the same second), a later `verified` row from the same agent for the same
file (run_tests excepted: its rows prove only tests pass), or a row whose file no longer exists.
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
_RUN_ID = re.compile(r"^(\d{8}T\d{6})-")  # run_tests.new_run_id: local start time, pid, random


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


def _started_at(s: dict) -> float | None:  # the summary's run start from its run_id; None when unknown
    m = _RUN_ID.match(str(s.get("run_id") or ""))
    try:
        return time.mktime(time.strptime(m[1], "%Y%m%dT%H%M%S")) if m else None
    except (ValueError, OverflowError):
        return None


def _all_green(s: dict) -> bool:
    """Tests ran and nothing failed, hung, crashed (`-::` lines) or was skipped without a name."""
    fails = [str(f) for f in s.get("failures") or []]
    try:
        passed, failed = int(s.get("passed") or 0), int(s.get("failed") or 0)
        unnamed = int(s.get("skipped") or 0) > len(s.get("skipped_tests") or [])
    except (TypeError, ValueError):
        return False
    crashed = any(f.startswith("-::") or "::" not in f for f in fails)
    return bool(passed and not failed and not fails and not crashed and not unnamed and not s.get("suspect_hang"))


def _summary_passes(e: dict, s: dict | None) -> bool:
    """The newest test run started after this failure, covered its file, and ran green with the test not skipped."""
    if not s or e.get("agent") != "run_tests" or e.get("kind") != "test-failure" or not _all_green(s):
        return False
    started, logged = _started_at(s), _logged_at(e)
    if started is None or logged is None or started <= logged:
        return False  # an older (or same-second) run cannot prove a newer failure fixed
    name = message(e)[1]
    test = f"{e['file']}::{name}"
    skipped = any(str(t).startswith(test) for t in s.get("skipped_tests") or [])
    covered = s.get("mode") == "full" or e["file"] in (s.get("selected") or [])
    return bool(covered and name and not skipped)


def _checkpoint_note(e: dict, facts: Facts) -> str:
    task = message(e)[1]
    failed = str(e.get("context", "")).rstrip().endswith("nothing saved")  # not an out-of-scope note
    if e.get("agent") != "checkpoint" or not failed or task not in facts.checkpoints:
        return ""
    saved, logged = facts.checkpoints[task], _logged_at(e)
    if saved is None or logged is None or saved == logged:
        return f"stale: {task} has a checkpoint; cannot tell if it came after this"
    return f"resolved: {task} checkpointed after this" if saved > logged else ""


def classify(entries: list[dict], facts: Facts | None = None) -> list[str]:  # O(K) total
    """One note per row: "" (open, or not an open incident), "resolved: why" or "stale: why"."""
    facts = facts or Facts()
    same: dict[tuple, int] = {}
    other: dict[tuple[str, str], int] = {}
    for i, e in enumerate(entries):
        m = _SEAM.match(str(e.get("context", ""))) if e.get("kind") == "verified" else None
        if m:
            same[("", "seam", m[1])] = i
        if e.get("kind") == "verified" and e.get("file"):
            same[(e["file"], *_writer(e))] = i
            other[(e["file"], str(e.get("agent") or ""))] = i
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
    if agent and agent != "run_tests" and other.get((e["file"], agent), -1) > i:  # same writer only
        return f"stale: {agent} logged `verified` for this file later"
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
