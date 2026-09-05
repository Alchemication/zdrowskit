"""Progress controls and check-in replies across restarts and write failures."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


from plan_frame import progress_paused, resolve_plan_frame
from store import open_db
from telegram_progress import (
    handle_checkin_callback,
    handle_checkin_reply,
    handle_targets,
)


class TestTelegramProgress:
    def _chat(self, tmp_path: Path) -> SimpleNamespace:
        context = tmp_path / "context"
        context.mkdir(exist_ok=True)
        log = context / "log.md"
        if not log.exists():
            log.write_text("# Weekly Log\n")
        daemon = SimpleNamespace(
            db=tmp_path / "health.db",
            context_dir=context,
            model_prefs_path=None,
            _self_originated_writes=set(),
        )
        poller = MagicMock()
        poller.send_reply.return_value = 77
        chat = SimpleNamespace(_daemon=daemon, _poller=poller)
        conn = open_db(daemon.db)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO checkin (week_start, asked_at, message_id) VALUES ('2026-08-31', '2026-09-04', 10)"
            )
        conn.close()
        return chat

    def test_note_survives_restart_and_is_not_duplicated(self, tmp_path: Path) -> None:
        chat = self._chat(tmp_path)
        handle_checkin_callback(chat, "cb", "checkin:2026-08-31:note", 10)
        restarted = self._chat(tmp_path)
        message = {
            "message_id": 78,
            "text": "Away with family",
            "reply_to_message": {"message_id": 77},
        }
        assert handle_checkin_reply(restarted, message)
        assert handle_checkin_reply(restarted, message)
        text = (tmp_path / "context/log.md").read_text()
        assert text.count("Away with family") == 1
        assert "week of 2026-08-31" in text
        conn = open_db(restarted._daemon.db)
        assert conn.execute("SELECT answered_at FROM checkin").fetchone()[0]
        conn.close()

    def test_failed_journal_save_leaves_reply_retryable(self, tmp_path: Path) -> None:
        chat = self._chat(tmp_path)
        handle_checkin_callback(chat, "cb", "checkin:2026-08-31:note", 10)
        message = {
            "message_id": 78,
            "text": "Travelling",
            "reply_to_message": {"message_id": 77},
        }
        with patch(
            "telegram_progress.apply_edit", side_effect=OSError("disk unavailable")
        ):
            assert handle_checkin_reply(chat, message)
        assert "Could not save" in chat._poller.send_reply.call_args.args[0]
        conn = open_db(chat._daemon.db)
        assert conn.execute("SELECT answered_at FROM checkin").fetchone()[0] is None
        conn.close()
        assert handle_checkin_reply(chat, message)
        assert "Travelling" in (tmp_path / "context/log.md").read_text()

    def test_unrelated_reply_is_not_consumed(self, tmp_path: Path) -> None:
        chat = self._chat(tmp_path)
        assert not handle_checkin_reply(
            chat,
            {
                "message_id": 78,
                "text": "hello",
                "reply_to_message": {"message_id": 99, "text": "One line is plenty"},
            },
        )

    def test_pause_survives_restart_and_resume_is_explicit(
        self, tmp_path: Path
    ) -> None:
        chat = self._chat(tmp_path)
        handle_targets(chat, "pause", 1)
        restarted = self._chat(tmp_path)
        conn = open_db(restarted._daemon.db)
        assert progress_paused(conn)
        with patch("llm.call_llm") as call:
            frame = resolve_plan_frame(
                conn, me=None, log=None, history=None, today=date.today().isoformat()
            )
        assert frame.mode == "hidden"
        call.assert_not_called()
        conn.close()
        handle_targets(restarted, "resume", 2)
        conn = open_db(restarted._daemon.db)
        assert not progress_paused(conn)
        conn.close()

    def test_wrong_checkin_message_cannot_write_journal(self, tmp_path: Path) -> None:
        chat = self._chat(tmp_path)
        handle_checkin_callback(chat, "cb", "checkin:2026-08-31:rest", 99)
        assert (tmp_path / "context/log.md").read_text() == "# Weekly Log\n"
