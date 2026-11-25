#!/usr/bin/env python3
"""Checkpoint one or more tasks: record each task's Owns files (plus any --include), warn about anything else.

    checkpoint.py Tn [Tm ...] [--include PATH ...] [--plan P] [--ledger .superlite/progress.md]
                  [--log .superlite/incidents.jsonl]
                  [--dispatch [--pool .superlite/pool.json] [--running T3] [--cap 10] [--batch N]]

Several ids are checkpointed in order in one process. Output: the SHA for one task; `Tn <sha>`
or `Tn FAILED` per task for several. Each task's pool agent (`current: Tn`) gets `current`
cleared, `busy: false` and Tn in its `tasks`. `--dispatch` then prints `next:` and the
`dispatch` lines for the updated ledger; a dispatch failure only warns, the checkpoints stand.

A checkpoint is a hidden shadow git object (never on HEAD, the branch or the user's index)
stored under refs/superlite/<run>/<Tn>; its SHA is what the ledger, probes and seam checks
use; with something left out it also prints `checkpoint Tn <sha> saved ...` on stderr. `superlite-cli finish` deletes the refs at the end of the run.

--plan defaults to the run's plan (checkpoint_scope.resolve_plan). `--include PATH` (repeatable; a file or folder)
adds changed files outside Owns to the first task, even another task's (with a note).

Steps: scope check (changes outside every task's Owns are left out with a warning on stderr and a noted
incident, never refusing; files another task owns are left out, silently), py_compile plus an import check
on each touched .py file under the project's interpreter (checkpoint_import.check; a missing third-party
module only warns when superlite's own Python ran it), checkpoint with the task title, append `- checkpoint Tn <sha>
<title>` to the progress ledger, then launch in the background `superlite-cli probe_run
--checkpoint SHA` and, for every seam of the task graph (`superlite-cli plan_tools taskgraph
--format full`) with both sides now checkpointed, `superlite-cli pt_review contract --plan P
--seam Ta,Tb --log LOG` (mismatches become incidents, a pass logs `verified`). Exits 1
on syntax or import problems, or when nothing of the task changed. Run it from the repository root.

Scope rules: `tests/regression/` (probe_run's `test_probe_*.py`) is owned by whichever task is
being checkpointed. `.superlite/` is never staged and never blocks; `setup_exclude()` adds it
to `.git/info/exclude` on every run. Stdlib only.
"""

import argparse
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import next_tasks as nt  # noqa: E402
import base  # noqa: E402
import checkpoint_import as ci  # noqa: E402
import checkpoint_scope as cs  # noqa: E402
import gitignore  # noqa: E402
import sandbox  # noqa: E402
import state  # noqa: E402

DEFAULT_LEDGER = f"{state.DIR_NAME}/progress.md"
DEFAULT_LOG = f"{state.DIR_NAME}/incidents.jsonl"
# Never staged, never blocking: today's entries plus every language's dependency/build/cache path in config/gitignore.json,
# so a late or missing .gitignore cannot block a task on node_modules, target, .venv ... (the plan's Ignore line joins in main).
IGNORED_PARTS = tuple(dict.fromkeys((f"{state.DIR_NAME}/", "__pycache__/", *(e for e in gitignore.union_entries() if e.endswith("/") and "*" not in e))))
PLAN_IGNORE: list[str] = []  # entries of the plan's `**Ignore:**` header line, set once in main
SHARED_OWNS = ("tests/regression/",)  # generated probe regression tests: owned by the task being checkpointed
# The ledger line for one saved task, `- checkpoint Tn <sha> <title>`; groups: task, sha, title.
# Every ledger reader imports this one regex.
LEDGER_LINE = re.compile(r"^\s*[-*+]?\s*checkpoint\s+(T\d+[A-Za-z]?)\s+([0-9a-f]{7,40})\s*(.*?)\s*$")
STATE = f"{state.DIR_NAME}/checkpoints.json"
TEMP_INDEX = f"{state.DIR_NAME}/checkpoint.index"
REF_ROOT = "refs/superlite"
SETTINGS_LOCAL = ".claude/settings.local.json"  # written by exclusive.py (peer suspension); no task owns it
WORKTREE_HOME = ".claude/worktrees/"  # linked checkouts (superlite's and the Claude Code harness's): never this tree's changes
TASK_ID = re.compile(r"T\d+[A-Za-z]?")

def git(*args, check=True, env=None, cwd=None):  # O(output) + process spawn
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       encoding="utf-8", env={**os.environ, **env} if env else None, cwd=cwd)
    if check and r.returncode != 0:
        nt.die(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()}", 1)
    return r


SANDBOXES = f"{state.DIR_NAME}/sandboxes/"  # prototype sandboxes: ignored by git, saved with `add -f` when owned


def sandbox_files(keep):  # O(sandbox files)
    """Files of the open prototype sandbox that `keep` claims; git status never lists them (the folder is ignored)."""
    if not keep:
        return []
    try:
        sl = state.run_dir(Path.cwd())
        rel = sandbox.open_sandbox(sl)
        return [f for f in sandbox.files(Path.cwd(), Path.cwd() / rel) if keep(f) and not f.endswith(".pyc")] if rel else []
    except OSError:
        return []


def hard_ignored(path):  # O(len path)
    """Run bookkeeping and bytecode: never staged, even when a task's Owns would match."""
    p = "/" + path
    if path.startswith(SANDBOXES) and not path.endswith(".pyc") and "/__pycache__/" not in p:
        return False  # a prototype's files: staged when the task owns them
    return path.endswith(".pyc") or f"/{state.DIR_NAME}/" in p or "/__pycache__/" in p or path == SETTINGS_LOCAL or path.startswith(WORKTREE_HOME)


def ignored(path):  # O(d + entries)
    return path.endswith(".pyc") or path == SETTINGS_LOCAL or path.startswith(WORKTREE_HOME) or gitignore.ignored(path, PLAN_IGNORE)  # matcher built once per process


def claims(owns):  # O(f)
    """Predicate: does one of these Owns patterns name `path` explicitly? Patterns that start with `**` (Wave 0's
    `**/types.*`, `**/__init__.py`) are too generic to pull files out of dependency folders, so they never do."""
    explicit = [o for o in owns if not o.startswith("**")]
    return lambda path: nt.owns_match(path, explicit)


def changed_files(keep=None):  # O(m log m) + process spawn
    """Changed paths minus the never-blocking ones; a path `keep(path)` accepts (the task's own Owns) stays even when
    ignored, except run bookkeeping. Same single `git status`: no extra spawn."""
    r = git("status", "--porcelain", "-z", "--untracked-files=all", "--no-renames")
    files = []
    for entry in r.stdout.split("\0"):
        if len(entry) > 3:
            path = entry[3:].replace("\\", "/")
            if not ignored(path) or (keep and not hard_ignored(path) and keep(path)):
                files.append(path)
    return sorted(set(files + sandbox_files(keep) + deleted_sandbox_files(keep)))


def deleted_sandbox_files(keep):  # O(checkpointed sandbox files)
    if not keep:
        return []
    rel = sandbox.open_sandbox(state.run_dir(Path.cwd()))
    st = load_state()
    entries = st.get("entries") if isinstance(st, dict) else None
    parent = entries[-1].get("sha") if entries else None
    if not rel or not parent:
        return []
    tree = git("ls-tree", "-r", "--name-only", parent).stdout.splitlines()
    return [f for f in tree if sandbox.inside(rel, f) and keep(f) and not Path(f).exists()]


def syntax_errors(files):  # O(total size of m_py files)
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            try:
                py_compile.compile(f, cfile=os.path.join(tmp, "x.pyc"), doraise=True)
            except py_compile.PyCompileError as e:
                exc = e.exc_value
                line = getattr(exc, "lineno", 0) or 0
                msg = getattr(exc, "msg", None) or str(e)
                errors.append((f, line, f"SyntaxError: {msg}"))
    return errors


def import_errors(files):  # O(m_py * import cost) + 1-2 process spawns; failures only (see checkpoint_import.check)
    return ci.check(files, state.DIR_NAME)[0]


def is_module(path):  # O(len path)
    name = Path(path).name
    return (path.endswith(".py") and not name.startswith("test_")
            and not name.endswith("_test.py") and name != "conftest.py")


def append_ledger(ledger, line):
    p = Path(ledger)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists() or p.stat().st_size == 0
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        if new:
            f.write("# Progress ledger\n\n")
        f.write(line + "\n")


def setup_exclude(cwd=None):  # O(x) + process spawn
    """Add `.superlite/` to the repo's .git/info/exclude (idempotent); True if it did."""
    r = git("rev-parse", "--git-path", "info/exclude", check=False, cwd=cwd)
    if r.returncode != 0 or not r.stdout.strip():
        return False
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = Path(cwd or Path.cwd()) / p
    try:
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        if any(ln.strip() in (state.DIR_NAME, f"{state.DIR_NAME}/") for ln in old.splitlines()):
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(("" if not old or old.endswith("\n") else "\n") + f"{state.DIR_NAME}/\n")
    except OSError:
        return False
    return True


def spawn_background(cmd):
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_GROUP (not DETACHED: it makes NO_WINDOW ignored and console children pop windows)
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **kw)


def probe_categories(plan):  # O(L + categories)
    """Union of the plan's Review Focus categories, comma separated ("" if none); --app-type is not passed."""
    try:
        import pt_review
        cats = []
        for _pkg, cs in pt_review.focus_packages(pt_review.parse_plan(plan)):
            cats += [c for c in cs if c not in cats]
        return ",".join(cats)
    except base.advisory_errors():
        return ""


def launch_probes(sha, plan=None):  # O(L) + process spawn
    probe = Path(os.environ.get("SUPERLITE_PROBE_RUN")
                 or nt.SCRIPTS.parent / "probes" / "probe_run.py")
    if not probe.exists():
        return False
    extra = []
    cats = probe_categories(plan) if plan else ""
    if cats:
        extra = ["--categories", cats]
    spawn_background(nt.sl_command("probe_run", "--checkpoint", sha, *extra))
    return True


def release_agents(pool_path, tids):  # O(pool bytes)
    """In pool.json, the agent whose `current` is a checkpointed task: clear `current`, set
    `busy: false`, add the task to its `tasks`. No pool file: nothing to do."""
    p = Path(pool_path)
    if not tids or not p.exists():
        return
    try:
        pool = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"warning: pool not updated: {e}", file=sys.stderr)
        return
    done = set(tids)
    changed = False
    for ag in pool.get("agents", []):
        cur = ag.get("current") or []  # "Tn", a batch "Tn,Tm", or a list
        curs = [str(x).strip() for x in (cur if isinstance(cur, list) else str(cur).split(",")) if str(x).strip()]
        hit = [t for t in curs if t in done]
        if not hit:
            continue
        for t in hit:
            if t not in ag.setdefault("tasks", []):
                ag["tasks"].append(t)
        left = [t for t in curs if t not in done]  # a FAILED batch task keeps the agent on it
        if left:
            ag["current"] = ",".join(left)
        else:
            ag.pop("current", None)
            ag["busy"] = False
        changed = True
    if changed:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(pool, indent=1) + "\n", encoding="utf-8")
        tmp.replace(p)


def checkpointed_tasks(ledger):  # O(c)
    """Task ids with a checkpoint line in the ledger."""
    p = Path(ledger)
    if not p.exists():
        return set()
    out = set()
    for ln in p.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n"):
        m = LEDGER_LINE.match(ln)
        if m:
            out.add(m.group(1))
    return out


def seams(plan):  # O(n + e), plan parsed once per process (nt.taskgraph_full)
    """[(producer, consumer)] from the task graph; [] on failure."""
    try:
        data = nt.taskgraph_full(plan)
    except SystemExit:  # nt.die on a bad graph: checkpoint anyway, no seams
        print("warning: no seams (taskgraph failed)", file=sys.stderr)
        return []
    pairs = []
    for s in data.get("seams") or []:
        ids = TASK_ID.findall(s)
        if len(ids) == 2:
            pairs.append((ids[0], ids[1]))
    if not pairs:  # seams missing from the output: derive them from the graph edges
        for t, deps in (data.get("graph") or {}).items():
            pairs += [(d, t) for d in deps]
    return pairs


def launch_seam_checks(plan, task, ledger, log):  # O(c + s) + process spawn per matching seam
    """Background `pt_review contract --seam` for each seam of `task` now fully checkpointed.

    Seams that do not involve `task` were launched when their own later side was checkpointed."""
    done = checkpointed_tasks(ledger) | {task}
    launched = []
    for a_, b_ in seams(plan):
        if task in (a_, b_) and a_ in done and b_ in done:
            spawn_background(nt.sl_command("pt_review", "contract", "--plan", plan,
                                           "--seam", f"{a_},{b_}", "--log", log))
            launched.append(f"{a_},{b_}")
    return launched


def log_incidents(log, task, items):  # O(k) + one append (no spawn in process)
    """items: (file, line, msg) -> one batched incident write."""
    nt.add_incidents(log, [(f, line, f"{task}: {msg}", "exception", "checkpoint", "open")
                           for f, line, msg in items])


def head_sha():
    r = git("rev-parse", "--verify", "-q", "HEAD", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def load_state():
    try:
        return json.loads(Path(STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_checkpoint(tid, title, files, plan):  # O(files) + git spawns
    """Shadow-commit `files` on top of the previous checkpoint (HEAD for the first); return the sha.

    A temporary index holds the parent's tree plus the owned files, so HEAD, the branch,
    the user's index and the working tree are not touched. The ref is refs/superlite/<run>/<Tn>."""
    st = load_state()
    if not st or not cs.same_plan(st.get("plan"), plan):
        stem = Path(plan).stem if plan else "run"
        st = {"plan": plan, "run": f"{stem}-{date.today():%Y%m%d}", "head": head_sha(),
              "branch": git("symbolic-ref", "-q", "HEAD", check=False).stdout.strip(),
              "entries": []}
    parent = st["entries"][-1]["sha"] if st["entries"] else st["head"]
    idx = Path(TEMP_INDEX).resolve()
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": str(idx)}
    try:
        git("read-tree", parent, env=env) if parent else git("read-tree", "--empty", env=env)
        plain = [f for f in files if not f.startswith(SANDBOXES)]
        boxed = [f for f in files if f.startswith(SANDBOXES)]
        if plain:
            git("add", "-A", "--", *plain, env=env)
        if boxed:
            git("add", "-f", "-A", "--", *boxed, env=env)  # the sandbox folder is git-ignored
        tree = git("write-tree", env=env).stdout.strip()
    finally:
        idx.unlink(missing_ok=True)
    cmd = ["commit-tree", tree, "-m", f"{tid}: {title}"] + (["-p", parent] if parent else [])
    sha = git(*cmd).stdout.strip()
    git("update-ref", f"{REF_ROOT}/{st['run']}/{tid}", sha)
    st["entries"].append({"task": tid, "sha": sha, "title": f"{tid}: {title}", "files": files})
    p = Path(STATE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=1) + "\n", encoding="utf-8")
    return sha


PLAN_COMPANIONS = (".review.md", ".feature.md", ".mvp.md")  # the plans a run's plan was approved from


def approved_plans(plan):
    """The review/feature/mvp plan beside `docs/plans/<date>-<topic>.md`: part of the run, never in scope."""
    base_path = Path(plan)
    stem = base_path.name[:-3] if base_path.name.endswith(".md") else base_path.name
    return [base_path.with_name(stem + suffix) for suffix in PLAN_COMPANIONS]


def spawn_learn(plan):  # O(1) advisory: start `learn sync` detached, never wait for it
    try:
        import learn
        learn.spawn(Path(state.DIR_NAME), plan)
    except base.advisory_errors():
        pass


def checkpoint_one(a, tid, info, own_files):  # O(m*o + py size) + git spawns (status, add, commit-tree, update-ref)
    """Checkpoint one task; (exit code, sha or None). Files other tasks own are left alone."""
    if tid not in info:
        print(f"error: unknown task {tid}", file=sys.stderr)
        return 2, None
    task = info[tid]
    owns = task["owns"]
    if not owns:
        print(f"error: {tid} has no Owns line", file=sys.stderr)
        return 2, None
    owns = owns + [o for o in SHARED_OWNS if o not in owns]
    other = [o for t, i in info.items() if t != tid for o in i["owns"]
             if o not in SHARED_OWNS]
    if getattr(a, "gitignore_owner", None):  # the coordinator's script edited .gitignore: only the Wave 0 task takes it
        if tid == a.gitignore_owner:
            owns = owns + [".gitignore"]
        else:
            other = other + [".gitignore"]
    mine, out_of_scope = [], []
    extra = cs.include_match(a.include if tid == a.task[0] else [])  # --include: the caller named these files
    claimed = claims(owns)
    for f in changed_files(lambda path: claimed(path) or extra(path)):
        if extra(f) and not hard_ignored(f):
            mine.append(f)
            owner = next((t for t, i in info.items() if t != tid and nt.owns_match(f, i["owns"])), None)
            if owner:
                print(f"note: --include {f} is owned by {owner}; saved with {tid} as asked", file=sys.stderr)
        elif (f in own_files and not nt.owns_match(f, owns)) or (tid == "T0" and nt.owns_match(f, other)):
            continue  # run bookkeeping; Wave 0 globs never take another task's file
        elif nt.owns_match(f, owns):
            mine.append(f)
        elif not nt.owns_match(f, other):  # another task's file, built in parallel in this tree: left alone
            out_of_scope.append(f)
    for i in a.include if tid == a.task[0] else []:
        if not any(cs.include_match([i])(f) for f in mine):
            print(f"warning: --include {i}: no change to save", file=sys.stderr)
    out_of_scope = cs.unsaved(out_of_scope, load_state(), a.plan)  # an --include saved earlier is not warned again
    if out_of_scope:  # never refuses: warn, log a noted incident per top-level directory, save the task's own files
        cs.warn_out_of_scope(tid, out_of_scope, a.log)
    if not mine:
        print(f"nothing to checkpoint for {tid}", file=sys.stderr)
        return 1, None
    present = [f for f in mine if os.path.exists(f)]
    py = [f for f in present if f.endswith(".py")]
    problems = syntax_errors(py)
    if not problems:
        problems, missing = ci.check([f for f in py if is_module(f)], state.DIR_NAME)
        for f, line, msg in missing:
            print(f"warning: import {f}:{line} {msg}", file=sys.stderr)
    if problems:
        for f, line, msg in problems:
            print(f"{'syntax' if msg.startswith('SyntaxError') else 'import'} "
                  f"{f}:{line} {msg}")
        log_incidents(a.log, tid, [(f, line, f"{msg}; nothing saved")
                                   for f, line, msg in problems])
        return 1, None
    sha = save_checkpoint(tid, task["title"], mine, a.plan)  # hidden ref: HEAD, branch, index and tree untouched
    append_ledger(a.ledger, f"- checkpoint {tid} {sha[:10]} {task['title']}")
    if out_of_scope or a.include:
        print(f"checkpoint {tid} {sha[:10]} saved: {len(mine)} files, {len(out_of_scope)} out-of-scope left out", file=sys.stderr)
    spawn_learn(a.plan)
    launch_probes(sha, a.plan)
    launch_seam_checks(a.plan, tid, a.ledger, a.log)
    return 0, sha


def _sl_state(plan, name):
    import plan_tools as pt
    return state.path(pt.stable_root(plan), name)


def _edit_state(plan, name, default, edit):
    path = _sl_state(plan, name)
    if not path.exists() and default is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    edit(data)
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def write_questions(plan, task, agent=""):
    _edit_state(plan, "questions.json", {"waiting": {}}, lambda d: d.setdefault("waiting", {}).__setitem__(task, {"agent": agent}))


def clear_question(plan, task):
    _edit_state(plan, "questions.json", None, lambda d: d.get("waiting", {}).pop(task, None))


def write_blocked(plan, task, tier):
    _edit_state(plan, "blocked.json", {"blocked": {}}, lambda d: d.setdefault("blocked", {}).__setitem__(task, {"tier": tier}))


def main(argv=None):  # O(L + t*(m*o)) + git spawns per task; one process for all tasks
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("task", nargs="*", help="one or more task ids, checkpointed in order")
    ap.add_argument("--plan", help="the run's plan (default: the one the run recorded, see checkpoint_scope.resolve_plan)")
    ap.add_argument("--include", action="append", default=[], metavar="PATH",
                    help="also save this changed file or folder outside Owns with the first task (repeatable)")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--log", default=DEFAULT_LOG)
    ap.add_argument("--dispatch", action="store_true",
                    help="after the checkpoints, print `dispatch` output (next tasks and agents)")
    ap.add_argument("--pool", default=f"{state.DIR_NAME}/pool.json", help="pool for --dispatch")
    ap.add_argument("--running", help="running tasks for --dispatch (default: from the pool)")
    ap.add_argument("--cap", type=int, default=10, help="cap for --dispatch")
    ap.add_argument("--batch", type=int, help="most tasks per agent message for --dispatch (default: config; 1 = off)")
    ap.add_argument("--question", metavar="T", help="record that task T waits for a NEEDS_CONTEXT answer")
    ap.add_argument("--answered", metavar="T", help="clear the question wait for task T")
    ap.add_argument("--blocked", metavar="T", help="record that task T is BLOCKED")
    ap.add_argument("--tier", help="required fixer tier for --blocked (haiku|sonnet|opus)")
    ap.add_argument("--agent", default="", help="implementer id for --question")
    a = ap.parse_args(argv)
    a.include = [cs.norm_path(i) for i in a.include]
    a.plan, source = cs.resolve_plan(a.plan, a.task or [t for t in (a.question, a.answered, a.blocked) if t], a.pool)
    if source:
        print(f"plan: {a.plan} (from {source})", file=sys.stderr)
    found, a.plan = bool(a.plan), a.plan or nt.DEFAULT_PLAN  # --question/--answered/--blocked only need its folder
    if a.question:
        write_questions(a.plan, a.question, a.agent)
        print(f"question {a.question}")
        if not a.task:
            return 0
    if a.answered:
        clear_question(a.plan, a.answered)
        print(f"answered {a.answered}")
        if not a.task:
            return 0
    if a.blocked:
        if not a.tier:
            nt.die("--blocked needs --tier")
        write_blocked(a.plan, a.blocked, a.tier)
        print(f"blocked {a.blocked} tier={a.tier}")
        if not a.task:
            return 0
    if not a.task:
        nt.die("checkpoint needs a task id (or --question/--answered/--blocked)")
    if not found:
        nt.die(f"no plan found for this run with a heading for {', '.join(a.task)} (none recorded, or none of the recorded plans has them; no single docs/plans match): pass --plan <plan.md>")

    info = nt.parse_plan(a.plan)
    if "T0" not in info:  # a Wave 0 task synthesized by `wave0`
        import wave0
        w0 = wave0.synth_info(a.plan)
        if w0:
            info["T0"] = {"id": "T0", "title": w0["title"], "owns": w0["owns"], "body": ""}
    unknown = [t for t in a.task if t not in info]
    if len(a.task) == 1 and unknown:
        nt.die(f"unknown task {unknown[0]}")
    if len(a.task) == 1 and not info[a.task[0]]["owns"]:
        nt.die(f"{a.task[0]} has no Owns line")
    setup_exclude()
    try:  # the plan's Ignore line joins the never-blocking list
        import plan_tools
        PLAN_IGNORE[:] = plan_tools.Plan.load(a.plan).header_ignore() or []
    except Exception:  # noqa: BLE001
        pass
    import wave0
    a.gitignore_owner = wave0.gitignore_owner(a.plan)  # the Wave 0 task checkpoints the .gitignore its script wrote

    own_files = set()  # bookkeeping files of the run itself, never in scope
    for p in (a.plan, a.ledger, a.log, *approved_plans(a.plan)):
        try:
            own_files.add(Path(p).resolve().relative_to(Path.cwd().resolve()).as_posix())
        except ValueError:
            pass

    worst = 0
    saved = []
    for tid in a.task:
        code, sha = checkpoint_one(a, tid, info, own_files)  # see checkpoint_one
        worst = max(worst, code)
        if sha:
            saved.append(tid)
            print(sha if len(a.task) == 1 else f"{tid} {sha}")
        elif len(a.task) > 1:
            print(f"{tid} FAILED")
    release_agents(a.pool, saved)

    if a.dispatch:
        print("next:")
        try:  # the checkpoints stand whatever happens here
            import dispatch
            dispatch.render(*dispatch.run(a.plan, a.pool, a.ledger, None, a.running, a.cap, a.batch))  # see dispatch.run
        except (SystemExit, Exception) as e:
            print(f"warning: dispatch failed ({e}); run `dispatch` after fixing it", file=sys.stderr)
    return worst


if __name__ == "__main__":
    sys.exit(main())
