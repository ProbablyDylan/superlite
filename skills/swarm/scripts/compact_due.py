"""Session compact due check and host-neutral nudge text.

    compact_due.is_due(sl_dir, now, window_s=1800) -> bool
    compact_due.nudge_line() -> str
    compact_due.limit(root) -> int      tokens at which the token nudge starts
    compact_due.step(root) -> int       growth after a nudge before the next one (a tenth of the window)

The limit is a share of the context window (default 70% of 1M). An absolute `compact_tokens` still wins.
Each setting is read from the env, else the project's superlite.json, else preferences.json in the
superlite home (per machine), else the default.

Due when now - stamp >= window_s, where stamp is last-compact mtime if present,
else the earliest launch t from metrics.jsonl (else 0). Stdlib only.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_config  # noqa: E402
import state  # noqa: E402

__all__ = ["is_due", "nudge_line", "limit", "step", "window", "share", "token_nudge"]

_DEFAULT_WINDOW_S = 1800.0  # 30 min
_DEFAULT_CONTEXT = 1_000_000  # the host can't tell a hook its window; most current models have 1M
_DEFAULT_SHARE = 0.70
_STEP_SHARE = 0.10


def _positive(value, source: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"token count from {source} must be a positive integer")
    try:
        n = int(value) if isinstance(value, (int, str)) else None
    except ValueError:
        n = None
    if n is None or n <= 0:
        raise ValueError(f"token count from {source} must be a positive integer: {value!r}")
    return n


def _fraction(value, source: str) -> float:
    """A share as 0.7 or 70 (percent); must land in (0, 1]."""
    try:
        f = float(value) if not isinstance(value, bool) else None
    except (TypeError, ValueError):
        f = None
    if f is not None and 1 < f <= 100:
        f /= 100
    if f is None or not 0 < f <= 1:
        raise ValueError(f"compact_share from {source} must be a share in (0, 1] or a percent: {value!r}")
    return f


def _preferences() -> dict:
    try:
        from history import home  # one rule for the superlite home (SUPERLITE_HOME in tests)
        data = json.loads((home() / "preferences.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return {}
    return data if isinstance(data, dict) else {}


def _setting(root: str, env: str, key: str, parse, default):
    """env, else superlite.json, else preferences.json, else default."""
    raw = os.environ.get(env)
    if raw is not None and raw.strip():
        return parse(raw.strip(), env)
    data = project_config.load(root)
    if key in data:
        return parse(data[key], "superlite.json")
    prefs = _preferences()
    if key in prefs:
        return parse(prefs[key], "preferences.json")
    return default


def window(root: str) -> int:
    """Context window in tokens: SUPERLITE_CONTEXT_WINDOW / context_window, else 1M."""
    return _setting(root, "SUPERLITE_CONTEXT_WINDOW", "context_window", _positive, _DEFAULT_CONTEXT)


def share(root: str) -> float:
    """Share of the window at which to nudge: SUPERLITE_COMPACT_SHARE / compact_share, else 0.70."""
    return _setting(root, "SUPERLITE_COMPACT_SHARE", "compact_share", _fraction, _DEFAULT_SHARE)


def limit(root: str) -> int:
    """Token limit: an absolute SUPERLITE_COMPACT_TOKENS / compact_tokens, else window x share."""
    absolute = _setting(root, "SUPERLITE_COMPACT_TOKENS", "compact_tokens", _positive, None)
    if absolute is not None:
        return absolute
    return int(window(root) * share(root))


def step(root: str) -> int:
    """After a nudge, the context grows this much (a tenth of the window) before the next one."""
    return max(1, int(window(root) * _STEP_SHARE))


def _k(n: int) -> str:
    return f"{round(n / 1000)}k"


def token_nudge(tokens: int, limit: int, command: str, request: int = 1) -> str:
    """Nudge text for a token-count trigger; request > 1 means a compact is still pending."""
    if request > 1:
        return (
            f"Context still growing (reminder {request}, {_k(tokens)} tokens, limit {_k(limit)}): "
            f"run {command} at the next good stopping point."
        )
    return (
        f"Context is {_k(tokens)} tokens (limit {_k(limit)}): run {command} at the next good stopping point, "
        f"then re-read {state.DIR_NAME}/progress.md and continue."
    )


def _launch_t(sl: Path) -> float:  # O(L)
    """Earliest launch timestamp in metrics.jsonl, or 0."""
    path = sl / "metrics.jsonl"
    earliest: float | None = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("ev") == "launch" and isinstance(e.get("t"), (int, float)):
            t = float(e["t"])
            earliest = t if earliest is None else min(earliest, t)
    return 0.0 if earliest is None else earliest


def _compact_mtime(sl: Path) -> float | None:
    path = sl / "last-compact"
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def is_due(sl_dir: str, now: float, window_s: float = _DEFAULT_WINDOW_S) -> bool:  # O(L)
    """True when the compact window has elapsed since last compact or session launch."""
    sl = Path(sl_dir)
    stamp = _compact_mtime(sl)
    if stamp is None:
        stamp = _launch_t(sl)
    return (now - stamp) >= window_s


def nudge_line() -> str:
    """Host-neutral line asking the agent to compact the session."""
    return (
        "Session context is stale: please compact now (host compact / summarize), "
        f"then continue. Stamp {state.DIR_NAME}/last-compact when done."
    )


if __name__ == "__main__":
    import os
    import time

    sl = sys.argv[1] if len(sys.argv) > 1 else ""
    if not sl:
        proj = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        sl = str(Path(proj) / state.DIR_NAME)
    if is_due(sl, time.time()):
        print(nudge_line())
        sys.exit(0)
    sys.exit(1)
