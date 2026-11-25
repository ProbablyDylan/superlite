"""Shared helpers for running pytest from the superlite scripts. Stdlib only.

project_python(root): the interpreter with the project's dependencies (SUPERLITE_PROJECT_PYTHON, .venv/venv,
`uv run --frozen python` with uv.lock), or None.

pytest_cmd(root): command prefix that runs pytest. Uses the project's interpreter (project_python) when there is
one with pytest available, else the current interpreter when it can import pytest, else `uv run --no-project --with pytest python -m pytest`, else raises
PytestUnavailable with a clear message (`superlite-cli` may have fallen back to a bare
`uv run --no-project python`, which has no pytest).

run_tree(cmd, timeout, **kw): subprocess.run that, on timeout, kills the whole process
tree of the child it started (`base.kill_tree`, by PID), never by name. Raises subprocess.TimeoutExpired.

spawn_detached(cmd, log): start cmd detached (own session / process group, stdin closed,
output to the log file, UTF-8 forced) and return the Popen without waiting. The child owns
its process tree (it runs run_tree itself); callers only ever kill the PID they got back.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import base


class PytestUnavailable(RuntimeError):
    pass


def have_pytest():
    return importlib.util.find_spec("pytest") is not None


def _venv_python(root: Path) -> Path | None:  # O(1) stats
    exe = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    for d in (".venv", "venv"):
        if (root / d / exe).is_file():
            return root / d / exe
    return None


def venv_has(py: Path, package: str) -> bool:  # O(file) + O(1) stats, no process
    """Whether the venv of interpreter `py` has `package` in the site-packages its OWN Python version reads
    (pyvenv.cfg's version; a lib/python3.11 left over after an upgrade to 3.12 does not count)."""
    venv = py.parent.parent
    if os.name == "nt":
        return (venv / "Lib" / "site-packages" / package).is_dir()
    try:
        cfg = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        cfg = ""
    m = re.search(r"^version(?:_info)?\s*=\s*(\d+)\.(\d+)", cfg, re.M)
    pattern = f"lib/python{m.group(1)}.{m.group(2)}/site-packages/{package}" if m else f"lib/python*/site-packages/{package}"
    return any(venv.glob(pattern))


def project_python(root=".") -> list | None:  # O(P) PATH search
    """The interpreter that has the PROJECT's dependencies, as a command prefix, or None for a project with none of
    these: $SUPERLITE_PROJECT_PYTHON (a file, or a command such as `poetry run python`), else <root>/.venv or venv,
    else `uv run --frozen python` when uv.lock exists and uv is on PATH. Superlite's own interpreter (sys.executable) runs the
    scripts; it usually lacks the project's packages (numpy, ...), so project code never runs under it when this
    finds one."""
    env = os.environ.get("SUPERLITE_PROJECT_PYTHON", "").strip()
    if env:
        return [env] if os.path.isfile(env) else env.split()
    r = Path(root).resolve()
    py = _venv_python(r)
    if py:
        return [str(py)]
    uv = shutil.which("uv")
    if uv and (r / "uv.lock").is_file():
        return [uv, "run", "--frozen", "python"]  # --frozen: never rewrite the project's uv.lock
    return None


def has_xdist(root=".") -> bool:  # O(file) stats: pytest-xdist in the interpreter pytest_cmd(root) picks
    venv = _venv_python(Path(root).resolve())
    if venv and venv_has(venv, "pytest") and not os.environ.get("SUPERLITE_PROJECT_PYTHON", "").strip():
        return venv_has(venv, "xdist")
    return importlib.util.find_spec("xdist") is not None


def pytest_cmd(root="."):  # O(P) PATH search
    """pytest under the project's interpreter (project_python) when it has one, else under superlite's own."""
    proj = project_python(root)
    venv = _venv_python(Path(root).resolve())
    if proj and proj[1:] == ["run", "--frozen", "python"] and Path(proj[0]).stem == "uv":  # project env + pytest if it lacks one
        return [proj[0], "run", "--frozen", "--with", "pytest", "python", "-m", "pytest"]
    if proj and not (venv and proj == [str(venv)] and not venv_has(venv, "pytest")):  # a venv without pytest falls through
        return [*proj, "-m", "pytest"]
    if have_pytest():
        return [sys.executable, "-m", "pytest"]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--no-project", "--with", "pytest", "python", "-m", "pytest"]
    raise PytestUnavailable("pytest is not installed for this interpreter "
                            f"({sys.executable}) and uv is not on PATH; "
                            "install pytest (pip install pytest) or uv")


def run_tree(cmd, timeout=None, **kw):  # O(a + o) + process spawn; on timeout adds O(D) kill
    if os.name == "nt":
        kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    if kw.pop("capture_output", False):
        kw["stdout"] = kw["stderr"] = subprocess.PIPE
    with subprocess.Popen(cmd, **kw) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            base.kill_tree(proc.pid)
            proc.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout, e.output, e.stderr) from None
        finally:  # an interrupt or any other error leaves the child running: kill its tree
            if proc.poll() is None:
                base.kill_tree(proc.pid)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def spawn_detached(cmd, log, **kw):  # O(a) + process spawn; does not wait
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if os.name == "nt":
        kw["creationflags"] = (kw.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
                               | subprocess.CREATE_NO_WINDOW)  # never DETACHED_PROCESS: it makes CREATE_NO_WINDOW ignored and every console child pops a window
    else:
        kw["start_new_session"] = True
    with open(log, "ab") as fh:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                env=env, close_fds=True, **kw)
