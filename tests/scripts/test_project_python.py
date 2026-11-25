"""pytest runs under the project's interpreter (its venv or uv project), not superlite's own Python."""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _pyrun  # noqa: E402


def _venv(root, with_pytest):
    py = root / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    if with_pytest:
        (root / ".venv" / "lib" / "python3.12" / "site-packages" / "pytest").mkdir(parents=True)
    return py


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERLITE_PROJECT_PYTHON", "poetry run python")
    _venv(tmp_path, True)
    assert _pyrun.project_python(tmp_path) == ["poetry", "run", "python"]
    assert _pyrun.pytest_cmd(tmp_path) == ["poetry", "run", "python", "-m", "pytest"]


def test_venv_with_pytest_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_PROJECT_PYTHON", raising=False)
    py = _venv(tmp_path, True)
    assert _pyrun.pytest_cmd(tmp_path) == [str(py), "-m", "pytest"]


def test_venv_without_pytest_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_PROJECT_PYTHON", raising=False)
    py = _venv(tmp_path, False)
    assert _pyrun.project_python(tmp_path) == [str(py)]
    assert str(py) not in _pyrun.pytest_cmd(tmp_path)


def test_uv_project_keeps_its_dependencies(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_PROJECT_PYTHON", raising=False)
    monkeypatch.setattr(_pyrun.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    assert _pyrun.pytest_cmd(tmp_path) == ["/bin/uv", "run", "--frozen", "--with", "pytest", "python", "-m", "pytest"]
    assert "--no-project" not in _pyrun.pytest_cmd(tmp_path)


def test_no_project_environment_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_PROJECT_PYTHON", raising=False)
    assert _pyrun.project_python(tmp_path) is None


def test_venv_package_must_match_the_venvs_own_python_version(tmp_path, monkeypatch):
    """After a system Python upgrade a venv can keep lib/python3.11/site-packages/pytest that its python3.12 never
    reads: that pytest does not count."""
    monkeypatch.delenv("SUPERLITE_PROJECT_PYTHON", raising=False)
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.12.4\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib" / "python3.11" / "site-packages" / "pytest").mkdir(parents=True)
    assert not _pyrun.venv_has(py, "pytest")
    assert str(py) not in _pyrun.pytest_cmd(tmp_path)
    (tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "pytest").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "xdist").mkdir(parents=True)
    assert _pyrun.pytest_cmd(tmp_path) == [str(py), "-m", "pytest"]
    assert _pyrun.has_xdist(tmp_path)


def test_uv_project_whose_venv_lacks_pytest_uses_uv(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERLITE_PROJECT_PYTHON", raising=False)
    monkeypatch.setattr(_pyrun.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    _venv(tmp_path, False)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    assert _pyrun.pytest_cmd(tmp_path) == ["/bin/uv", "run", "--frozen", "--with", "pytest", "python", "-m", "pytest"]
    (tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "xdist").mkdir(parents=True)
    assert _pyrun.has_xdist(tmp_path)  # uv runs the project's .venv: its xdist counts


def test_requirements_txt_project_gets_a_uv_env_with_its_requirements(tmp_path, monkeypatch):
    """ArchivistBot: no venv, no uv.lock; its deps (gallery_dl) come from requirements.txt."""
    monkeypatch.delenv("SUPERLITE_PROJECT_PYTHON", raising=False)
    monkeypatch.setattr(_pyrun.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    (tmp_path / "requirements.txt").write_text("gallery-dl\n", encoding="utf-8")
    req = str(tmp_path.resolve() / "requirements.txt")
    assert _pyrun.project_python(tmp_path) == ["/bin/uv", "run", "--no-project", "--with-requirements", req, "python"]
    assert _pyrun.pytest_cmd(tmp_path) == ["/bin/uv", "run", "--no-project", "--with-requirements", req,
                                           "--with", "pytest", "python", "-m", "pytest"]
