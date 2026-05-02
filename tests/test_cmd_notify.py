"""Tests for notification preferences CLI."""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from cmd_notify import cmd_notify


class TestCmdNotify:
    def test_show_prints_current_settings(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "notification_prefs.json"
        monkeypatch.setattr("cmd_notify.NOTIFICATION_PREFS_PATH", path)

        cmd_notify(Namespace(notify_cmd=None))

        out = capsys.readouterr().out
        assert "Current notification settings:" in out
        assert "Max nudges per day: 2" in out
        assert "/notify no nudges before 11am" in out

    def test_reset_all_clears_overrides_and_mutes(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "notification_prefs.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "overrides": {"nudges": {"max_per_day": 4}},
                    "temporary_mutes": [
                        {
                            "target": "nudges",
                            "expires_at": "2099-01-01T10:00:00+00:00",
                            "source_text": "mute nudges",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("cmd_notify.NOTIFICATION_PREFS_PATH", path)

        cmd_notify(Namespace(notify_cmd="reset", target="all"))

        saved = json.loads(path.read_text(encoding="utf-8"))
        out = capsys.readouterr().out
        assert saved["overrides"] == {}
        assert saved["temporary_mutes"] == []
        assert "Reset notification settings: all." in out
        assert "Max nudges per day: 2" in out

    def test_reset_one_section_preserves_other_overrides(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "notification_prefs.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "overrides": {
                        "nudges": {"max_per_day": 4},
                        "weekly_insights": {"weekday": "tuesday"},
                    },
                    "temporary_mutes": [],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("cmd_notify.NOTIFICATION_PREFS_PATH", path)

        cmd_notify(Namespace(notify_cmd="reset", target="nudges"))

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert "nudges" not in saved["overrides"]
        assert saved["overrides"]["weekly_insights"]["weekday"] == "tuesday"
        assert "Reset notification settings: nudges." in capsys.readouterr().out
