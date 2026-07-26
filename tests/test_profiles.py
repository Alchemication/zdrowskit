"""Profile roster, path isolation, CLI resolution, and Telegram routing tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from profiles import (
    Profile,
    ProfileConfigError,
    add_profile,
    adopt_existing,
    load_profiles,
    resolve_cli_profile,
    resolve_profile,
)
from store import open_db


def _write_roster(path: Path, body: str) -> None:
    path.write_text(body.strip() + "\n", encoding="utf-8")


class TestLoadProfiles:
    def test_loads_defaults_and_derived_paths(self, tmp_path: Path) -> None:
        roster = tmp_path / "profiles.toml"
        _write_roster(
            roster,
            """
            [profiles.adam]
            telegram_id = 11
            operator = true
            import_source = "local"

            [profiles.anna]
            telegram_id = 22
            enabled = false
            """,
        )

        profiles = load_profiles(roster)

        assert profiles["adam"].enabled is True
        assert profiles["anna"].import_source == "google-drive"
        assert profiles["anna"].db == tmp_path / "profiles" / "anna" / "health.db"
        assert profiles["adam"].state.name == "daemon_state.json"

    @pytest.mark.parametrize("name", ["Adam", "../adam", "a.b", ""])
    def test_rejects_unsafe_names(self, tmp_path: Path, name: str) -> None:
        roster = tmp_path / "profiles.toml"
        _write_roster(
            roster,
            f"""
            [profiles."{name}"]
            telegram_id = 11
            operator = true
            import_source = "local"
            """,
        )

        with pytest.raises(ProfileConfigError, match="Invalid profile name"):
            load_profiles(roster)

    @pytest.mark.parametrize(
        "body",
        [
            """
            [profiles.adam]
            telegram_id = 11
            """,
            """
            [profiles.adam]
            telegram_id = 11
            operator = true
            import_source = "local"
            [profiles.anna]
            telegram_id = 22
            operator = true
            """,
        ],
    )
    def test_requires_exactly_one_operator(self, tmp_path: Path, body: str) -> None:
        roster = tmp_path / "profiles.toml"
        _write_roster(roster, body)

        with pytest.raises(ProfileConfigError, match="exactly one operator"):
            load_profiles(roster)

    def test_rejects_local_import_for_non_operator(self, tmp_path: Path) -> None:
        roster = tmp_path / "profiles.toml"
        _write_roster(
            roster,
            """
            [profiles.adam]
            telegram_id = 11
            operator = true
            import_source = "local"
            [profiles.anna]
            telegram_id = 22
            import_source = "local"
            """,
        )

        with pytest.raises(ProfileConfigError, match="operator-only"):
            load_profiles(roster)


class TestResolveProfile:
    def test_routes_private_message_by_numeric_sender(self, tmp_path: Path) -> None:
        profiles = {
            "adam": Profile("adam", 11, tmp_path / "adam", operator=True),
            "anna": Profile("anna", 22, tmp_path / "anna"),
        }
        update = {
            "message": {
                "from": {"id": 22, "username": "not_authorization"},
                "chat": {"id": 22, "type": "private"},
                "text": "hello",
            }
        }

        assert resolve_profile(update, profiles) is profiles["anna"]

    @pytest.mark.parametrize(
        "update",
        [
            {
                "message": {
                    "from": {"id": 22},
                    "chat": {"id": -100, "type": "group"},
                }
            },
            {
                "message": {
                    "from": {"id": 22},
                    "chat": {"id": 11, "type": "private"},
                }
            },
            {
                "message": {
                    "from": {"id": 99, "username": "anna"},
                    "chat": {"id": 99, "type": "private"},
                }
            },
        ],
    )
    def test_default_denies_untrusted_identity_shapes(
        self, tmp_path: Path, update: dict
    ) -> None:
        profiles = {"anna": Profile("anna", 22, tmp_path / "anna")}
        assert resolve_profile(update, profiles) is None

    def test_disabled_profile_is_not_routable(self, tmp_path: Path) -> None:
        profiles = {"anna": Profile("anna", 22, tmp_path / "anna", enabled=False)}
        update = {
            "callback_query": {
                "from": {"id": 22},
                "message": {"chat": {"id": 22, "type": "private"}},
            }
        }
        assert resolve_profile(update, profiles) is None


class TestProfileOperations:
    def test_add_creates_isolated_tree_and_database(self, tmp_path: Path) -> None:
        roster = tmp_path / "profiles.toml"

        profile = add_profile(
            "adam",
            11,
            operator=True,
            import_source="local",
            path=roster,
        )

        assert profile.db.exists()
        assert (profile.context / "me.md").exists()
        assert profile.drive_cache.is_dir()
        assert load_profiles(roster)["adam"].db == profile.db

    def test_cli_defaults_to_operator_and_refuses_missing_db(
        self, tmp_path: Path
    ) -> None:
        roster = tmp_path / "profiles.toml"
        _write_roster(
            roster,
            """
            [profiles.adam]
            telegram_id = 11
            operator = true
            import_source = "local"
            """,
        )

        with pytest.raises(ProfileConfigError, match="does not exist"):
            resolve_cli_profile(None, db=None, roster_path=roster)

        db = tmp_path / "profiles" / "adam" / "health.db"
        conn = open_db(db)
        conn.close()
        profile, resolved = resolve_cli_profile(None, db=None, roster_path=roster)
        assert profile is not None
        assert profile.name == "adam"
        assert resolved == db

    def test_adoption_dry_run_and_copy_preserve_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        legacy_db = tmp_path / "health.db"
        conn = open_db(legacy_db)
        conn.execute(
            "INSERT INTO daily (date, imported_at) "
            "VALUES ('2026-01-01', '2026-01-02T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        context = tmp_path / "ContextFiles"
        context.mkdir()
        (context / "me.md").write_text("# Me\n", encoding="utf-8")
        (context / "strategy.md").write_text("# Strategy\n", encoding="utf-8")
        roster = tmp_path / "profiles.toml"
        monkeypatch.setenv("ZDROWSKIT_IMPORT_SOURCE", "local")

        mappings = adopt_existing(
            "adam",
            telegram_id=11,
            dry_run=True,
            path=roster,
        )
        assert mappings
        assert not roster.exists()

        adopt_existing("adam", telegram_id=11, path=roster)

        adopted = load_profiles(roster)["adam"]
        adopted_conn = open_db(adopted.db)
        assert adopted_conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 1
        adopted_conn.close()
        assert legacy_db.exists()
        assert (context / "me.md").exists()

    def test_adoption_maps_configured_drive_cache_to_profile_convention(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = open_db(tmp_path / "health.db")
        conn.close()
        context = tmp_path / "ContextFiles"
        context.mkdir()
        (context / "me.md").write_text("# Me\n", encoding="utf-8")
        (context / "strategy.md").write_text("# Strategy\n", encoding="utf-8")
        legacy_cache = tmp_path / "Imports" / "adam"
        legacy_cache.mkdir(parents=True)
        monkeypatch.setenv("ZDROWSKIT_IMPORT_SOURCE", "google-drive")
        monkeypatch.setenv("HEALTH_DATA_DIR", str(legacy_cache))
        monkeypatch.setenv(
            "ZDROWSKIT_GOOGLE_DRIVE_METRICS_FOLDER_ID", "metricsFolder123"
        )
        monkeypatch.setenv(
            "ZDROWSKIT_GOOGLE_DRIVE_WORKOUTS_FOLDER_ID", "workoutsFolder123"
        )

        mappings = adopt_existing(
            "adam",
            telegram_id=11,
            dry_run=True,
            path=tmp_path / "profiles.toml",
        )

        assert (
            legacy_cache,
            tmp_path / "profiles" / "adam" / "Imports" / "google-drive",
        ) in mappings

    def test_adoption_includes_committed_wal_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        legacy_db = tmp_path / "health.db"
        legacy_conn = open_db(legacy_db)
        legacy_conn.execute("PRAGMA journal_mode=WAL")
        legacy_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        legacy_conn.execute("PRAGMA wal_autocheckpoint=0")
        legacy_conn.execute(
            "INSERT INTO daily (date, imported_at) "
            "VALUES ('2026-01-01', '2026-01-02T00:00:00Z')"
        )
        legacy_conn.commit()
        assert legacy_db.with_name("health.db-wal").stat().st_size > 0

        context = tmp_path / "ContextFiles"
        context.mkdir()
        (context / "me.md").write_text("# Me\n", encoding="utf-8")
        (context / "strategy.md").write_text("# Strategy\n", encoding="utf-8")
        roster = tmp_path / "profiles.toml"
        monkeypatch.setenv("ZDROWSKIT_IMPORT_SOURCE", "local")

        adopt_existing("adam", telegram_id=11, path=roster)

        adopted = load_profiles(roster)["adam"]
        adopted_conn = open_db(adopted.db)
        assert (
            adopted_conn.execute(
                "SELECT COUNT(*) FROM daily WHERE date = '2026-01-01'"
            ).fetchone()[0]
            == 1
        )
        adopted_conn.close()
        legacy_conn.close()


class TestMultiProfileDaemon:
    def test_routes_interleaved_updates_to_isolated_runtimes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roster = tmp_path / "profiles.toml"
        add_profile(
            "adam",
            11,
            operator=True,
            import_source="local",
            path=roster,
        )
        add_profile(
            "anna",
            22,
            path=roster,
            metrics_id="annaMetrics123",
            workouts_id="annaWorkouts123",
        )
        profiles = load_profiles(roster)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")

        from daemon import Daemon

        daemon = Daemon(
            profiles,
            google_drive_service_account=tmp_path / "service-account.json",
        )
        adam_handler = MagicMock()
        anna_handler = MagicMock()
        daemon.runtimes["adam"]._chat._handle_telegram_message_monitored = adam_handler
        daemon.runtimes["anna"]._chat._handle_telegram_message_monitored = anna_handler

        daemon.handle_update(
            {
                "message": {
                    "message_id": 1,
                    "from": {"id": 22},
                    "chat": {"id": 22, "type": "private"},
                    "text": "Anna",
                }
            }
        )
        daemon.handle_update(
            {
                "message": {
                    "message_id": 2,
                    "from": {"id": 11},
                    "chat": {"id": 11, "type": "private"},
                    "text": "Adam",
                }
            }
        )

        for worker in daemon._workers.values():
            worker.shutdown(wait=True)

        anna_handler.assert_called_once()
        adam_handler.assert_called_once()
        assert daemon.runtimes["adam"].db != daemon.runtimes["anna"].db
        assert (
            daemon.runtimes["adam"].context_dir != daemon.runtimes["anna"].context_dir
        )

    def test_slow_profile_does_not_block_another_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One person's slow LLM turn must not delay anyone else's message.

        Regression: chat turns ran inline on the shared Telegram handler pool
        while holding a per-profile lock, so a backlog for one profile starved
        every other profile of workers.
        """
        roster = tmp_path / "profiles.toml"
        add_profile("adam", 11, operator=True, import_source="local", path=roster)
        add_profile(
            "anna",
            22,
            path=roster,
            metrics_id="annaMetrics123",
            workouts_id="annaWorkouts123",
        )
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")

        from daemon import Daemon

        daemon = Daemon(
            load_profiles(roster),
            google_drive_service_account=tmp_path / "service-account.json",
        )
        release_adam = threading.Event()
        anna_answered = threading.Event()

        daemon.runtimes["adam"]._chat._handle_telegram_message_monitored = (
            lambda message: release_adam.wait(5)
        )
        daemon.runtimes["anna"]._chat._handle_telegram_message_monitored = (
            lambda message: anna_answered.set()
        )

        def send(user_id: int, message_id: int) -> None:
            daemon.handle_update(
                {
                    "message": {
                        "message_id": message_id,
                        "from": {"id": user_id},
                        "chat": {"id": user_id, "type": "private"},
                        "text": "hello",
                    }
                }
            )

        started = time.monotonic()
        send(11, 1)
        send(22, 2)
        dispatch_seconds = time.monotonic() - started

        # Routing must hand off to the profile's worker, never run the turn on
        # the caller's thread — that is what used to serialize the roster.
        assert dispatch_seconds < 1
        # Anna is answered while Adam's turn is still blocked.
        assert anna_answered.wait(5)
        assert not release_adam.is_set()
        release_adam.set()
        daemon.shutdown_workers()

    def test_chat_backlog_is_shed_per_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        roster = tmp_path / "profiles.toml"
        add_profile("adam", 11, operator=True, import_source="local", path=roster)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")

        from daemon import Daemon, _MAX_PENDING_CHAT_TURNS

        daemon = Daemon(
            load_profiles(roster),
            google_drive_service_account=tmp_path / "service-account.json",
        )
        runtime = daemon.runtimes["adam"]
        release = threading.Event()
        handled = []
        runtime._chat._handle_telegram_message_monitored = lambda message: (
            handled.append(message) or release.wait(5)
        )
        sender = MagicMock()
        runtime._chat._poller = sender

        for message_id in range(_MAX_PENDING_CHAT_TURNS + 2):
            daemon.handle_update(
                {
                    "message": {
                        "message_id": message_id,
                        "from": {"id": 11},
                        "chat": {"id": 11, "type": "private"},
                        "text": "spam",
                    }
                }
            )

        # Everything past the cap is shed with a reply, not silently dropped.
        assert sender.send_reply.call_count == 2
        assert "Still working" in sender.send_reply.call_args[0][0]
        release.set()
        daemon.shutdown_workers()

    def test_non_operator_cannot_start_coding_agent(self, tmp_path: Path) -> None:
        from daemon import ProfileRuntime

        profile = Profile("anna", 22, tmp_path / "anna", operator=False)
        sender = MagicMock()
        runtime = ProfileRuntime(
            "test-model",
            profile.db,
            profile.context,
            import_source="local",
            profile=profile,
            sender=sender,
        )

        with patch.object(runtime._chat, "_handle_agent_command") as handle_agent:
            runtime._chat._handle_command("/codex edit the repo", 7)

        handle_agent.assert_not_called()
        sender.send_reply.assert_called_once_with(
            "Operator access required.", reply_to_message_id=7
        )
