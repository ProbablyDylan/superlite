"""Guard light: the main session may write project code, a closed gate holds only superlite's own build
agents and commands, and peers are not suspended. A finished run stops counting as active in every mode."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from test_block_coordinator_edits import BASH, HOOK, bash as hbash, run as hrun
from test_plan_tools import SCRIPTS, run

ROOT = SCRIPTS.parents[2]
ORCH = SCRIPTS / "orchestrator.py"
GUARD = SCRIPTS / "guard.py"
PEER = "superpowers@claude-plugins-official"


@pytest.fixture
def light(monkeypatch):
    monkeypatch.setenv("SUPERLITE_GUARD", "light")


@pytest.fixture
def active(tmp_path):
    (tmp_path / ".superlite").mkdir()
    (tmp_path / ".superlite" / "pool.json").write_text("{}")
    return tmp_path


def gated(tmp_path):
    (tmp_path / ".superlite").mkdir()
    (tmp_path / ".superlite" / "orchestrator.json").write_text('{"mode": "human", "approved": false}')
    return tmp_path


def agent(proj, subagent_type, prompt="look around"):
    return hrun(proj, "Agent", {"subagent_type": subagent_type, "prompt": prompt, "description": "d"})


# ---- run.finished ----

def test_finished_run_is_not_active(active):
    sd = active / ".superlite"
    assert hrun(active, "Edit", {"file_path": str(active / "src" / "a.py")}).returncode == 2
    time.sleep(0.02)
    (sd / "run.finished").write_text("x\n")
    assert hrun(active, "Edit", {"file_path": str(active / "src" / "a.py")}).returncode == 0
    assert hbash(active, "mv src/a.py src/b.py").returncode == 0
    time.sleep(0.02)
    (sd / "pool.json").write_text('{"agents": []}')  # the next run rewrites a marker
    assert hrun(active, "Edit", {"file_path": str(active / "src" / "a.py")}).returncode == 2


def test_finish_writes_mark_and_set_clears_it(tmp_path):
    sl = tmp_path / ".superlite"
    sl.mkdir()
    (sl / "progress.md").write_text("# Progress ledger\n", encoding="utf-8")
    run(SCRIPTS / "finish.py", "--ledger", sl / "progress.md", "--log", sl / "i.jsonl", cwd=tmp_path)
    assert (sl / "run.finished").is_file()
    run(ORCH, "set", "ai", "--plan", tmp_path / "plan.md", cwd=tmp_path)
    assert not (sl / "run.finished").exists()


# ---- light: writes ----

def test_light_allows_project_code_writes(active, light):
    assert hrun(active, "Edit", {"file_path": str(active / "src" / "app.py")}).returncode == 0
    assert hrun(active, "Write", {"file_path": "pyproject.toml"}).returncode == 0
    for cmd in ("mv src/a.py src/b.py", "rm -f build/out.o", "sed -i s/a/b/ src/app.py", "git apply x.diff",
                "echo hi > src/x.txt", "cp a.txt src/b.txt"):
        assert hbash(active, cmd).returncode == 0, cmd


def test_light_keeps_protected_state_files(active, light):
    for name in ("orchestrator.json", "feature.json", "human-replies.jsonl"):
        r = hrun(active, "Write", {"file_path": str(active / ".superlite" / name)})
        assert r.returncode == 2 and "never written directly" in r.stderr, name
    assert hbash(active, "echo {} > .superlite/orchestrator.json").returncode == 2


def test_light_set_by_preferences(active, tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("SUPERLITE_HOME", str(home))
    monkeypatch.delenv("SUPERLITE_GUARD", raising=False)
    target = {"file_path": str(active / "src" / "app.py")}
    assert hrun(active, "Edit", target).returncode == 2
    (home / "preferences.json").write_text('{"agent_specs": true, "guard": "light"}')
    assert hrun(active, "Edit", target).returncode == 0
    (home / "preferences.json").write_text('{"guard": "strict"}')
    assert hrun(active, "Edit", target).returncode == 2


# ---- light: the human gate ----

def test_light_gate_lets_other_agents_run(tmp_path, light):
    proj = gated(tmp_path)
    for st in ("general-purpose", "claude", "web-fetch", "Explore"):
        assert agent(proj, st).returncode == 0, st


def test_light_gate_holds_superlite_build(tmp_path, light):
    proj = gated(tmp_path)
    for st in ("superlite:implementer", "superlite:fixer-opus", "implementer-haiku", "superlite:final-reviewer"):
        r = agent(proj, st)
        assert r.returncode == 2 and "Human orchestrator" in r.stderr, st
    for tool in ("dispatch", "assign", "wave0", "checkpoint", "finish", "build_prime"):
        assert hbash(proj, f"superlite-cli {tool}").returncode == 2, tool


def test_strict_gate_unchanged(tmp_path):
    proj = gated(tmp_path)
    assert agent(proj, "general-purpose").returncode == 2


def test_light_keeps_batch_guard(active, light):
    sd = active / ".superlite"
    (sd / "dispatch.json").write_text(json.dumps({"batches": [["T1", "T2"]]}))
    r = agent(active, "superlite:implementer", "Build T3")
    assert r.returncode == 2 and "batch" in r.stderr


# ---- light: peers ----

def test_light_does_not_suspend_peers(tmp_path, monkeypatch, light):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"enabledPlugins": {PEER: True}}) + "\n", encoding="utf-8")
    cp = run(ORCH, "set", "human", "--plan", tmp_path / "plan.md", cwd=tmp_path)
    assert cp.returncode == 0 and "peers: not suspended (guard light)" in cp.out
    assert not (tmp_path / ".superlite" / "peers.json").exists()
    assert json.loads(settings.read_text(encoding="utf-8"))["enabledPlugins"][PEER] is True


# ---- guard.py ----

def test_guard_cli_set_and_status(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_GUARD", raising=False)
    assert "guard: strict" in run(GUARD, "status", cwd=tmp_path).out
    assert "guard: light" in run(GUARD, "set", "light", cwd=tmp_path).out
    prefs = Path(os.environ["SUPERLITE_HOME"]) / "preferences.json"
    assert json.loads(prefs.read_text(encoding="utf-8"))["guard"] == "light"
    assert "guard: strict" in run(GUARD, "set", "strict", cwd=tmp_path).out
