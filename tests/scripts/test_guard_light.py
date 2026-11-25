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


def note_of(r):
    """The additionalContext note of a pass, else ""; a pass never carries a permission decision or a rewrite."""
    assert r.returncode == 0, r.stderr
    if not r.stdout.strip():
        return ""
    out = json.loads(r.stdout)["hookSpecificOutput"]   # exactly one JSON object
    assert set(out) == {"hookEventName", "additionalContext"}, out
    assert out["additionalContext"].startswith("superlite (info): ") and "not blocked (guard light)" in out["additionalContext"]
    return out["additionalContext"]


@pytest.fixture
def stale(tmp_path):
    """A run from weeks ago that was never finished: every marker, a dispatch print, no run.finished."""
    sd = tmp_path / ".superlite"
    sd.mkdir()
    for name, body in (("stable.json", '{"stable": ["T1", "T2"], "fix": []}'), ("prime.md", "# superlite prime\n"),
                       ("pool.json", "{}"), ("dispatch.json", json.dumps({"batches": [["T1", "T2"]]}))):
        (sd / name).write_text(body)
    return tmp_path


def test_light_run_state_guards_only_inform(stale, tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    (home / "preferences.json").write_text('{"guard": "light"}')
    monkeypatch.setenv("SUPERLITE_HOME", str(home))
    monkeypatch.delenv("SUPERLITE_GUARD", raising=False)
    sd = stale / ".superlite"
    assert note_of(hbash(stale, "pytest -q")) == ""   # coordinator: runs as typed, silently
    assert note_of(hrun(stale, "Bash", {"command": "pytest"}, agent_id="a1")) == ""   # subagent full suite
    assert note_of(agent(stale, "general-purpose", "Review hooks/_edits read-only; compare with T7 and T1.")) == ""
    assert note_of(agent(stale, "superlite:implementer", "Your batch: T1,T2")) == ""
    for cmd in ("until pytest -q; do sleep 5; done", "pytest -q > /dev/null 2>&1"):
        assert "hang" in note_of(hbash(stale, cmd)), cmd
    assert "not one batch" in note_of(agent(stale, "superlite:implementer", "Build T3"))
    (sd / "questions.json").write_text(json.dumps({"waiting": {"T2": {"agent": "a1"}}}))
    assert "T2 waits for an answer" in note_of(agent(stale, "superlite:fixer-sonnet", "Fix T2"))
    assert not (sd / "incidents.jsonl").exists()   # nothing was blocked or rewritten
    cursor = json.dumps({"hook_event_name": "preToolUse", "tool_name": "Shell", "tool_input": {"command": "until pytest; do :; done"}})
    r = hrun(stale, "Shell", {}, raw=cursor)
    assert r.returncode == 0 and r.stdout == ""   # Cursor: no note (its output carries a permission)
    (sd / "questions.json").unlink()
    (sd / "blocked.json").write_text(json.dumps({"blocked": {"T2": {"tier": "opus"}}}))
    assert "needs a fixer of tier opus, not sonnet" in note_of(agent(stale, "superlite:fixer-sonnet", "Fix BLOCKED T2"))
    assert note_of(agent(stale, "superlite:fixer-opus", "Fix BLOCKED T2")) == ""
    assert hbash(stale, "echo {} > .superlite/orchestrator.json").returncode == 2   # protected state files still block


def test_strict_run_state_guards_unchanged(stale, monkeypatch):
    monkeypatch.delenv("SUPERLITE_GUARD", raising=False)
    r = hbash(stale, "pytest -q")
    assert r.returncode == 0 and "updatedInput" in r.stdout and "run_tests" in r.stdout
    assert hrun(stale, "Bash", {"command": "pytest"}, agent_id="a1").returncode == 2
    assert hbash(stale, "until pytest -q; do sleep 5; done").returncode == 2
    assert agent(stale, "superlite:implementer", "Build T3").returncode == 2


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


# ---- the overview flow: the machine plan is written first, then approved by its own path ----

def test_plain_lint_before_approval_does_not_start_a_run(tmp_path):
    from test_routing import mech_plan
    p = mech_plan(tmp_path, 2)
    run(ORCH, "set", "human", "--plan", p, cwd=tmp_path)
    run(SCRIPTS / "plan_tools.py", "lint", "--plan", p, cwd=tmp_path)
    sd = tmp_path / ".superlite"
    assert not any((sd / m).exists() for m in ("stable.json", "prime.md", "pool.json"))
    assert hrun(tmp_path, "Edit", {"file_path": str(tmp_path / "src" / "a.py")}).returncode == 0


def test_approve_takes_the_machine_plan(tmp_path):
    from test_orchestrator import orch, reply
    from test_routing import mech_plan
    p = mech_plan(tmp_path, 2)
    orch(tmp_path, "set", "human")
    reply(tmp_path, p)
    cp = orch(tmp_path, "approve", "--review", p)
    assert cp.returncode == 0, cp.both
    st = json.loads((tmp_path / ".superlite" / "orchestrator.json").read_text(encoding="utf-8"))
    assert st["approved"] is True and st["review_path"].endswith(p.name)
    lint = run(SCRIPTS / "plan_tools.py", "lint", "--stable", "--plan", p, cwd=tmp_path)
    assert lint.returncode != 4 and "BLOCKED" not in lint.both



# ---- light: kill guard and sessions ----

def test_light_allows_kill_by_name(tmp_path, light):
    for cmd in ("pkill -f 'java.*minecraft'", "killall node", "pgrep -f vite | xargs kill"):
        assert hbash(tmp_path, cmd).returncode == 0, cmd
    for cmd in ("kill -9 -1", "kill -s KILL -1"):
        assert hbash(tmp_path, cmd).returncode == 2, cmd


def test_strict_still_blocks_kill_by_name(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_GUARD", raising=False)
    assert hbash(tmp_path, "killall node").returncode == 2


def test_light_session_ensure_works_in_place(tmp_path, light):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    cp = run(SCRIPTS / "session.py", "ensure", "--slug", "demo", cwd=repo)
    assert cp.returncode == 0, cp.both
    assert "guard light" in cp.out and f"SESSION worktree={repo.as_posix()}" in cp.out
    branches = subprocess.run(["git", "branch", "--format=%(refname:short)"], cwd=repo, capture_output=True, text=True).stdout.split()
    assert branches == ["main"]
    wts = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout.strip().splitlines()
    assert len(wts) == 1


# ---- reach: other projects ----

def test_parent_run_does_not_govern_a_child_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_GUARD", raising=False)
    sd = tmp_path / ".superlite"
    sd.mkdir()
    (sd / "orchestrator.json").write_text('{"mode": "human", "approved": false}')
    (sd / "pool.json").write_text("{}")
    child = tmp_path / "other-project"
    (child / ".git").mkdir(parents=True)
    assert hrun(child, "Edit", {"file_path": str(child / "src" / "a.py")}).returncode == 0
    assert agent(child, "general-purpose").returncode == 0
    sub = tmp_path / "tools" / "editor"   # a plain subfolder of the run's own repo is still governed
    sub.mkdir(parents=True)
    assert hrun(sub, "Edit", {"file_path": str(tmp_path / "src" / "a.py")}).returncode == 2


def _session_start(cwd, home):
    import subprocess
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT), "SUPERLITE_HOME": str(home), "CLAUDE_PROJECT_DIR": str(cwd)}
    cp = subprocess.run([BASH, str(ROOT / "hooks" / "session-start")], cwd=cwd, env=env, capture_output=True,
                        text=True, stdin=subprocess.DEVNULL)
    return json.loads(cp.stdout)["hookSpecificOutput"]["additionalContext"]


def test_light_session_start_is_quiet_outside_superlite_projects(tmp_path, light):
    other = tmp_path / "homelab"
    other.mkdir()
    text = _session_start(other, tmp_path / "home")
    assert "not in use in this project" in text and "Start menu" not in text
    (other / ".superlite").mkdir()
    assert "Start menu" in _session_start(other, tmp_path / "home")


def test_strict_session_start_loads_everywhere(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_GUARD", raising=False)
    other = tmp_path / "homelab"
    other.mkdir()
    assert "Start menu" in _session_start(other, tmp_path / "home")


# ---- light: one run per topic, set aside and brought back ----

def _repo(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    return repo


def _ensure(repo, slug):
    cp = run(SCRIPTS / "session.py", "ensure", "--slug", slug, cwd=repo)
    assert cp.returncode == 0, cp.both
    return cp.out


def test_light_topics_park_and_resume(tmp_path, light):
    repo = _repo(tmp_path)
    sl = repo / ".superlite"
    _ensure(repo, "bay")
    (sl / "pool.json").write_text('{"agents": ["bay"]}')
    (sl / "progress.md").write_text("bay ledger\n")
    (sl / "test-cmd.json").write_text('{"cmd": "pytest"}')
    out = _ensure(repo, "rivers")
    assert "parked open run bay" in out
    assert (sl / "parked" / "bay" / "progress.md").read_text() == "bay ledger\n"
    assert not (sl / "pool.json").exists() and (sl / "test-cmd.json").is_file()   # project files stay
    assert hrun(repo, "Edit", {"file_path": str(repo / "src" / "a.py")}, ).returncode == 0  # bay's run no longer active
    (sl / "progress.md").write_text("rivers ledger\n")
    out = _ensure(repo, "bay")
    assert "parked open run rivers" in out and "resumed parked run bay" in out
    assert (sl / "progress.md").read_text() == "bay ledger\n" and not (sl / "parked" / "bay").exists()
    assert (sl / "parked" / "rivers" / "progress.md").is_file()
    assert "(guard light, in place)" in _ensure(repo, "bay")   # same topic: nothing moves


def test_light_topics_archive_finished_and_adopt_untracked(tmp_path, light):
    repo = _repo(tmp_path)
    sl = repo / ".superlite"
    sl.mkdir()
    (sl / "orchestrator.json").write_text('{"mode": "human", "approved": true, "review_path": "docs/plans/2026-11-04-bay-area-r3.md"}')
    (sl / "pool.json").write_text("{}")
    out = _ensure(repo, "bay-area-r3")   # a run from before topics: same name, adopted in place
    assert "parked" not in out and (sl / "pool.json").is_file()
    time.sleep(0.02)
    (sl / "run.finished").write_text("x\n")
    out = _ensure(repo, "rivers")
    assert "archived finished run bay-area-r3" in out
    assert any(p.name.startswith("bay-area-r3-") for p in (sl / "archive").iterdir())
    assert not (sl / "pool.json").exists()


# ---- light: bringing parked topics up ----

def _bay_run(repo):
    sl = repo / ".superlite"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-11-04-bay.md").write_text("# Bay: machine plan\n\n**Goal:** Graded shores and a wider bay. More detail here.\n")
    (sl / "stable.json").write_text(json.dumps({"stable": ["T1", "T2", "T3"], "fix": []}))
    (sl / "checkpoints.json").write_text(json.dumps({"plan": "docs/plans/2026-11-04-bay.md",
                                                     "entries": [{"task": "T1"}, {"task": "T2"}]}))


def test_parking_says_where_it_stood_and_to_carry_on(tmp_path, light):
    repo = _repo(tmp_path)
    _ensure(repo, "bay")
    _bay_run(repo)
    out = _ensure(repo, "rivers")
    assert "parked open run bay: 2 of 3 tasks done" in out and "Goal: Graded shores and a wider bay" in out
    assert "carry on without waiting" in out


def test_topics_status_and_brief(tmp_path, light):
    repo = _repo(tmp_path)
    _ensure(repo, "bay")
    _bay_run(repo)
    _ensure(repo, "rivers")
    st = run(SCRIPTS / "topics.py", "status", cwd=repo).out
    assert "parked:" in st and "- bay: 2 of 3 tasks done" in st
    brief = run(SCRIPTS / "topics.py", "brief", cwd=repo).out
    assert brief.startswith("Parked superlite topics") and "bay: 2 of 3 tasks done" in brief
    assert "bay" in _session_start(repo, tmp_path / "home") and "Parked superlite topics" in _session_start(repo, tmp_path / "home")
    _ensure(repo, "bay")
    assert run(SCRIPTS / "topics.py", "brief", cwd=repo).out == ""   # nothing parked but rivers, which has no run yet


# ---- merge with upstream: session create, the run archive ----

def test_light_session_create_stays_in_place_and_keeps_the_topic(tmp_path, light):
    import subprocess
    repo = _repo(tmp_path)
    sl = repo / ".superlite"
    _ensure(repo, "bay")
    (sl / "pool.json").write_text('{"agents": ["bay"]}')
    cp = run(SCRIPTS / "session.py", "create", "--slug", "bay-int", "--worktree", cwd=repo)
    assert cp.returncode == 0, cp.both
    assert "guard light" in cp.out and f"SESSION worktree={repo.as_posix()}" in cp.out
    assert (sl / "topic.txt").read_text().strip() == "bay" and (sl / "pool.json").is_file()   # nothing parked
    assert not (sl / "parked").exists()
    wts = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout.strip().splitlines()
    assert len(wts) == 1


def test_run_archive_leaves_other_topics_alone(tmp_path):
    import run_archive
    sl = tmp_path / ".superlite"
    (sl / "parked" / "rivers").mkdir(parents=True)
    (sl / "archive" / "old-2026-11-01").mkdir(parents=True)
    (sl / "progress.md").write_text("x\n")
    names = {p.name for p in run_archive.entries(sl)}
    assert "progress.md" in names and not names & {"parked", "archive"}


def test_topic_switch_keeps_project_files_from_upstream(tmp_path, light):
    repo = _repo(tmp_path)
    sl = repo / ".superlite"
    _ensure(repo, "bay")
    (sl / "pool.json").write_text("{}")
    for name in ("test-map.json", "peers.json"):
        (sl / name).write_text("{}")
    (sl / "research").mkdir()
    _ensure(repo, "rivers")
    assert (sl / "test-map.json").is_file() and (sl / "peers.json").is_file() and (sl / "research").is_dir()


def test_topics_status_reads_fix_rows(tmp_path, light, monkeypatch):
    repo = _repo(tmp_path)
    sl = repo / ".superlite"
    _ensure(repo, "bay")
    (sl / "stable.json").write_text(json.dumps({"stable": ["T1"], "fix": [{"task": "T2", "reasons": ["SIZE"]}]}))
    (sl / "checkpoints.json").write_text(json.dumps({"entries": [{"task": "T1"}]}))
    cp = run(SCRIPTS / "topics.py", "status", cwd=repo)
    assert cp.returncode == 0, cp.both
    assert "bay" in cp.out
