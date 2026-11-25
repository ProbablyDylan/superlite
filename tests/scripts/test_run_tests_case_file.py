"""run_tests.case_file never crashes on an empty or odd junit classname (pytest collection errors)."""
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "swarm" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import run_tests  # noqa: E402


def test_empty_classname_uses_the_dotted_module_in_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")
    assert run_tests.case_file({"classname": "", "name": "tests.test_x"}) == "tests/test_x.py"
    assert run_tests.case_file({"classname": "", "name": "tests.test_gone"}) == "tests.test_gone"
    assert run_tests.case_file({"classname": "", "name": ""}) == "unknown"
    assert run_tests.case_file({}) == "unknown"


def test_classname_with_empty_parts_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_y.py").write_text("", encoding="utf-8")
    assert run_tests.case_file({"classname": "tests..test_y.TestY", "name": "t"}) == "tests/test_y.py"
    assert run_tests.case_file({"classname": ".", "name": "t"}) == "t"
    assert run_tests.case_file({"classname": "", "name": "a./.b"}) == "a./.b"  # "/" is no module part
    assert run_tests.failure_line({"classname": "", "name": ""}, "boom", 7) == 7
