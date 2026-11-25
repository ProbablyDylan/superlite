"""A run nobody finished: run_idle.py closes it after hours of quiet (hooks, session start, suggest_flow); the budget
counts this run's working time and cost only."""
import json
import os
import subprocess
import sys
import time

import pytest

from test_block_coordinator_edits import BASH, run as pre_tool
from test_human_gate import reply
from test_plan_tools import SCRIPTS
from test_session_run_state import _start, hub, reg, row  # noqa: F401  (hub: fixture)

sys.path.insert(0, str(SCRIPTS))
import budget  # noqa: E402
import metrics  # noqa: E402
import run_idle  # noqa: E402
import suggest_flow  # noqa: E402

H = 3600


@pytest.fixture(autouse=True)
def light(monkeypatch):  # the idle close is a guard-light rule; strict keeps a run on until finish
    monkeypatch.setenv("SUPERLITE_GUARD", "light")


def age(p, hours):
    t = time.time() - hours * H
    os.utime(p, (t, t))


def run_dir(tmp_path, hours=0):
    sl = tmp_path / ".superlite"
    sl.mkdir(exist_ok=True)
    for n in ("pool.json", "progress.md"):
        (sl / n).write_text("{}")
        age(sl / n, hours)
    return sl


def test_closes_only_a_run_idle_past_the_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_RUN_IDLE_H", raising=False)
    sl = run_dir(tmp_path, hours=2)
    assert run_idle.close_if_idle(tmp_path) == "" and not (sl / "run.idle").exists()
    age(sl / "pool.json", 7)
    assert run_idle.close_if_idle(tmp_path) == ""  # progress.md moved 2 h ago: still working
    age(sl / "progress.md", 7)
    msg = run_idle.close_if_idle(tmp_path)
    assert "no longer counts as running" in msg and (sl / "run.idle").is_file()
    assert run_idle.closed(sl) and not run_idle.is_on(sl) and run_idle.close_if_idle(tmp_path) == ""
    assert (sl / "pool.json").is_file()  # nothing removed
    (sl / "pool.json").write_text("{}")  # the next build writes a marker: live again
    assert run_idle.is_on(sl) and not run_idle.closed(sl)


def test_finished_run_is_not_reclosed(tmp_path):
    sl = run_dir(tmp_path, hours=10)
    (sl / "run.finished").write_text("x")
    assert run_idle.close_if_idle(tmp_path) == "" and not (sl / "run.idle").exists()


def test_strict_keeps_a_run_on_until_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERLITE_GUARD", "strict")
    sl = run_dir(tmp_path, hours=8)
    edit = {"file_path": str(tmp_path / "src" / "app.py")}
    assert run_idle.close_if_idle(tmp_path) == "" and not (sl / "run.idle").exists()
    (sl / "run.idle").write_text("x")  # left from a light session
    assert pre_tool(tmp_path, "Edit", edit).returncode == 2  # strict, live run: the coordinator may not edit code
    assert preamble_active(tmp_path, "strict") == "1" and preamble_active(tmp_path, "light") == "0"


def preamble_active(proj, mode):
    hook = SCRIPTS.parents[2] / "hooks" / "_preamble"
    r = subprocess.run([BASH, "-c", f'. "{hook}"; printf %s "$active"'], input='{"tool_name":"Bash"}', capture_output=True,
                       text=True, env={**os.environ, "CLAUDE_PROJECT_DIR": str(proj), "SUPERLITE_GUARD": mode})
    return r.stdout


def test_a_prompt_after_a_long_absence_closes_the_run(tmp_path):
    sl = run_dir(tmp_path, hours=8)
    r = reply(tmp_path, "back again, carry on")
    assert (sl / "run.idle").is_file() and "idle since" in r.stdout
    assert "idle since" not in reply(tmp_path, "and again").stdout


def test_session_start_closes_an_idle_run_and_reports_existing_code(hub):  # noqa: F811
    sl = hub / ".superlite"
    (sl / "orchestrator.json").write_text("{}")
    (hub / "docs" / "plans").mkdir(parents=True)
    (sl / "stable.json").write_text("{}")
    assert "Project state: run in progress." in _start(hub)
    age(sl / "stable.json", 9)
    ctx = _start(hub)
    assert "idle since" in ctx and "Project state: existing code." in ctx and (sl / "run.idle").is_file()
    assert suggest_flow.project_state(str(hub)) == suggest_flow.EXISTING
    assert "idle since" not in _start(hub)  # once


def agent(aid, start, stop=None):
    return {"agent": aid, "start_ms": start * 1000, "stop_ms": stop * 1000 if stop else None, "complete": stop is not None,
            "first_abs": None, "segments": []}


def test_budget_counts_this_runs_working_time_only():
    now = 1_000_000.0
    old = [agent("a", now - 10 * 24 * H, now - 10 * 24 * H + 600)]  # ten days ago: another run
    cur = [agent("b", now - 1800, now - 1200), agent("c", now - 800, now - 400),  # 400 s pause: capped at 300
           agent("d", now - 700, now - 500)]  # inside c
    el, cost_from = budget.run_window(old + cur, now)
    assert el == 600 + metrics.GAP_CAP_S + 400 and cost_from == now - 10 * 24 * H + 600  # planning turns after it count
    lost = agent("e", now - 9 * H)  # never stopped, nine hours ago: its stop was lost
    assert budget.run_window(cur + [lost], now)[0] == el
    assert budget.run_window(cur + [agent("f", now - 100)], now)[0] == el + metrics.GAP_CAP_S + 100  # still running
    assert budget.run_window(old + cur, now, since=now - 1100)[0] == 400  # after a finish at now-1100


def test_coordinator_cost_scoped_to_the_run_and_newest_session(tmp_path):
    sl = tmp_path / ".superlite"
    sl.mkdir()
    old, new = tmp_path / "old.jsonl", tmp_path / "new.jsonl"

    def msg(i, ts):
        return json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": f"m{i}", "model": "claude-sonnet",
                           "usage": {"input_tokens": 1000, "output_tokens": 10}, "content": []}})
    old.write_text(msg(1, "2026-11-08T10:00:00Z") + "\n")
    new.write_text("\n".join(msg(i, f"2026-11-22T10:0{i}:00Z") for i in range(2, 5)) + "\n")
    (sl / "metrics.jsonl").write_text("\n".join(json.dumps({"ev": "launch", "session_transcript": str(p)})
                                                for p in (old, new)) + "\n")
    assert metrics.session_transcript(sl) == str(new)
    since = metrics._ts({"timestamp": "2026-11-22T10:03:00Z"})
    assert metrics.coordinator_record(sl)["turns"] == 3 and metrics.coordinator_record(sl, since=since)["turns"] == 2


def test_a_reused_agents_idle_time_between_tasks_is_not_work():
    now = 1_000_000.0
    a = agent("a", now - 30 * H, now - 60)
    a["segments"] = [{"first": now - 30 * H, "last": now - 30 * H + 600}, {"first": now - 3600, "last": now - 60}]
    el, cost_from = budget.run_window([a], now)
    assert el == 3600 - 60 and cost_from == now - 30 * H + 600  # 29 h idle: the earlier task was another run


def test_idle_limit_has_a_floor(monkeypatch):
    monkeypatch.setenv("SUPERLITE_RUN_IDLE_H", "0")
    assert run_idle.idle_hours() == 1.0


def test_a_resumed_build_reopens_on_its_next_dispatch(tmp_path):
    sl = run_dir(tmp_path, hours=8)
    run_idle.close_if_idle(tmp_path)
    assert run_idle.idle_closed(sl)
    (sl / "dispatch.json").write_text("{}")  # dispatch rewrites it every wave
    assert run_idle.is_on(sl) and not run_idle.idle_closed(sl)


def test_a_registered_open_session_with_an_idle_closed_run_is_existing_code(hub):  # noqa: F811
    sl = hub / ".superlite"
    (sl / "orchestrator.json").write_text("{}")
    reg(hub, [row(hub, run="open")], "demo")
    (sl / "pool.json").write_text("{}")
    assert "Project state: run in progress." in _start(hub)
    age(sl / "pool.json", 9)
    assert "Project state: existing code." in _start(hub)
    assert suggest_flow.project_state(str(hub)) == suggest_flow.EXISTING
