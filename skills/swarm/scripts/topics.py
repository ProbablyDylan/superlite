#!/usr/bin/env python3
"""Guard light: one run per topic in the same checkout, one at a time, with no branches or worktrees.

    topics.py status

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
            out.append(f"session: parked open run {name} -> {dest.relative_to(sl.parent).as_posix()}; "
                       f"`superlite-cli session ensure --slug {name}` brings it back")
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
    sub.add_parser("status")
    ap.parse_args(argv)
    sl = state.run_dir(Path.cwd())
    print(f"topic: {current(sl) or 'none'}")
    parked = sorted(p.name for p in (sl / PARKED).iterdir()) if (sl / PARKED).is_dir() else []
    print(f"parked: {', '.join(parked) or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
