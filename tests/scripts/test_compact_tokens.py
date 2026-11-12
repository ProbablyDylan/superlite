import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "swarm" / "scripts"))
import compact_due  # noqa: E402
import project_config  # noqa: E402


@pytest.fixture(autouse=True)
def _noenv(monkeypatch):
    for k in ("SUPERLITE_COMPACT_TOKENS", "SUPERLITE_CONTEXT_WINDOW", "SUPERLITE_COMPACT_SHARE"):
        monkeypatch.delenv(k, raising=False)


def _cfg(tmp_path, value, key="compact_tokens"):
    (tmp_path / "superlite.json").write_text(json.dumps({"version": 1, key: value}))


def _prefs(data):
    home = Path(os.environ["SUPERLITE_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "preferences.json").write_text(json.dumps(data))


def test_default_limit_is_70_percent_of_a_1m_window(tmp_path):
    assert compact_due.limit(str(tmp_path)) == 700000
    assert compact_due.step(str(tmp_path)) == 100000


def test_window_and_share_from_config(tmp_path):
    (tmp_path / "superlite.json").write_text(json.dumps({"version": 1, "context_window": 200000,
                                                         "compact_share": 0.5}))
    assert compact_due.limit(str(tmp_path)) == 100000
    assert compact_due.step(str(tmp_path)) == 20000


def test_share_as_percent_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERLITE_COMPACT_SHARE", "60")
    assert compact_due.limit(str(tmp_path)) == 600000
    monkeypatch.setenv("SUPERLITE_CONTEXT_WINDOW", "200000")
    assert compact_due.limit(str(tmp_path)) == 120000


def test_preferences_set_the_machine_default_and_project_wins(tmp_path):
    _prefs({"guard": "light", "context_window": 200000})
    assert compact_due.limit(str(tmp_path)) == 140000
    _cfg(tmp_path, 400000, key="context_window")
    assert compact_due.limit(str(tmp_path)) == 280000


def test_absolute_tokens_beat_the_share(tmp_path):
    (tmp_path / "superlite.json").write_text(json.dumps({"version": 1, "compact_tokens": 150000,
                                                         "compact_share": 0.9}))
    assert compact_due.limit(str(tmp_path)) == 150000


def test_bad_share_and_window_rejected(tmp_path, monkeypatch):
    for bad in ("lots", 0, 150, -1):
        _cfg(tmp_path, bad, key="compact_share")
        with pytest.raises(ValueError, match="compact_share"):
            compact_due.limit(str(tmp_path))
    _cfg(tmp_path, "big", key="context_window")
    with pytest.raises(ValueError, match="positive integer"):
        compact_due.limit(str(tmp_path))


def test_config_limit(tmp_path):
    _cfg(tmp_path, 50000)
    assert compact_due.limit(str(tmp_path)) == 50000


def test_env_beats_config(tmp_path, monkeypatch):
    _cfg(tmp_path, 50000)
    monkeypatch.setenv("SUPERLITE_COMPACT_TOKENS", "40000")
    assert compact_due.limit(str(tmp_path)) == 40000


def test_invalid_value_rejected(tmp_path, monkeypatch):
    for bad in ("lots", 0):
        _cfg(tmp_path, bad)
        with pytest.raises(ValueError, match="superlite.json must be a positive integer"):
            compact_due.limit(str(tmp_path))
    (tmp_path / "superlite.json").unlink()
    monkeypatch.setenv("SUPERLITE_COMPACT_TOKENS", "abc")
    with pytest.raises(ValueError, match="SUPERLITE_COMPACT_TOKENS must be a positive integer"):
        compact_due.limit(str(tmp_path))


def test_token_nudge_first():
    assert compact_due.token_nudge(162000, 150000, "/compact") == (
        "Context is 162k tokens (limit 150k): run /compact at the next good stopping point, "
        "then re-read .superlite/progress.md and continue."
    )


def test_token_nudge_repeat():
    assert compact_due.token_nudge(170000, 150000, "/compact", request=2) == (
        "Context still growing (reminder 2, 170k tokens, limit 150k): "
        "run /compact at the next good stopping point."
    )


def test_old_api_unchanged(tmp_path):
    assert compact_due.nudge_line() == (
        "Session context is stale: please compact now (host compact / summarize), "
        "then continue. Stamp .superlite/last-compact when done."
    )
    assert compact_due.is_due(str(tmp_path), 2000.0, 1800) is True
    assert compact_due.is_due(str(tmp_path), 100.0, 1800) is False


def test_project_config_accepts_compact_tokens(tmp_path):
    _cfg(tmp_path, 1000)
    assert project_config.load(tmp_path)["compact_tokens"] == 1000
    (tmp_path / "superlite.json").write_text('{"version": 1, "bogus": 1}')
    with pytest.raises(project_config.ProjectConfigError):
        project_config.load(tmp_path)
