"""checkpoint never refuses over files outside Owns: it saves the task's own files, warns about the rest, takes extra
files by --include, finds the run's plan when --plan is left out, and import-checks under the project's interpreter."""
import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1] / "skills" / "swarm" / "scripts"
PLAN_FIX = HERE / "fixtures" / "run_plan"
sys.path.insert(0, str(SCRIPTS))
ENV = {k: v for k, v in os.environ.items() if k != "SUPERLITE_PROJECT_PYTHON"}
ENV.update({"SUPERLITE_CLI": str(PLAN_FIX / "fake_cli"), "SL_TEST_PYTHON": sys.executable,
            "SUPERLITE_PROBE_RUN": str(PLAN_FIX / "no_such_probe.py")})


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def make_repo(tmp_path, plan_at="plan.md"):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / plan_at).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(PLAN_FIX / "plan.md", repo / plan_at)
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def write(repo, rel, text="X = 1\n"):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def cp(repo, *args, env=None):
    return subprocess.run([sys.executable, str(SCRIPTS / "checkpoint.py"), *map(str, args)], cwd=repo,
                          capture_output=True, text=True, encoding="utf-8", env=env or ENV)


def tree(repo, sha):
    return sorted(git(repo, "show", "--name-only", "--format=", sha).split())


def incidents(repo):
    p = repo / ".superlite" / "incidents.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ------------------------------------------------------------------ 1. out-of-scope files warn, never refuse

def test_out_of_scope_file_is_left_out_and_the_task_is_saved(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    write(repo, "layout.json", "{}\n")  # the user's editor saved a file no task owns
    r = cp(repo, "T1", "--plan", "plan.md")
    assert r.returncode == 0, r.stdout + r.stderr
    sha = r.stdout.strip()
    assert len(sha) == 40 and tree(repo, sha) == ["src/a.py"]  # stdout stays the bare sha
    assert "out-of-scope layout.json" in r.stderr and f"checkpoint T1 {sha[:10]}" in r.stderr
    rows = incidents(repo)
    assert [x["file"] for x in rows] == ["layout.json"] and rows[0]["resolution"] == "noted"
    assert f"checkpoint T1 {sha[:10]}" in (repo / ".superlite" / "progress.md").read_text(encoding="utf-8")


def test_only_out_of_scope_changes_still_say_nothing_to_save(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "stray.txt", "oops\n")
    r = cp(repo, "T1", "--plan", "plan.md")
    assert r.returncode == 1 and "out-of-scope stray.txt" in r.stderr and "nothing to checkpoint for T1" in r.stderr
    assert git(repo, "for-each-ref", "refs/superlite") == ""


def test_several_tasks_each_print_their_sha_despite_strays(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    write(repo, "src/b.py")
    write(repo, "notes.md", "coordinator notes\n")
    r = cp(repo, "T1", "T2", "--plan", "plan.md")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.split("\n")
    assert lines[0].startswith("T1 ") and lines[1].startswith("T2 ") and "FAILED" not in r.stdout


# ------------------------------------------------------------------ 2. --include

def test_include_adds_a_named_file_outside_owns(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    write(repo, "tests/test_other.py", "def test_x():\n    assert True\n")  # a test the fixer had to fix
    write(repo, "stray.txt")
    r = cp(repo, "T1", "--include", "tests/test_other.py", "--plan", "plan.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert tree(repo, r.stdout.strip()) == ["src/a.py", "tests/test_other.py"]
    assert "out-of-scope tests/test_other.py" not in r.stderr and "out-of-scope stray.txt" in r.stderr


def test_include_takes_another_tasks_file_with_a_note_and_flags_unchanged_paths(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    write(repo, "src/b.py")  # T2 owns it
    r = cp(repo, "--include", "./src/b.py", "--include", "README.md", "T1", "--plan", "plan.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert tree(repo, r.stdout.strip()) == ["src/a.py", "src/b.py"]
    assert "src/b.py is owned by T2" in r.stderr
    assert "--include README.md: no change" in r.stderr


def test_include_alone_is_enough_to_save(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "tests/helpers/util.py")
    r = cp(repo, "T1", "--include", "tests/helpers/", "--plan", "plan.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert tree(repo, r.stdout.strip()) == ["tests/helpers/util.py"]


# ------------------------------------------------------------------ 3. --plan is found when left out

def test_plan_left_out_reuses_the_runs_recorded_plan_and_keeps_the_chain(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    first = cp(repo, "T1", "--plan", "plan.md")
    assert first.returncode == 0, first.stderr
    write(repo, "src/b.py")
    second = cp(repo, "T2")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "plan: plan.md" in second.stderr
    st = json.loads((repo / ".superlite" / "checkpoints.json").read_text(encoding="utf-8"))
    assert [e["task"] for e in st["entries"]] == ["T1", "T2"]  # the same run, not a reset
    assert git(repo, "rev-parse", second.stdout.strip() + "^") == first.stdout.strip()


def test_plan_left_out_finds_the_one_plan_under_docs_plans(tmp_path):
    repo = make_repo(tmp_path, plan_at="docs/plans/2026-11-01-thing.md")
    write(repo, "docs/plans/2026-11-01-thing.review.md", "### T1 x\n")  # a companion is never the plan
    write(repo, "src/a.py")
    r = cp(repo, "T1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "plan: docs/plans/2026-11-01-thing.md" in r.stderr


def test_plan_left_out_and_none_found_names_the_flag(tmp_path):
    repo = make_repo(tmp_path, plan_at="elsewhere/plan.md")
    write(repo, "src/a.py")
    r = cp(repo, "T1")
    assert r.returncode == 2 and "--plan" in r.stderr and "file not found: .superlite/plan.md" not in r.stderr


# ------------------------------------------------------------------ 4. the import check runs on the project's interpreter

def _venv_with(repo, pkg):
    venv.create(repo / ".venv", with_pip=False)
    bindir = "Scripts" if os.name == "nt" else "bin"
    py = repo / ".venv" / bindir / ("python.exe" if os.name == "nt" else "python")
    site = subprocess.run([str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, check=True).stdout.strip()
    (Path(site) / pkg).mkdir(parents=True)
    (Path(site) / pkg / "__init__.py").write_text("VALUE = 1\n")


def test_import_check_uses_the_project_venv(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".gitignore").write_text(".venv/\n")
    _venv_with(repo, "venvonlylib_xyz")
    write(repo, "src/a.py", "import venvonlylib_xyz\nV = venvonlylib_xyz.VALUE\n")
    r = cp(repo, "T1", "--plan", "plan.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "venvonlylib_xyz" not in r.stdout + r.stderr  # imported for real, not excused as missing


def test_missing_third_party_module_warns_but_a_missing_project_module_fails(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py", "import thirdparty_not_installed_xyz\n")
    r = cp(repo, "T1", "--plan", "plan.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "warning: import src/a.py:1" in r.stderr and "thirdparty_not_installed_xyz" in r.stderr
    write(repo, "src/a.py", "import src.not_here_xyz\n")  # the project's own package: a real failure
    r = cp(repo, "T1", "--plan", "plan.md")
    assert r.returncode == 1 and "import src/a.py:1" in r.stdout and "ModuleNotFoundError" in r.stdout


def test_an_unrunnable_project_python_falls_back_with_a_warning(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py", "import json\n")
    r = cp(repo, "T1", "--plan", "plan.md", env={**ENV, "SUPERLITE_PROJECT_PYTHON": str(tmp_path / "no" / "python")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "import check under" in r.stderr


def test_import_errors_keeps_its_contract(tmp_path, monkeypatch):
    import checkpoint
    (tmp_path / "m.py").write_text("raise RuntimeError('boom')\n")
    (tmp_path / "n.py").write_text("import thirdparty_not_installed_xyz\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SUPERLITE_PROJECT_PYTHON", raising=False)
    assert [e[0] for e in checkpoint.import_errors(["m.py", "n.py"])] == ["m.py"]


def test_non_python_changes_never_touch_an_interpreter(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "docs/guide.md", "# guide\n")  # T4 owns docs/
    r = cp(repo, "T4", "--plan", "plan.md", env={**ENV, "SUPERLITE_PROJECT_PYTHON": str(tmp_path / "no" / "python")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "import" not in r.stderr


# ------------------------------------------------------------------ 5. review fixes

def test_a_missing_module_under_the_project_interpreter_fails(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py", "import helpres_typo_xyz\n")  # a typo, or a deleted project module
    r = cp(repo, "T1", "--plan", "plan.md", env={**ENV, "SUPERLITE_PROJECT_PYTHON": sys.executable})
    assert r.returncode == 1 and "import src/a.py:1" in r.stdout and "helpres_typo_xyz" in r.stdout


def test_a_missing_module_after_falling_back_warns_and_says_so(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py", "import helpres_typo_xyz\n")
    r = cp(repo, "T1", "--plan", "plan.md", env={**ENV, "SUPERLITE_PROJECT_PYTHON": str(tmp_path / "no" / "python")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fell back to superlite's own Python" in r.stderr and "helpres_typo_xyz" in r.stderr


NEW_PLAN = "### T7 · later · Complexity 1 · Est. context 5k · Est. time 1 min\n\n**Owns:** `src/g.py`\n"


def test_a_recorded_plan_without_the_task_is_skipped(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    assert cp(repo, "T1", "--plan", "plan.md").returncode == 0  # an earlier run recorded plan.md
    write(repo, "docs/plans/2026-11-24-next.md", "# Next\n\n## 9. Implementation plan\n\n" + NEW_PLAN)
    write(repo, "src/g.py")
    r = cp(repo, "T7")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "plan: docs/plans/2026-11-24-next.md" in r.stderr and "plan.md (checkpoints.json) has no heading for T7" in r.stderr
    assert tree(repo, r.stdout.strip()) == ["src/g.py"]


def test_the_plan_lint_last_froze_wins_over_an_earlier_run_reusing_its_ids(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    assert cp(repo, "T1", "--plan", "plan.md").returncode == 0  # an earlier run recorded plan.md, which has a T1
    nxt = "docs/plans/2026-11-25-next.md"
    write(repo, nxt, "# Next\n\n## 9. Implementation plan\n\n" + NEW_PLAN.replace("T7", "T1"))
    write(repo, ".superlite/stable.json", json.dumps({"stable": ["T1"], "plan": nxt}))  # lint --stable of the new plan
    write(repo, "src/g.py")
    r = cp(repo, "T1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"plan: {nxt}" in r.stderr and tree(repo, r.stdout.strip()) == ["src/g.py"]


def test_one_plan_spelled_two_ways_keeps_the_runs_chain(tmp_path):
    repo = make_repo(tmp_path, plan_at="docs/plans/p.md")
    write(repo, ".superlite/stable.json", json.dumps({"stable": ["T1", "T2"], "plan": "docs/plans/p.md"}))
    write(repo, "src/a.py")
    assert cp(repo, "T1", "--plan", str(repo / "docs" / "plans" / "p.md")).returncode == 0  # absolute $PLAN
    write(repo, "src/b.py")
    r = cp(repo, "T2")  # no --plan: stable.json's relative spelling
    assert r.returncode == 0, r.stdout + r.stderr
    st = json.loads((repo / ".superlite" / "checkpoints.json").read_text(encoding="utf-8"))
    assert [e["task"] for e in st["entries"]] == ["T1", "T2"]


def test_no_plan_with_the_task_asks_for_plan(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    assert cp(repo, "T1", "--plan", "plan.md").returncode == 0
    write(repo, "src/g.py")
    r = cp(repo, "T7")
    assert r.returncode == 2 and "--plan" in r.stderr and "has no heading for T7" in r.stderr


def test_an_included_file_is_not_warned_again_once_saved(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    write(repo, "tests/test_other.py", "def test_x():\n    assert True\n")
    assert cp(repo, "T1", "--include", "tests/test_other.py", "--plan", "plan.md").returncode == 0
    write(repo, "src/b.py")
    r = cp(repo, "T2", "--plan", "plan.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "warning: out-of-scope" not in r.stderr and incidents(repo) == []
    write(repo, "tests/test_other.py", "def test_x():\n    assert 1\n")  # changed again since: warn again
    write(repo, "src/c.py")
    r = cp(repo, "T3", "--plan", "plan.md")
    assert r.returncode == 0 and "out-of-scope tests/test_other.py" in r.stderr


def test_an_included_file_is_not_warned_by_the_next_task_in_the_same_call(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "src/a.py")
    write(repo, "src/b.py")
    write(repo, "notes.txt", "kept\n")
    r = cp(repo, "T1", "T2", "--include", "notes.txt", "--plan", "plan.md")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "warning: out-of-scope" not in r.stderr and incidents(repo) == []
