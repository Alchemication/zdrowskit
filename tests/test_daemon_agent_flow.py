"""Tests for the Telegram Codex bridge."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from daemon_agent_flow import CodexRunError, run_codex_readonly


class TestRunCodexReadonly:
    def test_starts_readonly_session_and_reads_last_message(
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

        result = run_codex_readonly("Where is the bot?", cwd=tmp_path)

        assert result.text == "Codex answer"
        assert result.session_id == "11111111-1111-1111-1111-111111111111"
        cmd = calls[0]
        assert cmd[:2] == ["codex", "exec"]
        assert "--sandbox" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
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

        result = run_codex_readonly("Where is the bot?", cwd=tmp_path)

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

        result = run_codex_readonly(
            "Next question",
            cwd=tmp_path,
            session_id="existing-session",
        )

        assert result.text == "Follow-up answer"
        assert result.session_id == "existing-session"
        assert calls[0][:4] == ["codex", "exec", "resume", "--json"]
        assert "existing-session" in calls[0]

    def test_raises_useful_error_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout="", stderr="auth failed")

        monkeypatch.delenv("ZDROWSKIT_CODEX_EXECUTABLE", raising=False)
        monkeypatch.setattr("daemon_agent_flow.shutil.which", lambda name: None)
        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(CodexRunError, match="auth failed"):
            run_codex_readonly("hello", cwd=tmp_path)
