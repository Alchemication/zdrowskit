"""Tests for the plan-frame decision and its caching."""

from __future__ import annotations

import sqlite3
import types
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

import plan_frame as pf
from plan_frame import (
    MODE_FACTS,
    MODE_FULL,
    MODE_HIDDEN,
    PlanFrame,
    build_plan_frame_messages,
    context_digest,
    load_plan_frame,
    parse_plan_frame_response,
    resolve_plan_frame,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _answer(mode: str, reason: str = "a stated reason") -> str:
    return f'{{"mode": "{mode}", "reason": "{reason}"}}'


class TestPlanFrameModes:
    def test_full_shows_everything(self) -> None:
        frame = PlanFrame(mode=MODE_FULL)
        assert frame.shows_strip is True
        assert frame.shows_verdict is True

    def test_facts_keeps_numbers_and_drops_the_judgement(self) -> None:
        frame = PlanFrame(mode=MODE_FACTS)
        assert frame.shows_strip is True
        assert frame.shows_verdict is False

    def test_hidden_shows_nothing(self) -> None:
        frame = PlanFrame(mode=MODE_HIDDEN)
        assert frame.shows_strip is False
        assert frame.shows_verdict is False

    def test_the_default_is_the_full_strip(self) -> None:
        """Every failure path returns this, so it must be the widest answer."""
        assert PlanFrame().mode == MODE_FULL


class TestParsePlanFrameResponse:
    def test_parses_each_mode(self) -> None:
        for mode in (MODE_FULL, MODE_FACTS, MODE_HIDDEN):
            parsed = parse_plan_frame_response(_answer(mode))
            assert parsed is not None
            assert parsed.mode == mode

    def test_parses_a_fenced_payload(self) -> None:
        parsed = parse_plan_frame_response('```json\n{"mode": "full"}\n```')
        assert parsed is not None and parsed.mode == MODE_FULL

    def test_full_needs_no_reason(self) -> None:
        assert parse_plan_frame_response('{"mode": "full"}') is not None

    def test_suppression_without_a_reason_is_refused(self) -> None:
        """A decision nobody can review later is one nobody can correct."""
        assert parse_plan_frame_response('{"mode": "hidden"}') is None
        assert parse_plan_frame_response('{"mode": "facts", "reason": ""}') is None

    def test_unknown_mode_is_refused(self) -> None:
        assert parse_plan_frame_response(_answer("quiet")) is None

    def test_unparseable_output_is_refused(self) -> None:
        assert parse_plan_frame_response("I think you should rest.") is None

    def test_a_json_array_is_refused(self) -> None:
        assert parse_plan_frame_response('[{"mode": "hidden"}]') is None


class TestPromptWithholdsTheNumbers:
    def test_the_prompt_carries_the_life_context(self) -> None:
        content = build_plan_frame_messages(
            me="Adam, 38.",
            log="- 2026-09-04: baby arrived, no sleep",
            history="- 2026-W35: consistent week",
            today="2026-09-05",
        )[0]["content"]
        assert "baby arrived" in content
        assert "Adam, 38." in content

    def test_the_call_cannot_be_told_how_the_week_is_going(self) -> None:
        """The one safeguard that makes this mechanism safe to have at all.

        A model shown the measurements would answer a different question —
        whether the numbers are flattering — and would learn to hide the strip
        exactly when it has something to say. The guarantee is structural
        rather than a matter of wording: there is no parameter to pass them
        through, so this fails the moment someone adds one.
        """
        import inspect

        accepted = set(inspect.signature(build_plan_frame_messages).parameters)
        assert accepted == {"me", "log", "history", "today", "prompts_dir"}

    def test_no_health_data_reaches_the_prompt(self) -> None:
        """Nothing in the rendered prompt comes from the database."""
        content = build_plan_frame_messages(
            me="Adam, 38.",
            log="- 2026-09-04: baby arrived",
            history=None,
            today="2026-09-05",
        )[0]["content"]
        # Every digit present traces to the context passed in or to the date.
        supplied = "Adam, 38.- 2026-09-04: baby arrived2026-09-05"
        template = Path("src/prompts/plan_frame_prompt.md").read_text()
        for line in content.splitlines():
            for token in line.split():
                if any(ch.isdigit() for ch in token):
                    assert token in supplied or token in template, token

    def test_missing_context_renders(self) -> None:
        content = build_plan_frame_messages(
            me=None, log=None, history=None, today="2026-09-05"
        )[0]["content"]
        assert "(not provided)" in content


class TestContextDigest:
    def test_same_context_same_key(self) -> None:
        assert context_digest("a", "b") == context_digest("a", "b")

    def test_changed_context_changes_the_key(self) -> None:
        assert context_digest("a", "b") != context_digest("a", "c")

    def test_none_and_empty_agree(self) -> None:
        assert context_digest(None, "b") == context_digest("", "b")

    def test_fields_do_not_bleed_into_each_other(self) -> None:
        """Concatenation alone would make ("ab", "") and ("a", "b") identical."""
        assert context_digest("ab", "") != context_digest("a", "b")


class TestResolvePlanFrame:
    @pytest.fixture(autouse=True)
    def _stub_llm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.reply = _answer(MODE_FULL)

        def fake_call_llm(messages, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(messages)
            return types.SimpleNamespace(text=self.reply, llm_call_id=42, model="stub")

        import llm

        monkeypatch.setattr(llm, "call_llm", fake_call_llm)

    def _resolve(
        self,
        conn: sqlite3.Connection,
        *,
        log: str | None = "- ordinary week",
        now: datetime = NOW,
    ) -> PlanFrame:
        return resolve_plan_frame(
            conn, me="Adam", log=log, history=None, today="2026-09-05", now=now
        )

    def test_decides_once_then_reads_the_cache(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        assert self._resolve(in_memory_db).mode == MODE_FULL
        second = self._resolve(in_memory_db)
        assert len(self.calls) == 1
        assert second.source == "cache"

    def test_a_journal_entry_reopens_the_question(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        self._resolve(in_memory_db)
        self._resolve(in_memory_db, log="- 2026-09-05: baby arrived")
        assert len(self.calls) == 2

    def test_a_normal_answer_holds_for_a_week(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        self._resolve(in_memory_db)
        self._resolve(in_memory_db, now=NOW + timedelta(days=6))
        assert len(self.calls) == 1

    def test_a_normal_answer_expires_after_a_week(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        self._resolve(in_memory_db)
        self._resolve(in_memory_db, now=NOW + timedelta(days=8))
        assert len(self.calls) == 2

    def test_a_suppression_has_to_keep_justifying_itself(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        """A stale hide is invisible, so it expires far sooner than a show."""
        self.reply = _answer(MODE_HIDDEN, "newborn at home")
        assert self._resolve(in_memory_db).mode == MODE_HIDDEN
        self._resolve(in_memory_db, now=NOW + timedelta(days=3))
        assert len(self.calls) == 2

    def test_the_decision_is_stored_with_its_reason(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        self.reply = _answer(MODE_FACTS, "travelling for work all week")
        self._resolve(in_memory_db)
        stored = load_plan_frame(in_memory_db)
        assert stored is not None
        assert stored.mode == MODE_FACTS
        assert stored.reason == "travelling for work all week"
        assert stored.llm_call_id == 42

    def test_a_failed_call_shows_the_full_strip(
        self, in_memory_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import llm

        monkeypatch.setattr(
            llm,
            "call_llm",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down")),
        )
        assert self._resolve(in_memory_db).mode == MODE_FULL

    def test_a_failed_call_keeps_an_existing_suppression(
        self, in_memory_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An outage must not start announcing progress during a bereavement."""
        self.reply = _answer(MODE_HIDDEN, "bereavement")
        self._resolve(in_memory_db)

        import llm

        monkeypatch.setattr(
            llm,
            "call_llm",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider down")),
        )
        later = self._resolve(in_memory_db, now=NOW + timedelta(days=5))
        assert later.mode == MODE_HIDDEN

    def test_an_unusable_answer_does_not_suppress(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        self.reply = "sorry, I cannot decide that"
        assert self._resolve(in_memory_db).mode == MODE_FULL
        assert load_plan_frame(in_memory_db) is None

    def test_an_empty_cache_reads_as_nothing(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        assert load_plan_frame(in_memory_db) is None

    def test_a_corrupt_stored_mode_is_ignored(
        self, in_memory_db: sqlite3.Connection
    ) -> None:
        in_memory_db.execute(
            "INSERT INTO plan_frame (id, mode, reason, context_hash, decided_at) "
            "VALUES (1, 'quiet', 'x', 'h', ?)",
            (NOW.isoformat(),),
        )
        in_memory_db.commit()
        assert load_plan_frame(in_memory_db) is None

    def test_the_cache_holds_one_row(self, in_memory_db: sqlite3.Connection) -> None:
        self._resolve(in_memory_db)
        self._resolve(in_memory_db, log="- something new")
        count = in_memory_db.execute("SELECT COUNT(*) FROM plan_frame").fetchone()[0]
        assert count == 1


class TestStalenessRules:
    def test_a_decision_without_a_timestamp_is_stale(self) -> None:
        assert pf._is_stale(PlanFrame(mode=MODE_FULL), NOW) is True

    def test_an_unparseable_timestamp_is_stale(self) -> None:
        assert pf._is_stale(PlanFrame(decided_at="not a date"), NOW) is True

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        frame = PlanFrame(decided_at=NOW.replace(tzinfo=None).isoformat())
        assert pf._is_stale(frame, NOW + timedelta(days=1)) is False
