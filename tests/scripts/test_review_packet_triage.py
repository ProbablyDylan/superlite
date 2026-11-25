"""Review packet noise: repeats collapse into one item with a count, rows a later event resolved leave the open list,
and rows that may be stale stay listed but marked."""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "swarm" / "scripts"))

import review_packet as rp  # noqa: E402
import review_triage as tr  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "review_basic"


def row(file="a.py", line=3, context="boom", agent="T1", kind="exception", **kw) -> dict:
    return {"file": file, "line": line, "context": context, "agent": agent, "kind": kind,
            "resolution": "open", **kw}


def verified(file, agent="run_tests", context="run_tests full: 3 tests pass") -> dict:
    return {"file": file, "line": 0, "context": context, "agent": agent, "kind": "verified"}


def items(entries, facts=None):
    return rp.incident_items(entries, tr.classify(entries, facts))


# ---------------------------------------------------------------- collapsing

def test_same_message_across_files_is_one_item():
    entries = [row(f"src/m{i}.py", 8, "T0: ModuleNotFoundError: No module named 'numpy'; nothing saved",
                   agent="checkpoint") for i in range(8)] * 2
    got = items(entries)
    assert len(got) == 1
    assert got[0].title.startswith("src/m0.py:8 (+7 more location(s))") and "16 entries" in got[0].title
    assert "(x16: T0)" in got[0].body and "same message also at: src/m1.py:8" in got[0].body
    assert got[0].more[:2] == ("src/m1.py", "src/m2.py")  # every file still reaches the hot-spot excerpts


def test_different_messages_stay_apart():
    got = items([row("a.py", 1, "issue 1"), row("a.py", 2, "issue 2")])
    assert len(got) == 2


def test_repeats_at_one_location_collapse_with_count():
    ctx = "probe {} crash RuntimeError: serve factory needs arguments ['root'] (size {}, {} probe(s), e.g. x/y)"
    entries = [row("adapter.py", 0, ctx.format(cat, n, n + 3), agent="probe_run") for n, cat in
               enumerate(["deep", "numeric", "encoding", "deep"])]
    got = items(entries)
    assert len(got) == 1
    lines = [ln for ln in got[0].body.splitlines() if "serve factory" in ln]
    assert len(lines) == 1 and "(x4: deep, numeric, encoding)" in lines[0] and "size" not in lines[0]


def test_probe_results_duplicates_collapse(tmp_path):
    r = {"category": "deep", "file": "a.py", "line": 4, "status": "crash", "detail": "RecursionError"}
    (tmp_path / "probe-results.json").write_text(json.dumps({"results": [r] * 9}), encoding="utf-8")
    got = rp.read_probe_failures(tmp_path)
    assert len(got) == 1 and got[0].title.endswith("(x9)")


# ---------------------------------------------------------------- resolved by a later event

def test_later_run_tests_verified_resolves():
    entries = [row("tests/t.py", 5, "test_a: KeyError: 99", agent="run_tests", kind="test-failure"),
               verified("tests/t.py")]
    notes = tr.classify(entries)
    assert notes[0].startswith("resolved")
    assert rp.incident_items(entries, notes) == []
    assert "tests/t.py:5" in rp.resolved_lines(entries, notes)[0]


def test_earlier_verified_does_not_resolve():
    entries = [verified("tests/t.py"), row("tests/t.py", 5, "test_a: KeyError: 99", agent="run_tests",
                                           kind="test-failure")]
    assert len(items(entries)) == 1


def test_probe_verified_resolves_only_its_category():
    fail = row("a.py", 9, "probe deep crash RecursionError: x (size 1, 2 probe(s), e.g. d/n)", agent="probe_run")
    same = verified("a.py", "probe_run", "probe deep: 6 probes pass (call), sizes up to 9 [sha:abc]")
    other = verified("a.py", "probe_run", "probe numeric: 6 probes pass (call), sizes up to 9 [sha:abc]")
    assert items([fail, same]) == []
    got = items([fail, other])
    assert len(got) == 1 and "possibly stale" in got[0].title  # another writer's verified: kept, marked


T0 = 1_790_000_000.0


def _run_id(epoch: float) -> str:  # run_tests.new_run_id's shape: local start time, pid, random
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(epoch)) + "-123-abcdef"


def _fail(t=T0):
    return row("tests/t.py", 5, "test_a: KeyError: 99", agent="run_tests", kind="test-failure", **_at(t))


def _green(**kw) -> dict:  # a green full run that started a minute after the failure row
    return {"mode": "full", "passed": 40, "failed": 0, "skipped": 0, "failures": [], "skipped_tests": [],
            "run_id": _run_id(T0 + 60), **kw}


def test_newest_full_run_resolves_test_failure():
    fail = _fail()
    assert items([fail], tr.Facts(summary=_green())) == []
    red = _green(passed=39, failed=1, failures=["tests/t.py::test_a: KeyError: 99"])
    assert len(items([fail], tr.Facts(summary=red))) == 1
    other = _green(mode="affected", passed=3, selected=["tests/u.py"])
    assert len(items([fail], tr.Facts(summary=other))) == 1  # did not re-run this file
    mine = _green(mode="affected", passed=3, selected=["tests/t.py"])
    assert items([fail], tr.Facts(summary=mine)) == []


def test_skipped_test_is_not_resolved():  # defect 1a: skipped is not green
    s = _green(skipped=1, skipped_tests=["tests/t.py::test_a"])
    assert len(items([_fail()], tr.Facts(summary=s))) == 1
    unnamed = _green(skipped=2, skipped_tests=[])  # the non-JUnit path counts skips without names
    assert len(items([_fail()], tr.Facts(summary=unnamed))) == 1


def test_run_stopped_by_another_failure_is_not_resolved():  # defect 1b: -x / --maxfail never reached it
    s = _green(failed=1, failures=["tests/g.py::test_g: boom"])
    assert len(items([_fail()], tr.Facts(summary=s))) == 1


def test_non_pytest_crash_is_not_resolved():  # defect 1c: run_tests writes `-::ctx` for a crash
    for s in (_green(failures=["-::exit 139 (SIGSEGV): Segmentation fault"]),
              _green(failures=["-::exit 139 (SIGSEGV): Segmentation fault"], failed=None)):
        assert len(items([_fail()], tr.Facts(summary=s))) == 1, s


def test_summary_older_than_the_failure_is_not_resolved():  # defect 1d: an older background run published late
    assert len(items([_fail()], tr.Facts(summary=_green(run_id=_run_id(T0 - 60))))) == 1
    assert len(items([_fail()], tr.Facts(summary=_green(run_id=_run_id(T0))))) == 1  # same second: order unknown
    assert len(items([_fail()], tr.Facts(summary=_green(run_id="")))) == 1  # no start time
    undated = row("tests/t.py", 5, "test_a: KeyError: 99", agent="run_tests", kind="test-failure")
    assert len(items([undated], tr.Facts(summary=_green()))) == 1


def test_crashed_hung_or_empty_run_resolves_nothing():
    for s in (_green(passed=0),  # no tests collected
              _green(passed=0, failures=["pytest produced no results (killed)"]),
              _green(passed=5, failures=["hang: watcher killed pytest"]),
              _green(passed=5, suspect_hang=True)):
        assert len(items([_fail()], tr.Facts(summary=s))) == 1, s


def test_later_seam_verified_resolves_the_mismatch():
    bad = row("src/w.py", 4, "seam T1↔T2: T1 Produces: w.set: missing parameter 'raw'", agent="pt_review")
    ok = {"file": "", "line": 0, "context": "verified: seam T1↔T2", "agent": "pt_review", "kind": "verified"}
    other = dict(ok, context="verified: seam T3↔T4")
    assert items([bad, ok]) == []
    assert len(items([ok, bad])) == 1
    assert len(items([bad, other])) == 1


def test_run_tests_pass_does_not_resolve_perf():
    slow = row("tests/t.py", 0, "slow: 40 s", agent="run_tests", kind="perf")
    assert len(items([slow, verified("tests/t.py")])) == 1


def test_generic_test_message_does_not_merge_across_files():
    got = items([row(f"tests/t{i}.py", 3, f"test_{i}: assert False", agent="run_tests", kind="test-failure")
                 for i in range(2)])
    assert len(got) == 2


def _at(epoch: float) -> dict:
    return {"date": time.strftime("%Y-%m-%d", time.localtime(epoch)),
            "time": time.strftime("%H:%M:%S", time.localtime(epoch))}


def test_checkpoint_row_resolved_by_later_checkpoint_only():
    t = 1_790_000_000.0
    fail = row("src/m.py", 8, "T1: ModuleNotFoundError: numpy; nothing saved", agent="checkpoint", **_at(t))
    assert items([fail], tr.Facts(checkpoints={"T1": t + 60})) == []
    assert "possibly stale" not in items([fail], tr.Facts(checkpoints={"T1": t - 60}))[0].title
    assert len(items([fail], tr.Facts(checkpoints={"T2": t + 60}))) == 1  # another task's checkpoint


def test_checkpoint_order_unknown_is_possibly_stale_and_ranked_last():
    undated = row("src/m.py", 8, "T1: ModuleNotFoundError: numpy; nothing saved", agent="checkpoint")
    live = row("src/n.py", 2, "real bug")
    got = sorted(items([undated, live], tr.Facts(checkpoints={"T1": 1.0})), key=lambda i: i.rank)
    assert [i.file for i in got] == ["src/n.py", "src/m.py"]
    assert "possibly stale: T1 has a checkpoint" in got[1].title
    assert "check it against the code" in got[1].body


def test_out_of_scope_note_is_not_resolved_by_its_own_checkpoint():  # defect 2
    t = 1_790_000_000.0
    left = row("vendor/", 0, "T1: change outside Owns left out of the checkpoint: 3 files under vendor/, first 3: "
               "vendor/a, vendor/b, vendor/c", agent="checkpoint", resolution="noted", **_at(t))
    for saved in (t + 60, t):
        assert tr.classify([left], tr.Facts(checkpoints={"T1": saved})) == [""], saved


def test_unrelated_writer_verified_does_not_mark_stale():  # defect 3
    bug = row("src/a.py", 4, "T1: ValueError in parse", agent="T1")
    assert tr.classify([bug, verified("src/a.py")])[0] == ""
    mine = row("src/a.py", 4, "off-by-one in parse", agent="reviewer")
    assert tr.classify([mine, verified("src/a.py", "reviewer", "parse ok")])[0].startswith("stale")


def test_missing_file_is_possibly_stale():
    got = items([row("gone.py", 1, "x")], tr.Facts(exists=lambda f: False))
    assert len(got) == 1 and "possibly stale: the file no longer exists" in got[0].title


# ---------------------------------------------------------------- the whole packet

def test_packet_counts_only_open_and_points_at_changed_files(tmp_path):
    proj = tmp_path / "proj"
    shutil.copytree(FIXTURE, proj)
    sl = proj / ".superlite"
    sl.mkdir(exist_ok=True)
    (sl / "checkpoints.json").write_text(json.dumps({"entries": [
        {"task": "T1", "sha": "0" * 40, "files": ["src/minisheet/workbook.py"]}]}), encoding="utf-8")
    log = proj / "log.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in [
        row("tests/t.py", 5, "test_a: KeyError: 99", agent="run_tests", kind="test-failure"),
        verified("tests/t.py")]) + "\n", encoding="utf-8")
    out = proj / "packet.md"
    rc = rp.cmd_packet(type("A", (), dict(plan=str(proj / "plan.md"), root=str(proj), log=str(log), probes=None,
                                          out=str(out), tests=str(proj / "none.json"), max_tokens=20000,
                                          base=None))())
    text = out.read_text(encoding="utf-8")
    assert rc == 0
    assert "test_a" not in text.split("Resolved by a later event", 1)[0]  # not an open item
    assert "tests/t.py:5  KeyError: 99 - resolved:" in text
    assert "## Changed files (read these)\n- src/minisheet/workbook.py" in text
    assert "no tools" not in text and "one turn" not in text
    assert "not the boundary" in text
