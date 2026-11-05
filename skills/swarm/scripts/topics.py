#!/usr/bin/env python3
"""Guard light: one run per topic in the same checkout, one at a time, with no branches or worktrees.

    topics.py status     the live topic and the parked ones, one plain line each
    topics.py brief      one line for session start (empty when nothing is parked)

`switch(live, want)` (called by `session ensure` in guard light) keeps the live run folder for one topic, named
in `.superlite/topic.txt`. Starting another topic sets the live run aside first: an open run goes to
`.superlite/parked/<topic>/`, a finished one (run.finished newer than its markers) to
`.superlite/archive/<topic>-<date>/`. A parked run for the wanted topic comes back. Project-level files (setup,
test command, probes, history) stay in place. Stdlib only.
"""
import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import base  # noqa: E402
import session_registry as R  # noqa: E402
import state  # noqa: E402

TOPIC = "topic.txt"
PARKED = "parked"
ARCHIVE = "archive"
MARKERS = ("stable.json", "prime.md", "pool.json")
# Not run state: they describe the project, or hold other runs.
KEEP = set(R.STATIONARY) | {TOPIC, PARKED, ARCHIVE, "setup.done", "test-cmd.json", "probes", "history.jsonl",
                            "agent-specs.json", "host-models.json", "__pycache__"}


def entries(sl: Path) -> list:  # O(entries)
    return [p for p in sl.iterdir() if p.name not in KEEP] if sl.is_dir() else []


def current(sl: Path) -> str | None:
    try:
        return (sl / TOPIC).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def unnamed(sl: Path) -> str:
    """A name for a run started before topics were tracked: the approved plan's topic, else a dated placeholder."""
    o = base.read_json(sl / "orchestrator.json", {})
    stem = Path(str((o if isinstance(o, dict) else {}).get("review_path") or "")).name
    for suffix in (".review.md", ".feature.md", ".mvp.md", ".md"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = stem.split("-")
    if len(parts) > 3 and all(p.isdigit() for p in parts[:3]):
        stem = "-".join(parts[3:])
    return stem or f"untitled-{date.today().isoformat()}"


def finished(sl: Path) -> bool:
    mark = sl / "run.finished"
    if not mark.is_file():
        return False
    t = mark.stat().st_mtime
    return all(not (sl / m).is_file() or (sl / m).stat().st_mtime <= t for m in MARKERS)


def _move(src: Path, dest: Path) -> None:  # O(S)
    dest.mkdir(parents=True, exist_ok=True)
    for p in entries(src):
        target = dest / p.name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        shutil.move(str(p), str(target))


def _free(base_dir: Path, name: str) -> Path:
    d, n = base_dir / name, 2
    while d.exists():
        d, n = base_dir / f"{name}-{n}", n + 1
    return d


def _plan(d: Path, project: Path):
    """The run's plan file: the one its checkpoints name, else the approved one, else None."""
    for rel in (base.read_json(d / "checkpoints.json", {}).get("plan"),
                base.read_json(d / "orchestrator.json", {}).get("review_path")):
        if rel and (project / rel).is_file():
            return project / rel
    return None


def _goal(plan) -> str:
    """The plan's `**Goal:**` line (first sentence), else its title."""
    if plan is None:
        return ""
    title = ""
    try:
        lines = plan.read_text(encoding="utf-8").splitlines()[:40]
    except OSError:
        return ""
    for line in lines:
        if line.startswith("**Goal:**"):
            g = line[len("**Goal:**"):].strip()
            first = g.split(". ")[0].rstrip(".")
            return first if len(first) <= 300 else first[:297] + "..."
        if not title and line.startswith("# "):
            title = line[2:].strip()
    return title


def summary(d: Path, project: Path, name: str) -> dict:  # O(files)
    """Plain facts about one run folder: name, goal, tasks done and planned, open or finished, last change."""
    st = base.read_json(d / "stable.json", {})
    planned = set((st.get("stable") or []) + (st.get("fix") or [])) if isinstance(st, dict) else set()
    ck = base.read_json(d / "checkpoints.json", {})
    done = {e.get("task") for e in (ck.get("entries") or []) if isinstance(e, dict)} & planned if planned else set()
    files = [f for e in entries(d) for f in ([e] if e.is_file() else e.rglob("*")) if f.is_file()]
    last = max((f.stat().st_mtime for f in files), default=None)
    orch = base.read_json(d / "orchestrator.json", {})
    waiting = isinstance(orch, dict) and orch.get("mode") == "human" and orch.get("approved") is False
    return {"name": name, "goal": _goal(_plan(d, project)), "done": len(done), "planned": len(planned),
            "finished": finished(d), "waiting": waiting,
            "last": date.fromtimestamp(last).isoformat() if last else None}


def describe(info: dict) -> str:
    """One plain line: name, where it stands, when it was last touched, its goal."""
    if info["finished"]:
        where = "finished"
    elif info["waiting"]:
        where = "plan waiting for approval"
    elif info["planned"] and info["done"] == info["planned"]:
        where = f"all {info['planned']} tasks built, not yet wrapped up"
    elif info["planned"]:
        where = f"{info['done']} of {info['planned']} tasks done"
    else:
        where = "planning, nothing built yet"
    when = f", last worked on {info['last']}" if info["last"] else ""
    goal = f". Goal: {info['goal']}" if info["goal"] else ""
    return f"{info['name']}: {where}{when}{goal}"


def parked(sl: Path) -> list:  # O(P * files)
    pd = sl / PARKED
    if not pd.is_dir():
        return []
    return [summary(d, sl.parent, d.name) for d in sorted(pd.iterdir()) if d.is_dir()]


def switch(sl: Path, want: str) -> list:  # O(S)
    """Make `want` the live topic in run folder `sl`; the lines to print."""
    out = []
    have = current(sl)
    if have == want:
        return [f"session: {want} (guard light, in place)"]
    if entries(sl):
        name = have or unnamed(sl)
        if name == want:  # a run from before topics were tracked, for this same topic: adopt it
            (sl / TOPIC).write_text(want + "\n", encoding="utf-8")
            return [f"session: {want} (guard light, in place)"]
        if finished(sl):
            dest = _free(sl / ARCHIVE, f"{name}-{date.today().isoformat()}")
            out.append(f"session: archived finished run {name} -> {dest.relative_to(sl.parent).as_posix()}")
        else:
            dest = _free(sl / PARKED, name)
            info = describe(summary(sl, sl.parent, name))
            out.append(f"session: parked open run {info}. Tell the user in one plain line that this run was set "
                       f"aside and can be brought back any time, then carry on without waiting. "
                       f"(`superlite-cli session ensure --slug {dest.name}` brings it back.)")
        _move(sl, dest)
    back = sl / PARKED / want
    if back.is_dir():
        _move(back, sl)
        shutil.rmtree(back, ignore_errors=True)
        out.append(f"session: resumed parked run {want}")
    sl.mkdir(parents=True, exist_ok=True)
    (sl / TOPIC).write_text(want + "\n", encoding="utf-8")
    out.append(f"session: {want} (guard light, in place)")
    return out


def ensure_in_place(start: Path, want: str) -> int:
    """`session ensure` in guard light: no branch, worktree or registry row; switch the live topic and say so."""
    print("session: off (guard light): no branch or worktree; work here, on the current branch")
    for line in switch(state.run_dir(start), want):
        print(line)
    R.session_line(start, want, "-")
    return 0


def main(argv=None):  # O(entries)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="the live topic and every parked one, one plain line each")
    sub.add_parser("brief", help="one line for session start, empty when nothing is parked")
    a = ap.parse_args(argv)
    sl = state.run_dir(Path.cwd())
    runs = parked(sl)
    if a.cmd == "brief":
        if runs:
            print("Parked superlite topics (open runs set aside): " + " | ".join(describe(r) for r in runs)
                  + ". Mention them only when the user starts something new or asks to pick up earlier work; "
                    "`superlite-cli session ensure --slug <topic>` brings one back.")
        return 0
    live = current(sl)
    if live and entries(sl):
        print("live: " + describe(summary(sl, sl.parent, live)))
    else:
        print("live: none")
    print("parked:" + ("" if runs else " none"))
    for r in runs:
        print("- " + describe(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
