"""Small CLI rough edges agents hit in real runs: incident kind words, bare subcommands, `sl status`."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "swarm" / "scripts"
CLI = SCRIPTS / "superlite-cli"
sys.path.insert(0, str(SCRIPTS))

import incident  # noqa: E402


@pytest.mark.parametrize("word,kind", [("bug", "exception"), ("fixed", "verified"), ("Failure", "test-failure"),
                                       ("perf", "perf")])
def test_incident_kind_words_map_to_known_kinds(word, kind):
    assert incident.make_entry("a.py", 1, "x", kind=word)["kind"] == kind


def test_incident_unknown_kind_still_lists_the_kinds():
    with pytest.raises(ValueError, match="kind must be one of: test-failure"):
        incident.make_entry("a.py", 1, "x", kind="banana")


@pytest.mark.parametrize("args", [["status"], ["topics"], ["orchestrator"]])
def test_bare_commands_print_state_not_usage(tmp_path, args):
    env = dict(os.environ, SUPERLITE_HOME=str(tmp_path / "home"))
    cp = subprocess.run(["bash", str(CLI), *args], capture_output=True, text=True, cwd=tmp_path, env=env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "unknown tool" not in cp.stderr and "arguments are required" not in cp.stderr
