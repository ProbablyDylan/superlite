#!/usr/bin/env python3
"""Close a build run nobody finished: once it has been idle for RUN_IDLE_H hours, write `.superlite/run.idle`.

    run_idle.py [DIR]        DIR: the project (default .)

A run counts as on while a run marker (stable.json, prime.md, pool.json, dispatch.json) is newer than run.finished (written by
`superlite-cli finish`) and run.idle (written here). Users who never run finish leave a run on for days: the hooks keep
treating it as live and the budget's clock keeps counting. The session-start hook calls this when a run is on; when the
newest run file (the markers, progress.md) is older than RUN_IDLE_H hours
(SUPERLITE_RUN_IDLE_H, default 6), it writes run.idle and prints one line. Nothing is moved or deleted: the run's files
stay for its report, topics parks (not archives) it, and the next dispatch (dispatch.json), lint --stable or build_prime
rewrites a marker, which makes the run live again. Library: idle_since(sl) -> the last activity epoch, or None. Stdlib only.
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402

MARKERS = ("stable.json", "prime.md", "pool.json", "dispatch.json")  # topics.MARKERS, the hooks' lists: change together
ACTIVITY = MARKERS + ("progress.md",)  # not metrics.jsonl or incidents.jsonl: any agent or hook block writes those
IDLE = "run.idle"


def idle_hours() -> float:
    try:
        return max(1.0, float(os.environ.get("SUPERLITE_RUN_IDLE_H", "6")))  # never 0: a compact must not close a run
    except ValueError:
        return 6.0


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def closed_at(sl: Path) -> float:
    """The newest of run.finished and run.idle (0 when neither): the run's clock starts after it."""
    return max((_mtime(sl / n) or 0.0) for n in ("run.finished", IDLE))


def is_on(sl: Path) -> bool:
    end = closed_at(sl)
    return any((m := _mtime(sl / n)) is not None and m > end for n in MARKERS)


def closed(sl: Path) -> bool:
    """Finished or idle-closed, with no run marker written since."""
    return closed_at(sl) > 0 and not is_on(sl)


def idle_closed(sl: Path) -> bool:
    """Closed by the idle rule (run.idle at least as new as run.finished), not by finish."""
    idle = _mtime(sl / IDLE)
    return idle is not None and closed(sl) and idle >= (_mtime(sl / "run.finished") or 0.0)


def idle_since(sl: Path, now: float | None = None) -> float | None:
    """The last activity time when the run is on and idle past the limit, else None."""
    if not is_on(sl):
        return None
    last = max((m for n in ACTIVITY if (m := _mtime(sl / n)) is not None), default=None)
    now = time.time() if now is None else now
    return last if last is not None and now - last > idle_hours() * 3600 else None


def close_if_idle(root: Path, now: float | None = None) -> str:
    sl = state.run_dir(root)
    last = idle_since(sl, now)
    if last is None:
        return ""
    when = datetime.fromtimestamp(last).isoformat(timespec="minutes")
    try:
        (sl / IDLE).write_text(f"idle since {when}\n", encoding="utf-8")
    except OSError:
        return ""
    return (f"superlite: the last build run was idle since {when}, so it no longer counts as running "
            f"(nothing removed; the next dispatch reopens it)")


if __name__ == "__main__":
    msg = close_if_idle(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
    if msg:
        print(msg)
