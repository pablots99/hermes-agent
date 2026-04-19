import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_cli.ops_dashboard import (
    list_project_records,
    list_run_records,
    normalize_linear_project,
    normalize_session_file,
)


NOW = datetime(2026, 4, 14, 10, 0, tzinfo=timezone.utc)


def _write_session(tmp_path: Path, filename: str, payload: dict) -> Path:
    path = tmp_path / filename
    path.write_text(json.dumps(payload))
    return path


def _payload(
    *,
    session_id: str,
    platform: str,
    started_at: str,
    updated_at: str,
    messages: list[dict],
    model: str = "gpt-5.4",
) -> dict:
    return {
        "session_id": session_id,
        "platform": platform,
        "model": model,
        "session_start": started_at,
        "last_updated": updated_at,
        "message_count": len(messages),
        "messages": messages,
    }


def test_normalize_regular_session_completed(tmp_path):
    payload = _payload(
        session_id="20260414_093000_abcd12",
        platform="discord",
        started_at="2026-04-14T09:30:00+00:00",
        updated_at="2026-04-14T09:31:15+00:00",
        messages=[
            {"role": "user", "content": "Please check PAB-126 and /lab/obsidian_vault/Projects/Open_Source_Lab/Jax_Ops_Dashboard_MVP.md"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "read_file"}},
                    {"function": {"name": "search_files"}},
                ],
                "content": "",
            },
            {"role": "tool", "content": '{"status":"success"}'},
            {
                "role": "assistant",
                "content": "Completed the first pass for PAB-126 and captured the next implementation steps.",
                "finish_reason": "stop",
            },
        ],
    )
    path = _write_session(tmp_path, "session_20260414_093000_abcd12.json", payload)

    bundle = normalize_session_file(path, now=NOW)

    assert bundle.session.session_id == "20260414_093000_abcd12"
    assert bundle.session.platform == "discord"
    assert bundle.session.tool_call_count == 2
    assert bundle.session.issue_identifiers == ["PAB-126"]
    assert bundle.session.project_hints == ["Open_Source_Lab"]
    assert bundle.run.run_type == "interactive"
    assert bundle.run.status == "completed"
    assert bundle.run.duration_seconds == 75
    assert bundle.run.latest_tool_name == "search_files"
    assert bundle.run.failure_reason is None
    assert "Completed the first pass" in bundle.run.summary


def test_normalize_cron_session_extracts_job_and_summary(tmp_path):
    payload = _payload(
        session_id="cron_66d2e64a7518_20260414_082242",
        platform="cron",
        started_at="2026-04-14T08:22:42.972524",
        updated_at="2026-04-14T08:35:13.371554",
        messages=[
            {"role": "user", "content": "Work Linear Todo issue PAB-126 using the Jax ops dashboard plan."},
            {"role": "assistant", "content": "Executed the dashboard ingestion pass.", "finish_reason": "stop"},
        ],
    )
    path = _write_session(tmp_path, "session_cron_66d2e64a7518_20260414_082242.json", payload)

    bundle = normalize_session_file(path, now=NOW)

    assert bundle.run.run_type == "cron"
    assert bundle.run.cron_job_id == "66d2e64a7518"
    assert bundle.run.status == "completed"
    assert bundle.run.issue_identifiers == ["PAB-126"]
    assert bundle.run.summary == "Executed the dashboard ingestion pass."


def test_recent_incomplete_run_is_running(tmp_path):
    recent = NOW - timedelta(seconds=45)
    payload = _payload(
        session_id="20260414_095900_live01",
        platform="cli",
        started_at="2026-04-14T09:59:00+00:00",
        updated_at=recent.isoformat(),
        messages=[
            {"role": "user", "content": "Keep working on the dashboard."},
            {"role": "assistant", "tool_calls": [{"function": {"name": "terminal"}}], "content": ""},
            {"role": "tool", "content": '{"status":"success","output":"still working"}'},
        ],
    )
    path = _write_session(tmp_path, "session_20260414_095900_live01.json", payload)

    bundle = normalize_session_file(path, now=NOW)

    assert bundle.run.status == "running"
    assert bundle.run.failure_reason is None
    assert bundle.run.summary == '{"status":"success","output":"still working"}'


def test_stale_incomplete_run_becomes_failed_with_reason(tmp_path):
    payload = _payload(
        session_id="20260414_080000_fail01",
        platform="cli",
        started_at="2026-04-14T08:00:00+00:00",
        updated_at="2026-04-14T08:02:00+00:00",
        messages=[
            {"role": "user", "content": "Investigate the failure."},
            {"role": "assistant", "tool_calls": [{"function": {"name": "execute_code"}}], "content": ""},
            {"role": "tool", "content": 'HTTP 500: {"status":"error","message":"Traceback: boom"}'},
        ],
    )
    path = _write_session(tmp_path, "session_20260414_080000_fail01.json", payload)

    bundle = normalize_session_file(path, now=NOW)

    assert bundle.run.status == "failed"
    assert bundle.run.failure_reason == 'HTTP 500: {"status":"error","message":"Traceback: boom"}'
    assert bundle.run.summary == 'HTTP 500: {"status":"error","message":"Traceback: boom"}'


def test_assistant_incomplete_finish_reason_is_not_marked_completed(tmp_path):
    recent = NOW - timedelta(seconds=30)
    payload = _payload(
        session_id="20260414_095930_partial01",
        platform="cli",
        started_at="2026-04-14T09:59:00+00:00",
        updated_at=recent.isoformat(),
        messages=[
            {"role": "user", "content": "Continue working on the dashboard."},
            {
                "role": "assistant",
                "content": "Let me think through the normalization strategy first.",
                "finish_reason": "incomplete",
            },
        ],
    )
    path = _write_session(tmp_path, "session_20260414_095930_partial01.json", payload)

    running_bundle = normalize_session_file(path, now=NOW)
    assert running_bundle.run.status == "running"

    failed_bundle = normalize_session_file(path, now=NOW + timedelta(minutes=10))
    assert failed_bundle.run.status == "failed"


def test_list_run_records_orders_newest_first_and_skips_bad_json(tmp_path):
    newer_payload = _payload(
        session_id="20260414_094500_newer",
        platform="discord",
        started_at="2026-04-14T09:45:00+00:00",
        updated_at="2026-04-14T09:50:00+00:00",
        messages=[
            {"role": "user", "content": "PAB-126"},
            {"role": "assistant", "content": "done", "finish_reason": "stop"},
        ],
    )
    older_payload = _payload(
        session_id="20260414_083000_older",
        platform="cli",
        started_at="2026-04-14T08:30:00+00:00",
        updated_at="2026-04-14T08:40:00+00:00",
        messages=[
            {"role": "user", "content": "older run"},
            {"role": "assistant", "content": "done", "finish_reason": "stop"},
        ],
    )
    _write_session(tmp_path, "session_20260414_094500_newer.json", newer_payload)
    _write_session(tmp_path, "session_20260414_083000_older.json", older_payload)
    (tmp_path / "session_bad.json").write_text("{not json")

    runs = list_run_records(tmp_path, now=NOW)

    assert [run["run_id"] for run in runs] == [
        "20260414_094500_newer",
        "20260414_083000_older",
    ]


def test_normalize_linear_project_parses_full_description_and_links_context():
    project = {
        "id": "proj-123",
        "name": "Open Source Lab",
        "description": """## Context
- **Obsidian:** /lab/obsidian_vault/Projects/Open_Source_Lab/
- **Repo:** https://github.com/pablots99/hermes-agent
- **Discord:** #open-source-lab (ID: 1493001000000000000)
- **Overview:** 01_Overview/README.md
- **Current state:** docs/current-state.md
- **Stack:** 03_Architecture/Stack.md

## Goal
Ship a live read-only dashboard for Jax ops visibility.
""",
        "updatedAt": "2026-04-14T09:58:00+00:00",
        "url": "https://linear.app/pablot/project/open-source-lab",
    }
    linked_issues = [
        {
            "identifier": "PAB-125",
            "project": {"id": "proj-123", "name": "Open Source Lab"},
            "state": {"type": "started"},
            "updatedAt": "2026-04-14T09:57:00+00:00",
        }
    ]
    observed_runs = [
        {
            "run_id": "run-1",
            "status": "running",
            "updated_at": "2026-04-14T09:59:00+00:00",
            "project_hints": ["Open_Source_Lab"],
            "issue_identifiers": ["PAB-125"],
        }
    ]

    record = normalize_linear_project(project, linked_issues=linked_issues, observed_runs=observed_runs)

    assert record.project_key == "Open_Source_Lab"
    assert record.obsidian_path == "/lab/obsidian_vault/Projects/Open_Source_Lab/"
    assert record.repo_url == "https://github.com/pablots99/hermes-agent"
    assert record.discord_channel_name == "open-source-lab"
    assert record.discord_channel_id == "1493001000000000000"
    assert record.overview_path == "01_Overview/README.md"
    assert record.current_state_path == "docs/current-state.md"
    assert record.stack_path == "03_Architecture/Stack.md"
    assert record.goal == "Ship a live read-only dashboard for Jax ops visibility."
    assert record.status == "active"
    assert record.metadata_status == "complete"
    assert record.issue_identifiers == ["PAB-125"]
    assert record.last_activity_at == "2026-04-14T09:59:00+00:00"


def test_normalize_linear_project_handles_partial_metadata_and_local_repo_path():
    project = {
        "id": "proj-456",
        "name": "Private Inference",
        "description": """## Context
- **Repo:** /srv/projects/private-inference
- **Discord:** #private-inference

## Goal
Replace hosted inference with a self-managed stack.
""",
    }

    record = normalize_linear_project(project)

    assert record.project_key == "Private_Inference"
    assert record.obsidian_path == "/lab/obsidian_vault/Projects/Private_Inference/"
    assert record.repo_url is None
    assert record.repo_path == "/srv/projects/private-inference"
    assert record.discord_channel_name == "private-inference"
    assert record.discord_channel_id is None
    assert record.metadata_status == "complete"
    assert record.status == "unknown"


def test_normalize_linear_project_falls_back_to_issue_and_run_hints_when_description_is_sparse():
    project = {
        "id": "proj-789",
        "name": "Open Source Lab",
        "description": "## Context\n- **Repo:** https://github.com/pablots99/hermes-agent\n",
    }
    linked_issues = [
        {
            "identifier": "PAB-127",
            "project": {"name": "Open Source Lab"},
            "project_hints": ["Open_Source_Lab"],
            "state": {"type": "unstarted"},
        }
    ]
    observed_runs = [
        {
            "run_id": "run-2",
            "status": "completed",
            "project_hints": ["Open_Source_Lab"],
            "issue_identifiers": ["PAB-126"],
            "updated_at": "2026-04-14T09:40:00+00:00",
        }
    ]

    record = normalize_linear_project(project, linked_issues=linked_issues, observed_runs=observed_runs)

    assert record.project_key == "Open_Source_Lab"
    assert record.obsidian_path == "/lab/obsidian_vault/Projects/Open_Source_Lab/"
    assert record.issue_identifiers == ["PAB-127", "PAB-126"]
    assert record.project_hints == ["Open_Source_Lab"]
    assert record.status == "planned"
    assert record.metadata_status == "partial"


def test_list_project_records_orders_latest_activity_first():
    projects = [
        {
            "id": "proj-old",
            "name": "Older Project",
            "description": "- **Obsidian:** /lab/obsidian_vault/Projects/Older_Project/",
            "updatedAt": "2026-04-14T08:00:00+00:00",
        },
        {
            "id": "proj-new",
            "name": "Newer Project",
            "description": "- **Obsidian:** /lab/obsidian_vault/Projects/Newer_Project/",
            "updatedAt": "2026-04-14T09:00:00+00:00",
        },
    ]

    records = list_project_records(projects)

    assert [record["project_key"] for record in records] == ["Newer_Project", "Older_Project"]
