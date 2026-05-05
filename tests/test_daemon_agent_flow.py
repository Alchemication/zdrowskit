"""Tests for the Telegram Codex bridge."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from daemon_agent_flow import CodexRunError, run_codex_workspace


class TestRunCodexWorkspace:
    def test_starts_workspace_write_session_and_reads_last_message(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(cmd)
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text("Codex answer\n", encoding="utf-8")
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"type":"session_configured",'
                    '"session_id":"11111111-1111-1111-1111-111111111111"}\n'
                ),
                stderr="",
            )

        monkeypatch.delenv("ZDROWSKIT_CODEX_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_agent_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = run_codex_workspace("Where is the bot?", cwd=tmp_path)

        assert result.text == "Codex answer"
        assert result.session_id == "11111111-1111-1111-1111-111111111111"
        cmd = calls[0]
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--sandbox" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
        assert "--ask-for-approval" in cmd
        assert cmd[cmd.index("--ask-for-approval") + 1] == "never"

    def test_uses_configured_codex_executable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(cmd)
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text("Codex answer\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setenv("ZDROWSKIT_CODEX_EXECUTABLE", "/opt/homebrew/bin/codex")
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = run_codex_workspace("Where is the bot?", cwd=tmp_path)

        assert result.text == "Codex answer"
        assert calls[0][0] == "/opt/homebrew/bin/codex"

    def test_resumes_existing_session(self, tmp_path: Path, monkeypatch) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append(cmd)
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text("Follow-up answer\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.delenv("ZDROWSKIT_CODEX_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_agent_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        result = run_codex_workspace(
            "Next question",
            cwd=tmp_path,
            session_id="existing-session",
        )

        assert result.text == "Follow-up answer"
        assert result.session_id == "existing-session"
        exec_index = calls[0].index("exec")
        assert calls[0][exec_index : exec_index + 3] == ["exec", "resume", "--json"]
        assert "existing-session" in calls[0]

    def test_streaming_emits_progress_from_jsonl(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        progress: list[str] = []
        calls: list[list[str]] = []

        class FakePopen:
            def __init__(self, cmd: list[str], **kwargs: object) -> None:
                calls.append(cmd)
                self.cmd = cmd
                self.stdout = io.StringIO(
                    '{"type":"session_configured",'
                    '"session_id":"11111111-1111-1111-1111-111111111111"}\n'
                    '{"type":"agent_message","message":"Checking files"}\n'
                )
                self.stderr = io.StringIO("")

            def wait(self, timeout: int) -> int:
                output_path = Path(
                    self.cmd[self.cmd.index("--output-last-message") + 1]
                )
                output_path.write_text("Codex answer\n", encoding="utf-8")
                return 0

            def kill(self) -> None:
                return

        monkeypatch.delenv("ZDROWSKIT_CODEX_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_agent_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        result = run_codex_workspace(
            "Where is the bot?",
            cwd=tmp_path,
            progress_callback=progress.append,
        )

        assert result.text == "Codex answer"
        assert result.session_id == "11111111-1111-1111-1111-111111111111"
        assert "session configured" in progress
        assert "agent message: Checking files" in progress
        assert calls[0][0] == "codex"

    def test_raises_useful_error_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout="", stderr="auth failed")

        monkeypatch.delenv("ZDROWSKIT_CODEX_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_agent_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(CodexRunError, match="auth failed"):
            run_codex_workspace("hello", cwd=tmp_path)
