"""Tests for agent.codex_usage — rolling Codex subscription usage windows."""

import json
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "test_codex_usage.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    sessions_dir = home / "sessions" / "2026" / "04" / "14"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture()
def populated_codex_db(db):
    now = time.time()
    hour = 3600
    day = 86400

    # Recent Codex session via billing_provider
    db.create_session(session_id="c1", source="cli", model="gpt-5.4", user_id="u1")
    db.update_token_counts("c1", input_tokens=1_000, output_tokens=400, model="gpt-5.4")
    db.append_message("c1", role="assistant", content="first")
    db.append_message("c1", role="assistant", content="second")
    db.append_message("c1", role="assistant", content="third")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, billing_provider = ?, billing_mode = ? WHERE id = 'c1'",
        (now - 2 * hour, now - int(1.5 * hour), "openai-codex", "subscription_included"),
    )
    db._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE session_id = 'c1'",
        (now - int(1.8 * hour),),
    )

    # Older Codex session via model fallback (no billing_provider persisted)
    db.create_session(session_id="c2", source="discord", model="gpt-5.2-codex", user_id="u2")
    db.update_token_counts("c2", input_tokens=2_000, output_tokens=800, model="gpt-5.2-codex")
    db.append_message("c2", role="assistant", content="older-1")
    db.append_message("c2", role="assistant", content="older-2")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = 'c2'",
        (now - 2 * day, now - 2 * day + hour),
    )
    db._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE session_id = 'c2'",
        (now - 2 * day + 120,),
    )

    # Monthly-only Codex session
    db.create_session(session_id="c3", source="cli", model="gpt-5.4", user_id="u1")
    db.update_token_counts("c3", input_tokens=3_000, output_tokens=900, model="gpt-5.4")
    db.append_message("c3", role="assistant", content="monthly-1")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, billing_provider = ?, billing_mode = ? WHERE id = 'c3'",
        (now - 15 * day, now - 15 * day + 1800, "openai-codex", "subscription_included"),
    )
    db._conn.execute(
        "UPDATE messages SET timestamp = ? WHERE session_id = 'c3'",
        (now - 15 * day + 60,),
    )

    # Non-Codex session should be ignored
    db.create_session(session_id="other", source="cli", model="claude-sonnet", user_id="u1")
    db.update_token_counts("other", input_tokens=50_000, output_tokens=10_000, model="claude-sonnet")
    db.append_message("other", role="assistant", content="ignore me")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, billing_provider = ? WHERE id = 'other'",
        (now - hour, now - hour + 300, "anthropic"),
    )

    db._conn.commit()
    return db


class TestCodexUsageReport:
    def test_generate_empty_report(self, db):
        from agent.codex_usage import CodexUsageTracker

        report = CodexUsageTracker(db).generate()

        assert report["empty"] is True
        assert report["windows"] == []

    def test_generate_tracks_5h_7d_30d_windows(self, populated_codex_db):
        from agent.codex_usage import CodexUsageTracker

        report = CodexUsageTracker(populated_codex_db).generate()

        assert report["empty"] is False
        labels = [w["key"] for w in report["windows"]]
        assert labels == ["5h", "7d", "30d"]

        five_hour = report["windows"][0]
        assert five_hour["session_count"] == 1
        assert five_hour["api_calls"] == 3
        assert five_hour["input_tokens"] == 1_000
        assert five_hour["output_tokens"] == 400
        assert five_hour["total_tokens"] == 1_400

        weekly = report["windows"][1]
        assert weekly["session_count"] == 2
        assert weekly["api_calls"] == 5
        assert weekly["total_tokens"] == 4_200

        monthly = report["windows"][2]
        assert monthly["session_count"] == 3
        assert monthly["api_calls"] == 6
        assert monthly["total_tokens"] == 8_100

    def test_source_filter(self, populated_codex_db):
        from agent.codex_usage import CodexUsageTracker

        report = CodexUsageTracker(populated_codex_db).generate(source="cli")

        weekly = report["windows"][1]
        monthly = report["windows"][2]
        assert weekly["session_count"] == 1
        assert weekly["api_calls"] == 3
        assert monthly["session_count"] == 2
        assert monthly["api_calls"] == 4

    def test_model_breakdown_aggregates_codex_models(self, populated_codex_db):
        from agent.codex_usage import CodexUsageTracker

        report = CodexUsageTracker(populated_codex_db).generate()

        assert report["models"][0]["model"] == "gpt-5.4"
        assert report["models"][0]["session_count"] == 2
        assert report["models"][0]["total_tokens"] == 5_300
        assert any(m["model"] == "gpt-5.2-codex" for m in report["models"])


class TestCodexRateLimits:
    def test_generate_includes_live_rate_limits_from_codex_sessions(self, populated_codex_db, codex_home):
        from agent.codex_usage import CodexUsageTracker

        session_path = codex_home / "sessions" / "2026" / "04" / "14" / "rollout-2026-04-14T05-28-01-test.jsonl"
        session_path.write_text(
            "\n".join(
                [
                    json.dumps({
                        "timestamp": "2026-04-14T05:28:01.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "rate_limits": {
                                "limit_id": "codex",
                                "primary": {"used_percent": 5.0, "window_minutes": 300, "resets_at": 1776150428},
                                "secondary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1776411124},
                                "credits": None,
                                "plan_type": "plus",
                            },
                        },
                    }),
                    json.dumps({
                        "timestamp": "2026-04-14T05:28:04.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"total_token_usage": {"total_tokens": 1234}},
                            "rate_limits": {
                                "limit_id": "codex",
                                "primary": {"used_percent": 5.0, "window_minutes": 300, "resets_at": 1776150428},
                                "secondary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1776411124},
                                "credits": None,
                                "plan_type": "plus",
                            },
                        },
                    }),
                ]
            )
            + "\n"
        )

        report = CodexUsageTracker(populated_codex_db).generate()

        assert report["live_rate_limits"]["plan_type"] == "plus"
        assert report["live_rate_limits"]["source"] == str(session_path)
        assert report["live_rate_limits"]["primary"]["remaining_percent"] == 95.0
        assert report["live_rate_limits"]["secondary"]["remaining_percent"] == 90.0


class TestCodexUsageFormatting:
    def test_terminal_format_contains_sections(self, populated_codex_db, codex_home):
        from agent.codex_usage import CodexUsageTracker

        session_path = codex_home / "sessions" / "2026" / "04" / "14" / "rollout-2026-04-14T05-28-01-test.jsonl"
        session_path.write_text(
            json.dumps({
                "timestamp": "2026-04-14T05:28:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {"used_percent": 5.0, "window_minutes": 300, "resets_at": 1776150428},
                        "secondary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1776411124},
                        "credits": None,
                        "plan_type": "plus",
                    },
                },
            }) + "\n"
        )

        tracker = CodexUsageTracker(populated_codex_db)
        text = tracker.format_terminal(tracker.generate())

        assert "Codex Usage" in text
        assert "Live limits" in text
        assert "95.0% left" in text
        assert "Last 5h" in text
        assert "Last 7d" in text
        assert "Last 30d" in text
        assert "Top Codex models" in text

    def test_gateway_format_contains_window_summaries(self, populated_codex_db, codex_home):
        from agent.codex_usage import CodexUsageTracker

        session_path = codex_home / "sessions" / "2026" / "04" / "14" / "rollout-2026-04-14T05-28-01-test.jsonl"
        session_path.write_text(
            json.dumps({
                "timestamp": "2026-04-14T05:28:01.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {"used_percent": 5.0, "window_minutes": 300, "resets_at": 1776150428},
                        "secondary": {"used_percent": 10.0, "window_minutes": 10080, "resets_at": 1776411124},
                        "credits": None,
                        "plan_type": "plus",
                    },
                },
            }) + "\n"
        )

        tracker = CodexUsageTracker(populated_codex_db)
        text = tracker.format_gateway(tracker.generate())

        assert "**Codex Usage**" in text
        assert "**Live limits:**" in text
        assert "95.0% left" in text
        assert "Last 5h" in text
        assert "assistant turns" in text
        assert "Top models" in text
