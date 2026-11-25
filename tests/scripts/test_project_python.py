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
