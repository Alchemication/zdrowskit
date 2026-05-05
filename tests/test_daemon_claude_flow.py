"""Tests for the Telegram Claude bridge."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from daemon_claude_flow import ClaudeRunError, run_claude_workspace


class TestRunClaudeWorkspace:
    def test_runs_print_mode_with_accept_edits(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"type":"result","subtype":"success",'
                    '"result":"Claude answer",'
                    '"session_id":"22222222-2222-2222-2222-222222222222"}'
                ),
                stderr="",
            )

        monkeypatch.delenv("ZDROWSKIT_CLAUDE_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_claude_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = run_claude_workspace("Where is the bot?", cwd=tmp_path)

        assert result.text == "Claude answer"
        assert result.session_id == "22222222-2222-2222-2222-222222222222"
        cmd = calls[0]
        assert cmd[0] == "claude"
        assert "--print" in cmd
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        assert "--permission-mode" in cmd
        assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
        assert cmd[-1] == "Where is the bot?"

    def test_uses_configured_claude_executable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(
                returncode=0,
                stdout='{"result":"hi","session_id":"abc"}',
                stderr="",
            )

        monkeypatch.setenv("ZDROWSKIT_CLAUDE_EXECUTABLE", "/opt/homebrew/bin/claude")
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = run_claude_workspace("Where is the bot?", cwd=tmp_path)

        assert result.text == "hi"
        assert calls[0][0] == "/opt/homebrew/bin/claude"

    def test_resumes_existing_session(self, tmp_path: Path, monkeypatch) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(
                returncode=0,
                stdout='{"result":"Follow-up answer","session_id":"existing-session"}',
                stderr="",
            )

        monkeypatch.delenv("ZDROWSKIT_CLAUDE_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_claude_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = run_claude_workspace(
            "Next question",
            cwd=tmp_path,
            session_id="existing-session",
        )

        assert result.text == "Follow-up answer"
        assert result.session_id == "existing-session"
        assert "--resume" in calls[0]
        assert calls[0][calls[0].index("--resume") + 1] == "existing-session"

    def test_streaming_uses_stream_json_and_emits_progress(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        progress: list[str] = []
        calls: list[list[str]] = []

        class FakePopen:
            def __init__(self, cmd: list[str], **kwargs: object) -> None:
                calls.append(cmd)
                self.stdout = io.StringIO(
                    '{"type":"system","subtype":"init",'
                    '"session_id":"22222222-2222-2222-2222-222222222222"}\n'
                    '{"type":"assistant","message":{"content":[{"type":"text","text":"Partial"}]}}\n'
                    '{"type":"result","subtype":"success","result":"Claude answer",'
                    '"session_id":"22222222-2222-2222-2222-222222222222"}\n'
                )
                self.stderr = io.StringIO("")

            def wait(self, timeout: int) -> int:
                return 0

            def kill(self) -> None:
                return

        monkeypatch.delenv("ZDROWSKIT_CLAUDE_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_claude_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        result = run_claude_workspace(
            "Where is the bot?",
            cwd=tmp_path,
            progress_callback=progress.append,
        )

        assert result.text == "Claude answer"
        assert result.session_id == "22222222-2222-2222-2222-222222222222"
        cmd = calls[0]
        assert cmd[0] == "claude"
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"
        assert "--verbose" in cmd
        assert "--include-partial-messages" in cmd
        assert "system init" in progress
        assert any(item.startswith("assistant") for item in progress)
        assert "final answer" in progress

    def test_raises_useful_error_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout="", stderr="auth failed")

        monkeypatch.delenv("ZDROWSKIT_CLAUDE_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_claude_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(ClaudeRunError, match="auth failed"):
            run_claude_workspace("hello", cwd=tmp_path)

    def test_raises_when_cli_returns_empty(self, tmp_path: Path, monkeypatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.delenv("ZDROWSKIT_CLAUDE_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_claude_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(ClaudeRunError, match="empty response"):
            run_claude_workspace("hello", cwd=tmp_path)
