"""Normalization helpers for the Jax ops dashboard read model.

This module builds lightweight session/run records directly from Hermes session
JSON files in ``HERMES_HOME/sessions``. It is intentionally read-only and keeps
just enough metadata for overview and active-work surfaces without loading full
transcripts into memory-heavy UI models.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable, Optional

from hermes_constants import get_hermes_home

_ACTIVE_WINDOW_SECONDS = 300
_DEFAULT_SUMMARY_LENGTH = 240
_MAX_SUMMARY_SOURCE_LENGTH = 4000
_CRON_FILE_RE = re.compile(r"^session_cron_([^_]+)_")
_CRON_SESSION_RE = re.compile(r"^cron_([^_]+)_")
_ISSUE_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_OBSIDIAN_PROJECT_RE = re.compile(r"/Projects/([^/\n]+)/")
_OBSIDIAN_LINE_RE = re.compile(r"(?:^|\n)-?\s*(?:\*\*)?Obsidian(?:\*\*)?:\s*([^\n]+)", re.IGNORECASE)
_REPO_LINE_RE = re.compile(r"(?:^|\n)-?\s*(?:\*\*)?Repo(?:\*\*)?:\s*([^\n]+)", re.IGNORECASE)
_DISCORD_LINE_RE = re.compile(
    r"(?:^|\n)-?\s*(?:\*\*)?Discord(?:\*\*)?:\s*#?([A-Za-z0-9._-]+)?(?:\s*\((?:ID:\s*)?([0-9]{6,})\))?",
    re.IGNORECASE,
)
_POINTER_LINE_TEMPLATE = r"(?:^|\n)-?\s*(?:\*\*)?{label}(?:\*\*)?:\s*([^\n]+)"
_GOAL_SECTION_RE = re.compile(r"##\s*Goal\s*(.+?)(?:\n##\s|\Z)", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_ERROR_SNIPPET_RE = re.compile(
    r'("status"\s*:\s*"error"|"success"\s*:\s*false|traceback|exception|http\s+[45]\d\d|"exit_code"\s*:\s*[1-9])',
    re.IGNORECASE,
)


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    source_path: str
    transcript_path: str
    platform: Optional[str]
    model: Optional[str]
    started_at: Optional[str]
    updated_at: Optional[str]
    message_count: int
    assistant_message_count: int
    tool_result_count: int
    tool_call_count: int
    last_role: Optional[str]
    summary: str
    issue_identifiers: list[str]
    project_hints: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunRecord:
    run_id: str
    session_id: str
    run_type: str
    platform: Optional[str]
    status: str
    cron_job_id: Optional[str]
    transcript_path: str
    started_at: Optional[str]
    updated_at: Optional[str]
    duration_seconds: Optional[int]
    message_count: int
    tool_call_count: int
    latest_tool_name: Optional[str]
    summary: str
    failure_reason: Optional[str]
    issue_identifiers: list[str]
    project_hints: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProjectRecord:
    project_key: str
    name: str
    linear_project_id: Optional[str]
    linear_project_url: Optional[str]
    obsidian_path: Optional[str]
    repo_url: Optional[str]
    repo_path: Optional[str]
    discord_channel_name: Optional[str]
    discord_channel_id: Optional[str]
    overview_path: Optional[str]
    current_state_path: Optional[str]
    stack_path: Optional[str]
    goal: Optional[str]
    status: str
    metadata_status: str
    last_activity_at: Optional[str]
    issue_count: int
    run_count: int
    issue_identifiers: list[str]
    project_hints: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SessionRunBundle:
    session: SessionRecord
    run: RunRecord


def get_sessions_dir() -> Path:
    return get_hermes_home() / "sessions"


def list_session_run_bundles(
    sessions_dir: Path | None = None,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[SessionRunBundle]:
    """Load and normalize session JSON files from disk.

    Results are ordered by ``updated_at`` descending, then filename descending.
    Invalid JSON files are skipped to keep dashboard ingestion resilient.
    """
    base = sessions_dir or get_sessions_dir()
    if not base.exists():
        return []

    bundles: list[SessionRunBundle] = []
    for path in sorted(base.glob("session*.json")):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        bundles.append(normalize_session_file(path, payload=payload, now=now))

    bundles.sort(
        key=lambda bundle: (
            bundle.run.updated_at or "",
            bundle.session.source_path,
        ),
        reverse=True,
    )
    if limit is not None:
        bundles = bundles[:limit]
    return bundles


def list_session_records(
    sessions_dir: Path | None = None,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    return [bundle.session.to_dict() for bundle in list_session_run_bundles(sessions_dir, now=now, limit=limit)]


def list_run_records(
    sessions_dir: Path | None = None,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    return [bundle.run.to_dict() for bundle in list_session_run_bundles(sessions_dir, now=now, limit=limit)]


def list_project_records(
    linear_projects: Iterable[dict[str, Any]],
    *,
    linked_issues: Iterable[dict[str, Any]] | None = None,
    observed_runs: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    projects = [
        normalize_linear_project(project, linked_issues=linked_issues, observed_runs=observed_runs).to_dict()
        for project in linear_projects
    ]
    projects.sort(key=lambda item: (item.get("last_activity_at") or "", item["project_key"]), reverse=True)
    return projects


def normalize_linear_project(
    project: dict[str, Any],
    *,
    linked_issues: Iterable[dict[str, Any]] | None = None,
    observed_runs: Iterable[dict[str, Any]] | None = None,
) -> ProjectRecord:
    description = _clean_string(project.get("description")) or ""
    pointers = _parse_project_description(description)
    project_id = _clean_string(project.get("id"))
    project_name = _clean_string(project.get("name")) or "Unknown project"

    related_issues = [item for item in (linked_issues or []) if _project_related(item, project, pointers)]
    related_runs = [item for item in (observed_runs or []) if _project_related(item, project, pointers)]

    project_hints = _collect_project_hints(project, pointers, related_issues, related_runs)
    project_key = _first_non_empty(
        pointers.get("project_key"),
        _first_project_hint(related_runs),
        _first_project_hint(related_issues),
        _project_key_from_name(project_name),
        "unknown_project",
    )

    obsidian_path = _normalize_obsidian_path(
        _first_non_empty(
            pointers.get("obsidian_path"),
            _obsidian_path_from_hint(_first_project_hint(related_runs)),
            _obsidian_path_from_hint(_first_project_hint(related_issues)),
            _obsidian_path_from_hint(project_key),
        )
    )

    repo_url, repo_path = _split_repo_pointer(pointers.get("repo_pointer"))
    issue_identifiers = _collect_issue_identifiers(related_issues, related_runs)
    last_activity_at = _latest_timestamp(
        _clean_string(project.get("updatedAt")),
        *[_extract_timestamp(item) for item in related_issues],
        *[_extract_timestamp(item) for item in related_runs],
    )

    metadata_status = _metadata_status(
        obsidian_path=obsidian_path,
        repo_url=repo_url,
        repo_path=repo_path,
        discord_channel_name=pointers.get("discord_channel_name"),
        discord_channel_id=pointers.get("discord_channel_id"),
    )

    return ProjectRecord(
        project_key=project_key,
        name=project_name,
        linear_project_id=project_id,
        linear_project_url=_clean_string(project.get("url")),
        obsidian_path=obsidian_path,
        repo_url=repo_url,
        repo_path=repo_path,
        discord_channel_name=pointers.get("discord_channel_name"),
        discord_channel_id=pointers.get("discord_channel_id"),
        overview_path=pointers.get("overview_path"),
        current_state_path=pointers.get("current_state_path"),
        stack_path=pointers.get("stack_path"),
        goal=pointers.get("goal"),
        status=_project_status(related_issues, related_runs),
        metadata_status=metadata_status,
        last_activity_at=last_activity_at,
        issue_count=len(related_issues),
        run_count=len(related_runs),
        issue_identifiers=issue_identifiers,
        project_hints=project_hints,
    )


def normalize_session_file(
    path: Path,
    *,
    payload: Optional[dict[str, Any]] = None,
    now: datetime | None = None,
) -> SessionRunBundle:
    data = payload if payload is not None else _load_json(path) or {}
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []

    session_id = str(data.get("session_id") or path.stem)
    started_dt = _parse_dt(data.get("session_start"))
    updated_dt = _parse_dt(data.get("last_updated"))
    last_message = messages[-1] if messages else {}

    tool_call_names = _tool_call_names(messages)
    summary = _derive_summary(messages)
    issue_identifiers = _extract_issue_identifiers(_summary_text_sources(messages, summary))
    project_hints = _extract_project_hints(_summary_text_sources(messages, summary))

    session_record = SessionRecord(
        session_id=session_id,
        source_path=str(path),
        transcript_path=str(path),
        platform=_clean_string(data.get("platform")),
        model=_clean_string(data.get("model")),
        started_at=_to_iso(started_dt),
        updated_at=_to_iso(updated_dt),
        message_count=int(data.get("message_count") or len(messages)),
        assistant_message_count=sum(1 for message in messages if message.get("role") == "assistant"),
        tool_result_count=sum(1 for message in messages if message.get("role") == "tool"),
        tool_call_count=len(tool_call_names),
        last_role=_clean_string(last_message.get("role")),
        summary=summary,
        issue_identifiers=issue_identifiers,
        project_hints=project_hints,
    )

    run_type, cron_job_id = _derive_run_type(path.name, session_id, _clean_string(data.get("platform")))
    status = _derive_run_status(messages, updated_dt=updated_dt, now=now)
    failure_reason = _derive_failure_reason(messages, status)
    duration_seconds = _duration_seconds(started_dt, updated_dt)

    run_record = RunRecord(
        run_id=session_id,
        session_id=session_id,
        run_type=run_type,
        platform=_clean_string(data.get("platform")),
        status=status,
        cron_job_id=cron_job_id,
        transcript_path=str(path),
        started_at=_to_iso(started_dt),
        updated_at=_to_iso(updated_dt),
        duration_seconds=duration_seconds,
        message_count=session_record.message_count,
        tool_call_count=session_record.tool_call_count,
        latest_tool_name=tool_call_names[-1] if tool_call_names else None,
        summary=summary,
        failure_reason=failure_reason,
        issue_identifiers=issue_identifiers,
        project_hints=project_hints,
    )
    return SessionRunBundle(session=session_record, run=run_record)


def _parse_project_description(description: str) -> dict[str, Optional[str]]:
    obsidian_path = _normalize_obsidian_path(_extract_pointer_line(description, _OBSIDIAN_LINE_RE))
    repo_pointer = _extract_pointer_line(description, _REPO_LINE_RE)
    discord_channel_name, discord_channel_id = _extract_discord_pointer(description)
    overview_path = _extract_labeled_pointer(description, "Overview")
    current_state_path = _extract_labeled_pointer(description, "Current state")
    stack_path = _extract_labeled_pointer(description, "Stack")
    goal = _extract_goal(description)
    project_key = _project_key_from_name(_extract_project_folder(obsidian_path)) if obsidian_path else None
    return {
        "obsidian_path": obsidian_path,
        "repo_pointer": repo_pointer,
        "discord_channel_name": discord_channel_name,
        "discord_channel_id": discord_channel_id,
        "overview_path": overview_path,
        "current_state_path": current_state_path,
        "stack_path": stack_path,
        "goal": goal,
        "project_key": project_key,
    }


def _load_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _extract_pointer_line(text: str, pattern: re.Pattern[str]) -> Optional[str]:
    match = pattern.search(text or "")
    if not match:
        return None
    value = _clean_string(match.group(1))
    if not value:
        return None
    return value.lstrip("* ").strip("` ") or None


def _extract_labeled_pointer(text: str, label: str) -> Optional[str]:
    pattern = re.compile(_POINTER_LINE_TEMPLATE.format(label=re.escape(label)), re.IGNORECASE)
    return _extract_pointer_line(text, pattern)


def _extract_discord_pointer(text: str) -> tuple[Optional[str], Optional[str]]:
    match = _DISCORD_LINE_RE.search(text or "")
    if match and (match.group(1) or match.group(2)):
        channel_name = _clean_string(match.group(1))
        channel_id = _clean_string(match.group(2))
        return channel_name, channel_id

    raw = _extract_labeled_pointer(text, "Discord")
    if not raw:
        return None, None
    channel_id_match = re.search(r"(?:ID:\s*)?([0-9]{6,})", raw)
    channel_id = _clean_string(channel_id_match.group(1)) if channel_id_match else None
    channel_name_match = re.search(r"#([A-Za-z0-9._-]+)", raw)
    channel_name = _clean_string(channel_name_match.group(1)) if channel_name_match else None
    if channel_name:
        return channel_name, channel_id
    cleaned = raw.split("(", 1)[0].strip().lstrip("#")
    return _clean_string(cleaned), channel_id


def _extract_goal(text: str) -> Optional[str]:
    match = _GOAL_SECTION_RE.search(text or "")
    if not match:
        return None
    return _collapse_whitespace(match.group(1)) or None


def _extract_project_folder(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    match = _OBSIDIAN_PROJECT_RE.search(path)
    if match:
        return _clean_string(match.group(1))
    return _clean_string(Path(path).name or Path(path).parent.name)


def _project_related(item: dict[str, Any], project: dict[str, Any], pointers: dict[str, Optional[str]]) -> bool:
    project_id = _clean_string(project.get("id"))
    item_project = item.get("project") if isinstance(item.get("project"), dict) else {}
    item_project_id = _clean_string(item_project.get("id"))
    if project_id and item_project_id == project_id:
        return True

    project_name = _clean_string(project.get("name"))
    item_project_name = _clean_string(item_project.get("name"))
    if project_name and item_project_name and _project_key_from_name(item_project_name) == _project_key_from_name(project_name):
        return True

    target_key = pointers.get("project_key") or _project_key_from_name(project_name)
    if target_key:
        hints = _extract_related_hints(item)
        if target_key in hints:
            return True
    return False


def _extract_related_hints(item: dict[str, Any]) -> set[str]:
    hints: set[str] = set()
    for hint in item.get("project_hints") or []:
        normalized = _project_key_from_name(_clean_string(hint))
        if normalized:
            hints.add(normalized)
    project = item.get("project") if isinstance(item.get("project"), dict) else {}
    for candidate in (project.get("name"), project.get("key"), item.get("project_key")):
        normalized = _project_key_from_name(_clean_string(candidate))
        if normalized:
            hints.add(normalized)
    return hints


def _collect_project_hints(
    project: dict[str, Any],
    pointers: dict[str, Optional[str]],
    related_issues: Iterable[dict[str, Any]],
    related_runs: Iterable[dict[str, Any]],
) -> list[str]:
    candidates: list[str] = []
    for candidate in (
        pointers.get("project_key"),
        _extract_project_folder(pointers.get("obsidian_path")),
        _project_key_from_name(_clean_string(project.get("name"))),
    ):
        if candidate:
            candidates.append(candidate)
    for item in list(related_issues) + list(related_runs):
        candidates.extend(item.get("project_hints") or [])
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        normalized = _project_key_from_name(_clean_string(item))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result[:10]


def _collect_issue_identifiers(
    related_issues: Iterable[dict[str, Any]],
    related_runs: Iterable[dict[str, Any]],
) -> list[str]:
    candidates: list[str] = []
    for item in related_issues:
        identifier = _clean_string(item.get("identifier"))
        if identifier:
            candidates.append(identifier)
    for run in related_runs:
        candidates.extend(run.get("issue_identifiers") or [])
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result[:25]


def _split_repo_pointer(pointer: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    pointer = _clean_string(pointer)
    if not pointer:
        return None, None
    if pointer.startswith(("http://", "https://", "git@")):
        return pointer, None
    if pointer.startswith("/") or pointer.startswith("~/"):
        return None, pointer
    return pointer, None


def _normalize_obsidian_path(path: Optional[str]) -> Optional[str]:
    path = _clean_string(path)
    if not path:
        return None
    if "/Projects/" not in path:
        return path
    return path if path.endswith("/") else f"{path}/"


def _obsidian_path_from_hint(hint: Optional[str]) -> Optional[str]:
    hint = _project_key_from_name(_clean_string(hint))
    if not hint:
        return None
    return f"/lab/obsidian_vault/Projects/{hint}/"


def _project_key_from_name(value: Optional[str]) -> Optional[str]:
    value = _clean_string(value)
    if not value:
        return None
    if "/Projects/" in value:
        value = _extract_project_folder(value) or value
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return normalized or None


def _first_project_hint(items: Iterable[dict[str, Any]]) -> Optional[str]:
    for item in items:
        for hint in item.get("project_hints") or []:
            normalized = _project_key_from_name(_clean_string(hint))
            if normalized:
                return normalized
    return None


def _project_status(related_issues: Iterable[dict[str, Any]], related_runs: Iterable[dict[str, Any]]) -> str:
    run_statuses = {_clean_string(item.get("status")) for item in related_runs}
    if "running" in run_statuses:
        return "active"
    issue_states = {
        _clean_string(((item.get("state") or {}).get("type") if isinstance(item.get("state"), dict) else None))
        for item in related_issues
    }
    if "started" in issue_states:
        return "active"
    if issue_states & {"unstarted", "backlog", "triage"}:
        return "planned"
    if "failed" in run_statuses:
        return "attention"
    if "completed" in run_statuses:
        return "idle"
    return "unknown"


def _metadata_status(
    *,
    obsidian_path: Optional[str],
    repo_url: Optional[str],
    repo_path: Optional[str],
    discord_channel_name: Optional[str],
    discord_channel_id: Optional[str],
) -> str:
    filled = sum(
        1
        for item in (obsidian_path, repo_url or repo_path, discord_channel_name or discord_channel_id)
        if item
    )
    if filled >= 3:
        return "complete"
    if filled >= 1:
        return "partial"
    return "minimal"


def _extract_timestamp(item: dict[str, Any]) -> Optional[str]:
    for key in ("updated_at", "updatedAt", "last_activity_at"):
        value = _clean_string(item.get(key))
        if value:
            return value
    return None


def _latest_timestamp(*values: Optional[str]) -> Optional[str]:
    parsed: list[datetime] = []
    for value in values:
        dt = _parse_dt(value)
        if dt is not None:
            parsed.append(dt)
    if not parsed:
        return None
    return max(parsed).isoformat()


def _first_non_empty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        cleaned = _clean_string(value)
        if cleaned:
            return cleaned
    return None


def _duration_seconds(started_at: Optional[datetime], updated_at: Optional[datetime]) -> Optional[int]:
    if not started_at or not updated_at:
        return None
    return max(0, int((updated_at - started_at).total_seconds()))


def _derive_run_type(filename: str, session_id: str, platform: Optional[str]) -> tuple[str, Optional[str]]:
    match = _CRON_FILE_RE.match(filename) or _CRON_SESSION_RE.match(session_id)
    if match:
        return "cron", match.group(1)
    if platform in {"cli", "discord", "telegram", "slack", "whatsapp", "signal", "matrix", "mattermost", "feishu", "wecom", "dingtalk", "sms", "email"}:
        return "interactive", None
    if platform == "cron":
        return "cron", None
    return "interactive", None


def _derive_run_status(
    messages: list[dict[str, Any]],
    *,
    updated_dt: Optional[datetime],
    now: datetime | None,
) -> str:
    if not messages:
        return "failed"

    current_time = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    last_message = messages[-1]
    last_role = last_message.get("role")
    finish_reason = _clean_string(last_message.get("finish_reason"))

    if last_role == "assistant" and finish_reason == "stop":
        return "completed"

    if updated_dt is not None:
        age_seconds = (current_time - updated_dt).total_seconds()
        if age_seconds <= _ACTIVE_WINDOW_SECONDS:
            if last_role == "assistant" and finish_reason in {"incomplete", "tool_calls"}:
                return "running"
            if last_role != "assistant":
                return "running"

    if last_role == "assistant" and finish_reason is None and _clean_string(last_message.get("content")):
        return "completed"

    return "failed"


def _derive_failure_reason(messages: list[dict[str, Any]], status: str) -> Optional[str]:
    if status != "failed":
        return None

    for message in reversed(messages):
        text = _message_text(message.get("content"))
        if text and _ERROR_SNIPPET_RE.search(text):
            return _truncate(_collapse_whitespace(text), _DEFAULT_SUMMARY_LENGTH)

    last_message = messages[-1] if messages else {}
    last_role = _clean_string(last_message.get("role")) or "unknown"
    return f"Run ended without a final assistant response (last role: {last_role})."


def _derive_summary(messages: list[dict[str, Any]]) -> str:
    final_assistant = _latest_message_text(messages, role="assistant")
    if final_assistant:
        return _truncate(final_assistant, _DEFAULT_SUMMARY_LENGTH)

    last_tool_text = _latest_message_text(messages, role="tool")
    if last_tool_text:
        return _truncate(last_tool_text, _DEFAULT_SUMMARY_LENGTH)

    last_error = None
    for message in reversed(messages):
        text = _message_text(message.get("content"))
        if text and _ERROR_SNIPPET_RE.search(text):
            last_error = text
            break
    if last_error:
        return _truncate(last_error, _DEFAULT_SUMMARY_LENGTH)

    first_user = _latest_message_text(list(reversed(messages)), role="user")
    if first_user:
        return _truncate(first_user, _DEFAULT_SUMMARY_LENGTH)

    return "No transcript summary available."


def _latest_message_text(messages: Iterable[dict[str, Any]], *, role: str) -> Optional[str]:
    for message in reversed(list(messages)):
        if message.get("role") != role:
            continue
        text = _message_text(message.get("content"))
        if text:
            return _collapse_whitespace(text)
    return None


def _tool_call_names(messages: Iterable[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for message in messages:
        for tool_call in message.get("tool_calls") or []:
            name = _clean_string(((tool_call or {}).get("function") or {}).get("name"))
            if name:
                names.append(name)
    return names


def _message_text(content: Any) -> Optional[str]:
    if content is None:
        return None
    if isinstance(content, str):
        return _collapse_whitespace(content[:_MAX_SUMMARY_SOURCE_LENGTH]) or None
    if isinstance(content, list):
        fragments: list[str] = []
        for item in content:
            if isinstance(item, str):
                fragments.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    fragments.append(text)
        joined = " ".join(fragments)
        return _collapse_whitespace(joined[:_MAX_SUMMARY_SOURCE_LENGTH]) or None
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        if isinstance(text, str):
            return _collapse_whitespace(text[:_MAX_SUMMARY_SOURCE_LENGTH]) or None
        return _collapse_whitespace(json.dumps(content)[:_MAX_SUMMARY_SOURCE_LENGTH]) or None
    return _collapse_whitespace(str(content)[:_MAX_SUMMARY_SOURCE_LENGTH]) or None


def _summary_text_sources(messages: list[dict[str, Any]], summary: str) -> list[str]:
    sources: list[str] = [summary]
    if messages:
        first_user = next((msg for msg in messages if msg.get("role") == "user"), None)
        if first_user:
            text = _message_text(first_user.get("content"))
            if text:
                sources.append(text)
        last_assistant = next((msg for msg in reversed(messages) if msg.get("role") == "assistant"), None)
        if last_assistant:
            text = _message_text(last_assistant.get("content"))
            if text:
                sources.append(text)
    return sources


def _extract_issue_identifiers(texts: Iterable[str]) -> list[str]:
    candidates: list[str] = []
    for text in texts:
        candidates.extend(_ISSUE_RE.findall(text or ""))
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result[:10]


def _extract_project_hints(texts: Iterable[str]) -> list[str]:
    candidates: list[str] = []
    for text in texts:
        candidates.extend(match.group(1) for match in _OBSIDIAN_PROJECT_RE.finditer(text or ""))
    seen: set[str] = set()
    result: list[str] = []
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result[:10]


def _collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _truncate(text: str, limit: int) -> str:
    collapsed = _collapse_whitespace(text)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _clean_string(value: Any) -> Optional[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None
