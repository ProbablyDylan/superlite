#!/usr/bin/env python3
"""Guard mode: how much the superlite hooks hold the main session back.

    guard.py status          print the mode and where it came from
    guard.py set light|strict

`strict` (the default) is upstream superlite: during a build run the coordinator dispatches project code
instead of writing it, a closed human gate holds every agent that can write, and a run suspends peer
plugins (superpowers). `light` keeps the run machinery (protected state files, batch, question and tier
checks, the gate on superlite's own build agents and commands) and drops the rest: the kill guard (except
`kill -9 -1`), and the session branches and worktrees (`session ensure` works in place).

The mode is SUPERLITE_GUARD, else the "guard" key in preferences.json in the superlite home.
hooks/_preamble reads the same setting in bash: change both together. Stdlib only.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import base  # noqa: E402
from history import home  # noqa: E402  one rule for the global home

ENV = "SUPERLITE_GUARD"
MODES = ("strict", "light")


def preferences_path() -> Path:
    return home() / "preferences.json"


def mode() -> str:
    """'light' or 'strict': the env, then preferences.json; anything else counts as strict."""
    env = os.environ.get(ENV, "").strip()
    if env:
        return "light" if env == "light" else "strict"
    data = base.read_json(preferences_path(), {})
    return "light" if isinstance(data, dict) and data.get("guard") == "light" else "strict"


def light() -> bool:
    return mode() == "light"


def main(argv=None):  # O(file)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("set")
    s.add_argument("mode", choices=MODES)
    a = ap.parse_args(argv)
    if a.cmd == "set":
        path = preferences_path()
        data = base.read_json(path, {})
        data = data if isinstance(data, dict) else {}
        data["guard"] = a.mode
        path.parent.mkdir(parents=True, exist_ok=True)
        base.write_json(path, data)
    src = f"env {ENV}" if os.environ.get(ENV, "").strip() else str(preferences_path())
    print(f"guard: {mode()} ({src})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
