"""Linear Agent platform adapter.

Provides first-class Linear Agent Session support instead of treating Linear as a
stateless generic webhook source.

Endpoints:
- GET  /health
- GET  /linear/oauth/authorize
- GET  /linear/oauth/callback
- POST /linear/webhook

Configuration lives under ``platforms.linear.extra`` and/or env vars loaded by
``gateway.config._apply_env_overrides``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket as _socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate exercised elsewhere
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import build_session_key
from hermes_constants import get_hermes_home
from hermes_cli import kanban_db

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8646
DEFAULT_WEBHOOK_PATH = "/linear/webhook"
DEFAULT_AUTHORIZE_PATH = "/linear/oauth/authorize"
DEFAULT_CALLBACK_PATH = "/linear/oauth/callback"
DEFAULT_SCOPES = ["read", "write", "app:mentionable", "app:assignable"]
STATE_TTL_SECONDS = 1800
TOKEN_REFRESH_SKEW_SECONDS = 300
MAX_BODY_BYTES = 1_048_576
_TOKEN_STORE_FILENAME = "linear_oauth_tokens.json"
_STATE_STORE_FILENAME = "linear_oauth_states.json"
_LINEAR_APP_LOCK_SCOPE = "linear_app"
DEFAULT_MAX_CONCURRENT_SESSIONS = 3
DEFAULT_EXECUTION_MODE = "autonomous_with_testing"
SUPPORTED_EXECUTION_MODES = {
    "autonomous_dev",
    "autonomous_with_testing",
    "human_gate",
    "manual_only",
    "kanban_orchestrated",
}
DEFAULT_SUPPORTED_TASK_TYPES = ["engineering", "ops", "research", "product", "admin"]
DEFAULT_TASK_TYPE = "engineering"
DEFAULT_TASK_TYPE_LABEL_PREFIX = "type:"
DEFAULT_EXECUTION_MODE_LABEL_PREFIX = "mode:"
DEFAULT_IN_PROGRESS_STATE_NAME = "In Progress"
DEFAULT_BLOCKED_STATE_NAME = "Blocked"
DEFAULT_TESTING_STATE_NAME = "Testing"
DEFAULT_TESTING_FALLBACK_STATE_NAME = "In Review"
DEFAULT_IN_REVIEW_STATE_NAME = "In Review"
DEFAULT_DONE_STATE_NAME = "Done"
DEFAULT_BACKLOG_STATE_NAME = "Backlog"
DEFAULT_RECONCILE_INTERVAL_SECONDS = 60
DEFAULT_RECONCILE_LEASE_SECONDS = 300
VALID_WORKFLOW_DECISIONS = {
    "done",
    "ready_for_testing",
    "backlog",
    "stay_in_progress",
    "change_scope",
    "needs_human_review",
}

KANBAN_LINK_BLOCK_HEADER = "Linear ↔ Hermes Kanban"


def check_linear_requirements() -> bool:
    """Check if Linear adapter dependencies are available."""
    return AIOHTTP_AVAILABLE


class LinearAdapter(BasePlatformAdapter):
    """Native Linear Agent Session adapter."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.LINEAR)
        extra = config.extra or {}
        self._host = str(extra.get("host") or DEFAULT_HOST)
        self._port = int(extra.get("port") or DEFAULT_PORT)
        self._public_base_url = str(extra.get("public_base_url") or "").rstrip("/")
        self._webhook_path = str(extra.get("webhook_path") or DEFAULT_WEBHOOK_PATH)
        self._authorize_path = str(extra.get("authorize_path") or DEFAULT_AUTHORIZE_PATH)
        self._callback_path = str(extra.get("callback_path") or DEFAULT_CALLBACK_PATH)
        self._client_id = str(extra.get("client_id") or "")
        self._client_secret = str(extra.get("client_secret") or "")
        self._webhook_secret = str(extra.get("webhook_secret") or "")
        raw_scopes = extra.get("scopes") or DEFAULT_SCOPES
        if isinstance(raw_scopes, str):
            self._scopes = [p.strip() for p in raw_scopes.replace(" ", ",").split(",") if p.strip()]
        else:
            self._scopes = [str(p).strip() for p in raw_scopes if str(p).strip()]
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._max_body_bytes = int(extra.get("max_body_bytes") or MAX_BODY_BYTES)
        self._max_concurrent_sessions = max(1, int(extra.get("max_concurrent_sessions") or DEFAULT_MAX_CONCURRENT_SESSIONS))
        self._default_execution_mode = self._normalize_execution_mode(extra.get("default_execution_mode"))
        raw_project_modes = extra.get("project_execution_modes") or {}
        self._project_execution_modes = {
            str(key).strip(): self._normalize_execution_mode(value)
            for key, value in raw_project_modes.items()
            if str(key).strip()
        } if isinstance(raw_project_modes, dict) else {}
        raw_project_kanban_boards = extra.get("project_kanban_boards") or {}
        self._project_kanban_boards = {
            str(key).strip(): str(value).strip()
            for key, value in raw_project_kanban_boards.items()
            if str(key).strip() and str(value).strip()
        } if isinstance(raw_project_kanban_boards, dict) else {}
        self._kanban_auto_create_boards = bool(extra.get("kanban_auto_create_boards", True))
        self._kanban_post_start_comment = bool(extra.get("kanban_post_start_comment", True))
        self._kanban_include_link_block = bool(extra.get("kanban_include_link_block", True))
        raw_supported_task_types = extra.get("supported_task_types") or DEFAULT_SUPPORTED_TASK_TYPES
        if isinstance(raw_supported_task_types, str):
            self._supported_task_types = {
                task_type.strip().lower()
                for task_type in raw_supported_task_types.replace(" ", ",").split(",")
                if task_type.strip()
            }
        else:
            self._supported_task_types = {
                str(task_type).strip().lower()
                for task_type in raw_supported_task_types
                if str(task_type).strip()
            }
        if not self._supported_task_types:
            self._supported_task_types = {DEFAULT_TASK_TYPE}
        self._task_type_label_prefix = str(extra.get("task_type_label_prefix") or DEFAULT_TASK_TYPE_LABEL_PREFIX).strip().lower()
        self._execution_mode_label_prefix = str(extra.get("execution_mode_label_prefix") or DEFAULT_EXECUTION_MODE_LABEL_PREFIX).strip().lower()
        self._in_progress_state_name = str(extra.get("in_progress_state_name") or DEFAULT_IN_PROGRESS_STATE_NAME)
        self._blocked_state_name = str(extra.get("blocked_state_name") or DEFAULT_BLOCKED_STATE_NAME)
        self._testing_state_name = str(extra.get("testing_state_name") or DEFAULT_TESTING_STATE_NAME)
        self._testing_fallback_state_name = str(extra.get("testing_fallback_state_name") or DEFAULT_TESTING_FALLBACK_STATE_NAME)
        self._in_review_state_name = str(extra.get("in_review_state_name") or DEFAULT_IN_REVIEW_STATE_NAME)
        self._done_state_name = str(extra.get("done_state_name") or DEFAULT_DONE_STATE_NAME)
        self._backlog_state_name = str(extra.get("backlog_state_name") or DEFAULT_BACKLOG_STATE_NAME)
        self._reconcile_interval_seconds = max(0, int(extra.get("reconcile_interval_seconds") or DEFAULT_RECONCILE_INTERVAL_SECONDS))
        self._reconcile_lease_seconds = max(self._reconcile_interval_seconds, int(extra.get("reconcile_lease_seconds") or DEFAULT_RECONCILE_LEASE_SECONDS))
        self._reconcile_task: Optional[asyncio.Task] = None
        self._reconciled_issue_leases: Dict[str, Dict[str, Any]] = {}
        self._session_semaphore = asyncio.Semaphore(self._max_concurrent_sessions)
        self._session_counter_lock = asyncio.Lock()
        self._running_session_count = 0
        self._queued_session_count = 0
        self._session_info: Dict[str, Dict[str, Any]] = {}
        self._issue_state_cache: Dict[str, List[Dict[str, Any]]] = {}
        self._tokens_path = get_hermes_home() / _TOKEN_STORE_FILENAME
        self._states_path = get_hermes_home() / _STATE_STORE_FILENAME

    @staticmethod
    def _normalize_execution_mode(value: Any) -> str:
        mode = str(value or DEFAULT_EXECUTION_MODE).strip().lower()
        return mode if mode in SUPPORTED_EXECUTION_MODES else DEFAULT_EXECUTION_MODE

    def _extract_label_names(self, issue: Dict[str, Any]) -> List[str]:
        labels = issue.get("labels") or issue.get("labelIds") or []
        if isinstance(labels, dict):
            labels = labels.get("nodes") or labels.get("items") or []
        names: List[str] = []
        for label in labels:
            if isinstance(label, dict):
                name = str(label.get("name") or label.get("label") or "").strip()
            else:
                name = str(label).strip()
            if name:
                names.append(name)
        return names

    def _derive_task_type(self, issue: Dict[str, Any]) -> str:
        for label in self._extract_label_names(issue):
            lowered = label.lower()
            if lowered.startswith(self._task_type_label_prefix):
                task_type = lowered[len(self._task_type_label_prefix):].strip()
                if task_type:
                    return task_type
        return DEFAULT_TASK_TYPE

    def _derive_execution_mode(self, issue: Dict[str, Any]) -> str:
        for label in self._extract_label_names(issue):
            lowered = label.lower()
            if lowered.startswith(self._execution_mode_label_prefix):
                mode = lowered[len(self._execution_mode_label_prefix):].strip()
                return self._normalize_execution_mode(mode)

        project = issue.get("project") or {}
        project_name = str(project.get("name") or "").strip()
        project_id = str(project.get("id") or "").strip()
        if project_id and project_id in self._project_execution_modes:
            return self._project_execution_modes[project_id]
        if project_name and project_name in self._project_execution_modes:
            return self._project_execution_modes[project_name]
        return self._default_execution_mode

    def _build_execution_policy(self, issue: Dict[str, Any]) -> Dict[str, Any]:
        task_type = self._derive_task_type(issue)
        execution_mode = self._derive_execution_mode(issue)
        can_execute = execution_mode != "manual_only" and task_type in self._supported_task_types
        block_reason = None
        if execution_mode == "manual_only":
            block_reason = "Project is configured for manual_only execution mode."
        elif task_type not in self._supported_task_types:
            block_reason = f"Task type '{task_type}' is not executable by the current Jax executor."
        return {
            "task_type": task_type,
            "execution_mode": execution_mode,
            "can_execute": can_execute,
            "block_reason": block_reason,
        }

    @staticmethod
    def _normalize_project_key(name: Any) -> Optional[str]:
        normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(name or "")).strip("_")
        return normalized or None

    @staticmethod
    def _normalize_kanban_board_slug(value: Any) -> Optional[str]:
        slug = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower())
        slug = re.sub(r"-+", "-", slug).strip("-_")
        return slug or None

    @classmethod
    def _derive_repo_board_slug(cls, repo_url: Any) -> Optional[str]:
        repo = str(repo_url or "").strip().rstrip("/")
        if not repo:
            return None
        if ":" in repo and "/" in repo and not repo.startswith(("http://", "https://")):
            repo = repo.split(":", 1)[-1]
        repo_name = repo.rsplit("/", 1)[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        return cls._normalize_kanban_board_slug(repo_name)

    def _derive_kanban_board(self, issue: Dict[str, Any], project_metadata: Optional[Dict[str, Any]] = None) -> tuple[Optional[str], Optional[str]]:
        project = issue.get("project") or {}
        project_name = str((project_metadata or {}).get("project_name") or project.get("name") or "").strip()
        project_id = str(project.get("id") or "").strip()
        if project_id and project_id in self._project_kanban_boards:
            board = self._normalize_kanban_board_slug(self._project_kanban_boards[project_id])
            if board:
                return board, "project_mapping"
        if project_name and project_name in self._project_kanban_boards:
            board = self._normalize_kanban_board_slug(self._project_kanban_boards[project_name])
            if board:
                return board, "project_mapping"
        repo_board = self._derive_repo_board_slug((project_metadata or {}).get("repo_url"))
        if repo_board:
            return repo_board, "repo"
        project_board = self._normalize_kanban_board_slug(project_name)
        if project_board:
            return project_board, "project_name"
        return None, None

    def _build_kanban_guidance_block(self, session: Dict[str, Any]) -> str:
        board_slug = str(session.get("kanban_board_slug") or "").strip() or "(derive-or-create a repo/domain board)"
        link_block = self._build_kanban_link_block(session, include_commands=False)
        return (
            "Kanban orchestration policy:\n"
            "- Linear is the human-facing control plane. Hermes Kanban is the execution plane.\n"
            f"- Hermes Kanban board: {board_slug}\n"
            "- Do execution work through the Kanban board: break the issue into runnable tasks, use dependencies, and keep tactical execution there instead of turning Linear into the worker queue.\n"
            "- Do not mirror every Kanban task back into Linear. Sync only milestone-level updates: started, blocked, needs approval, ready for testing, done.\n"
            "- Keep Linear titles and comments outcome-oriented. Keep Kanban tasks tactical and execution-oriented.\n\n"
            f"{link_block}\n\n"
        )

    def _ensure_kanban_board_ready(self, session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if str(session.get("execution_mode") or "") != "kanban_orchestrated":
            return None
        board_slug = self._normalize_kanban_board_slug(session.get("kanban_board_slug"))
        if not board_slug:
            return None
        project_name = str(session.get("project_name") or "").strip()
        meta: Optional[Dict[str, Any]] = None
        already_exists = kanban_db.board_exists(board_slug)
        session["kanban_board_exists"] = already_exists
        session["kanban_board_auto_created"] = False
        if already_exists:
            try:
                meta = kanban_db.read_board_metadata(board_slug)
            except Exception:
                logger.debug("[linear] Failed reading existing kanban board metadata for %s", board_slug, exc_info=True)
            return meta
        if not self._kanban_auto_create_boards:
            return None
        try:
            meta = kanban_db.create_board(
                board_slug,
                name=project_name or None,
                description=(
                    f"Auto-created by Linear kanban orchestration for {project_name}."
                    if project_name else
                    "Auto-created by Linear kanban orchestration."
                ),
            )
            session["kanban_board_exists"] = True
            session["kanban_board_auto_created"] = True
            return meta
        except Exception:
            logger.warning("[linear] Failed to auto-create kanban board %s", board_slug, exc_info=True)
            return None

    def _build_kanban_link_block(self, session: Dict[str, Any], *, include_commands: bool = True) -> str:
        if not self._kanban_include_link_block:
            return ""
        issue_identifier = str(session.get("issue_identifier") or session.get("issue_id") or "").strip() or "(unknown issue)"
        issue_title = str(session.get("issue_title") or "").strip()
        board_slug = str(session.get("kanban_board_slug") or "").strip() or "(pending board resolution)"
        board_source = str(session.get("kanban_board_source") or "").strip() or "derived"
        state = "auto-created" if session.get("kanban_board_auto_created") else "existing"
        issue_line = issue_identifier if not issue_title else f"{issue_identifier} — {issue_title}"
        lines = [
            KANBAN_LINK_BLOCK_HEADER,
            f"- Linear issue: {issue_line}",
            f"- Kanban board: {board_slug} ({state}, source: {board_source})",
            "- Policy: Linear tracks milestones; Hermes Kanban owns tactical execution.",
        ]
        if include_commands and board_slug and not board_slug.startswith("("):
            lines.append(f"- Local follow-up: `hermes kanban boards switch {board_slug}`")
        return "\n".join(lines)

    def _build_kanban_start_comment(self, session: Dict[str, Any]) -> Optional[str]:
        if str(session.get("execution_mode") or "") != "kanban_orchestrated":
            return None
        board_slug = str(session.get("kanban_board_slug") or "").strip()
        if not board_slug:
            return None
        opening = f"Jax is executing this issue through Hermes Kanban on board `{board_slug}`."
        if session.get("kanban_board_auto_created"):
            opening += " I auto-created the board for this project."
        link_block = self._build_kanban_link_block(session)
        if link_block:
            return f"{opening}\n\n{link_block}"
        return opening

    @staticmethod
    def _extract_project_registry_metadata(prompt_context: Any) -> Dict[str, Any]:
        text = str(prompt_context or "")
        if not text:
            return {}

        metadata: Dict[str, Any] = {}

        project_match = re.search(r'<project\s+name="([^"]+)"[^>]*>(.*?)</project>', text, re.IGNORECASE | re.DOTALL)
        if project_match:
            project_name = project_match.group(1).strip()
            if project_name:
                metadata["project_name"] = project_name
                project_key = LinearAdapter._normalize_project_key(project_name)
                if project_key:
                    metadata["project_key"] = project_key
            project_blob = project_match.group(2)
        else:
            project_blob = text

        obsidian_match = re.search(r"Obsidian:\s*([^|\n<]+)", project_blob, re.IGNORECASE)
        if obsidian_match:
            metadata["obsidian_path"] = obsidian_match.group(1).strip()

        repo_match = re.search(r"Repo:\s*([^|\n<]+)", project_blob, re.IGNORECASE)
        if repo_match:
            repo_value = repo_match.group(1).strip()
            if repo_value and repo_value.startswith("github.com/"):
                repo_value = f"https://{repo_value}"
            if repo_value:
                metadata["repo_url"] = repo_value

        discord_match = re.search(
            r"Discord:\s*#?([^\s|<]+)\s*\((\d+)\)",
            project_blob,
            re.IGNORECASE,
        )
        if discord_match:
            metadata["discord_channel_name"] = discord_match.group(1).strip()
            metadata["discord_channel_id"] = discord_match.group(2).strip()

        return metadata

    def _store_session_metadata(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        agent_session = payload.get("agentSession") or {}
        issue = agent_session.get("issue") or {}
        chat_id = f"linear:{agent_session.get('id') or ''}"
        policy = self._build_execution_policy(issue)
        project = issue.get("project") or {}
        creator = agent_session.get("creator") or {}
        assignee = issue.get("assignee") or {}
        project_metadata = self._extract_project_registry_metadata(payload.get("promptContext"))
        kanban_board_slug, kanban_board_source = self._derive_kanban_board(issue, project_metadata)
        session = {
            "context_type": "linear_agent_session",
            "agent_session_id": str(agent_session.get("id") or ""),
            "app_user_id": str(payload.get("appUserId") or agent_session.get("appUserId") or ""),
            "organization_id": str(payload.get("organizationId") or agent_session.get("organizationId") or ""),
            "chat_name": issue.get("identifier") or issue.get("title") or agent_session.get("url") or chat_id,
            "issue_id": str(issue.get("id") or issue.get("identifier") or ""),
            "issue_identifier": str(issue.get("identifier") or issue.get("id") or ""),
            "issue_title": str(issue.get("title") or ""),
            "team_id": str((issue.get("team") or {}).get("id") or issue.get("teamId") or ""),
            "team_name": str((issue.get("team") or {}).get("name") or issue.get("team") or ""),
            "project_id": str(project.get("id") or ""),
            "project_name": str(project.get("name") or ""),
            "project_key": self._normalize_project_key(project.get("name") or "") or None,
            "label_names": self._extract_label_names(issue),
            "creator_id": str(agent_session.get("creatorId") or creator.get("id") or ""),
            "creator_name": str(creator.get("name") or ""),
            "current_assignee_id": str(assignee.get("id") or issue.get("assigneeId") or "") or None,
            "current_assignee_name": str(assignee.get("name") or issue.get("assignee") or "") or None,
            "kanban_board_slug": kanban_board_slug,
            "kanban_board_source": kanban_board_source,
            "updated_at": time.time(),
            **project_metadata,
            **policy,
        }
        self._ensure_kanban_board_ready(session)
        self._session_info[chat_id] = session
        return session

    def _determine_handoff_assignee(self, session: Dict[str, Any]) -> Optional[str]:
        current_assignee = session.get("current_assignee_id")
        if current_assignee and current_assignee != session.get("app_user_id"):
            return current_assignee
        creator_id = session.get("creator_id")
        return creator_id or None

    @staticmethod
    def _is_blocking_relation_type(value: Any) -> bool:
        normalized = re.sub(r"[^a-z]", "", str(value or "").lower())
        return normalized in {"blockedby", "blockedbyrelation", "blockedbyissue"}

    async def _list_unresolved_dependencies(self, session: Dict[str, Any]) -> List[Dict[str, str]]:
        issue_id = str(session.get("issue_id") or session.get("issue_identifier") or "")
        app_user_id = str(session.get("app_user_id") or "")
        if not issue_id or not app_user_id:
            return []
        access_token = await self._ensure_access_token(app_user_id)
        payload = {
            "query": (
                "query { issue(id: \""
                + issue_id
                + "\") { relations { nodes { type relatedIssue { identifier title state { name type } } } } } }"
            )
        }
        result = await asyncio.to_thread(
            self._http_json,
            "https://api.linear.app/graphql",
            payload,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        relations = (((result.get("data") or {}).get("issue") or {}).get("relations") or {}).get("nodes") or []
        unresolved: List[Dict[str, str]] = []
        for relation in relations:
            if not self._is_blocking_relation_type(relation.get("type")):
                continue
            related_issue = relation.get("relatedIssue") or {}
            state = related_issue.get("state") or {}
            state_type = str(state.get("type") or "").strip().lower()
            if state_type in {"completed", "canceled"}:
                continue
            unresolved.append(
                {
                    "identifier": str(related_issue.get("identifier") or related_issue.get("title") or "dependency"),
                    "state": str(state.get("name") or state_type or "unknown"),
                }
            )
        return unresolved

    def _build_dependency_block_message(self, unresolved_dependencies: List[Dict[str, str]]) -> str:
        dependency = unresolved_dependencies[0]
        return (
            "Jax did not start because this issue depends on unresolved work: "
            f"{dependency.get('identifier', 'dependency')} ({dependency.get('state', 'unknown')})."
        )

    def _classify_retry_decision(self, session: Dict[str, Any]) -> Dict[str, Any]:
        retry_policy = str(session.get("retry_policy") or "none").strip().lower()
        max_attempts_by_policy = {
            "none": 0,
            "conservative": 1,
            "standard": 2,
            "aggressive": 4,
        }
        max_attempts = max_attempts_by_policy.get(retry_policy, 0)
        error_class = str(session.get("last_error_class") or "permanent_failure").strip().lower()
        attempt_count = int(session.get("retry_attempt_count") or 0)
        retryable = error_class in {"transient_api", "transient_tool"}
        should_retry = retryable and attempt_count < max_attempts
        next_attempt = attempt_count + 1 if should_retry else attempt_count
        return {
            "retry_policy": retry_policy,
            "error_class": error_class,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "retryable": retryable,
            "should_retry": should_retry,
            "next_attempt": next_attempt,
        }

    def _classify_refit_decision(self, session: Dict[str, Any]) -> Dict[str, Any]:
        autonomy_first = bool(session.get("autonomy_first", False))
        error_class = str(session.get("last_error_class") or "").strip().lower()
        refit_attempt_count = int(session.get("refit_attempt_count") or 0)
        max_refits = int(session.get("max_refit_attempts") or 1)
        refittable = autonomy_first and error_class in {"agent_execution_error", "scope_too_large", "ambiguous_spec"}
        should_refit = refittable and refit_attempt_count < max_refits
        next_attempt = refit_attempt_count + 1 if should_refit else refit_attempt_count
        return {
            "autonomy_first": autonomy_first,
            "error_class": error_class,
            "refit_attempt_count": refit_attempt_count,
            "max_refits": max_refits,
            "refittable": refittable,
            "should_refit": should_refit,
            "next_attempt": next_attempt,
        }

    def _resolve_success_workflow_decision(self, session: Dict[str, Any]) -> str:
        execution_mode = str(session.get("execution_mode") or self._default_execution_mode)
        decision = str(session.get("workflow_decision") or "").strip().lower()
        if decision in VALID_WORKFLOW_DECISIONS:
            if execution_mode == "autonomous_dev" and decision == "ready_for_testing":
                return "done"
            if execution_mode == "human_gate" and decision in {"done", "ready_for_testing"}:
                return "needs_human_review"
            return decision
        if execution_mode == "autonomous_dev":
            return "done"
        if execution_mode == "human_gate":
            return "needs_human_review"
        return "done"

    def _classify_success_rerun_budget(self, session: Dict[str, Any]) -> Dict[str, Any]:
        current_count = int(session.get("success_rerun_count") or 0)
        max_reruns = max(1, int(session.get("max_success_reruns") or 3))
        allowed = current_count < max_reruns
        next_count = current_count + 1 if allowed else current_count
        return {
            "current_count": current_count,
            "max_reruns": max_reruns,
            "allowed": allowed,
            "next_count": next_count,
        }

    def _build_scope_followup_title(self, session: Dict[str, Any]) -> str:
        base_title = str(session.get("issue_title") or session.get("issue_identifier") or "Follow-up slice").strip()
        suffix = " — follow-up slice"
        if base_title.endswith(suffix):
            return base_title
        max_base_len = 140 - len(suffix)
        if len(base_title) > max_base_len:
            base_title = base_title[: max_base_len - 1].rstrip() + "…"
        return f"{base_title}{suffix}"

    def _build_scope_followup_description(self, session: Dict[str, Any], reason: str) -> str:
        issue_identifier = str(session.get("issue_identifier") or session.get("issue_id") or "this issue").strip()
        issue_title = str(session.get("issue_title") or "").strip()
        lines = [
            f"Autonomous follow-up created by Jax while narrowing scope for {issue_identifier}.",
        ]
        if issue_title:
            lines.append(f"Parent issue title: {issue_title}")
        if reason:
            lines.append(f"Why Jax narrowed scope: {reason}")
        lines.append("Purpose: track deferred remaining scope that was split out while the current issue continues on the active slice.")
        return "\n\n".join(lines)

    async def _create_followup_issue(
        self,
        session: Dict[str, Any],
        *,
        title: str,
        description: str,
    ) -> Dict[str, Any]:
        team_id = str(session.get("team_id") or "").strip()
        app_user_id = str(session.get("app_user_id") or "").strip()
        if not team_id or not app_user_id:
            raise RuntimeError("Missing team_id or app_user_id for Linear follow-up creation")
        access_token = await self._ensure_access_token(app_user_id)
        issue_id = str(session.get("issue_id") or "").strip()
        project_id = str(session.get("project_id") or "").strip()
        assignee_id = str(session.get("current_assignee_id") or "").strip()
        input_payload: Dict[str, Any] = {
            "teamId": team_id,
            "title": title,
            "description": description,
        }
        if issue_id:
            input_payload["parentId"] = issue_id
        if project_id:
            input_payload["projectId"] = project_id
        if assignee_id:
            input_payload["assigneeId"] = assignee_id
        payload = {
            "query": (
                "mutation($input: IssueCreateInput!) { "
                "issueCreate(input: $input) { success issue { id identifier title } } }"
            ),
            "variables": {"input": input_payload},
        }
        result = await asyncio.to_thread(
            self._http_json,
            "https://api.linear.app/graphql",
            payload,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        errors = result.get("errors") or []
        if errors:
            raise RuntimeError(errors[0].get("message") or str(errors[0]))
        create_payload = ((result.get("data") or {}).get("issueCreate") or {})
        if not create_payload.get("success"):
            raise RuntimeError(f"issueCreate failed: {result}")
        return (create_payload.get("issue") or {})

    async def _ensure_scope_followup_issue(self, session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        reason = str(session.get("workflow_decision_reason") or "").strip()
        existing_identifier = str(session.get("scope_followup_issue_identifier") or "").strip()
        existing_reason = str(session.get("scope_followup_issue_reason") or "").strip()
        if existing_identifier and existing_reason == reason:
            return {
                "id": str(session.get("scope_followup_issue_id") or "").strip(),
                "identifier": existing_identifier,
                "title": str(session.get("scope_followup_issue_title") or "").strip(),
            }
        if not reason:
            return None
        followup = await self._create_followup_issue(
            session,
            title=self._build_scope_followup_title(session),
            description=self._build_scope_followup_description(session, reason),
        )
        session["scope_followup_issue_reason"] = reason
        session["scope_followup_issue_id"] = str(followup.get("id") or "").strip() or None
        session["scope_followup_issue_identifier"] = str(followup.get("identifier") or "").strip() or None
        session["scope_followup_issue_title"] = str(followup.get("title") or "").strip() or None
        return followup

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        missing = []
        if not self._client_id:
            missing.append("client_id")
        if not self._client_secret:
            missing.append("client_secret")
        if not self._webhook_secret:
            missing.append("webhook_secret")
        if missing:
            logger.error("[linear] Missing required config: %s", ", ".join(missing))
            return False

        if not self._acquire_platform_lock(
            _LINEAR_APP_LOCK_SCOPE,
            self._client_id,
            "Linear app credentials",
        ):
            return False

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_get(self._authorize_path, self._handle_authorize)
        app.router.add_get(self._callback_path, self._handle_callback)
        app.router.add_post(self._webhook_path, self._handle_webhook)

        try:
            try:
                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    sock.connect(("127.0.0.1", self._port))
                logger.error("[linear] Port %d already in use", self._port)
                self._release_platform_lock()
                return False
            except (ConnectionRefusedError, OSError):
                pass

            self._runner = web.AppRunner(app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
            self._mark_connected()
            if self._reconcile_interval_seconds > 0:
                self._reconcile_task = asyncio.create_task(self._reconcile_delegated_started_issues_loop())
                self._background_tasks.add(self._reconcile_task)
                self._reconcile_task.add_done_callback(self._background_tasks.discard)
            logger.info(
                "[linear] Listening on %s:%d (authorize=%s callback=%s webhook=%s)",
                self._host,
                self._port,
                self._authorize_path,
                self._callback_path,
                self._webhook_path,
            )
            return True
        except Exception:
            self._release_platform_lock()
            raise

    async def disconnect(self) -> None:
        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass
            self._reconcile_task = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        self._release_platform_lock()
        self._mark_disconnected()
        logger.info("[linear] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        session = self._session_info.get(chat_id)
        if not session:
            return SendResult(success=False, error=f"Unknown Linear session: {chat_id}")

        activity_type = str((metadata or {}).get("linear_activity_type") or "response")
        ephemeral = bool((metadata or {}).get("ephemeral", False))
        signal = (metadata or {}).get("signal")
        try:
            if session.get("agent_session_id"):
                result = await self._create_activity(
                    app_user_id=session["app_user_id"],
                    agent_session_id=session["agent_session_id"],
                    activity_type=activity_type,
                    body=content,
                    ephemeral=ephemeral,
                    signal=signal,
                )
                activity = ((result or {}).get("agentActivityCreate") or {}).get("agentActivity") or {}
                return SendResult(success=True, message_id=activity.get("id"))
            if session.get("issue_id"):
                result = await self._create_issue_comment(
                    app_user_id=session["app_user_id"],
                    issue_id=session["issue_id"],
                    body=content,
                )
                comment = ((result or {}).get("commentCreate") or {}).get("comment") or {}
                return SendResult(success=True, message_id=comment.get("id"))
            return SendResult(success=False, error=f"Linear session {chat_id} has no agent session or issue target")
        except Exception as exc:
            logger.error("[linear] Failed to send activity for %s: %s", chat_id, exc)
            return SendResult(success=False, error=str(exc))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        info = self._session_info.get(chat_id, {})
        return {
            "name": info.get("chat_name") or chat_id,
            "type": "linear",
            "chat_id": chat_id,
            "session_metadata": dict(info) if info else {},
        }

    def _extract_workflow_decision_from_response(self, response: str) -> tuple[str, Optional[Dict[str, str]]]:
        if not response:
            return response, None
        match = re.search(r"```hermes_workflow\s*(\{.*?\})\s*```\s*$", response, re.DOTALL | re.IGNORECASE)
        if not match:
            return response, None
        try:
            payload = json.loads(match.group(1))
        except Exception:
            return response, None
        decision = str(payload.get("decision") or "").strip().lower()
        if decision not in VALID_WORKFLOW_DECISIONS:
            return response, None
        reason = str(payload.get("reason") or "").strip()
        cleaned = (response[:match.start()] + response[match.end():]).strip()
        metadata = {"decision": decision}
        if reason:
            metadata["reason"] = reason
        return cleaned, metadata

    def apply_agent_result_metadata(self, chat_id: str, agent_result: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(agent_result, dict):
            return agent_result
        session = self._session_info.get(chat_id)
        if not session:
            return agent_result
        session.pop("workflow_decision", None)
        session.pop("workflow_decision_reason", None)
        final_response = str(agent_result.get("final_response") or "")
        cleaned_response, workflow = self._extract_workflow_decision_from_response(final_response)
        if workflow:
            session["workflow_decision"] = workflow["decision"]
            if workflow.get("reason"):
                session["workflow_decision_reason"] = workflow["reason"]
            agent_result["final_response"] = cleaned_response
            messages = agent_result.get("messages")
            if isinstance(messages, list):
                for message in reversed(messages):
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        content = message.get("content")
                        if isinstance(content, str):
                            message["content"] = cleaned_response
                        break
        return agent_result

    def _build_reconciled_issue_chat_id(self, issue_id: str) -> str:
        return f"linear:issue:{issue_id}"

    def _should_reconcile_issue(self, issue: Dict[str, Any]) -> bool:
        issue_id = str(issue.get("id") or issue.get("identifier") or "").strip()
        if not issue_id:
            return False
        state_name = str((issue.get("state") or {}).get("name") or "").strip().lower()
        excluded_state_names = {
            self._testing_state_name.strip().lower(),
            self._testing_fallback_state_name.strip().lower(),
            self._in_review_state_name.strip().lower(),
            self._blocked_state_name.strip().lower(),
            self._backlog_state_name.strip().lower(),
            self._done_state_name.strip().lower(),
        }
        if state_name and state_name in excluded_state_names:
            return False
        lease = self._reconciled_issue_leases.get(issue_id) or {}
        updated_at = str(issue.get("updatedAt") or "").strip()
        leased_at = float(lease.get("leased_at") or 0)
        leased_updated_at = str(lease.get("updated_at") or "").strip()
        if leased_at and leased_updated_at and leased_updated_at == updated_at:
            if time.time() - leased_at < self._reconcile_lease_seconds:
                return False
        return True

    def _build_reconciled_issue_prompt(self, issue: Dict[str, Any], session: Dict[str, Any]) -> str:
        issue_identifier = str(issue.get("identifier") or issue.get("id") or "").strip()
        issue_title = str(issue.get("title") or "").strip()
        issue_description = str(issue.get("description") or "").strip()
        project_name = str((issue.get("project") or {}).get("name") or session.get("project_name") or "").strip()
        execution_mode = str(session.get("execution_mode") or self._default_execution_mode)
        allowed_decisions = ["done", "backlog", "stay_in_progress", "change_scope", "needs_human_review"]
        if execution_mode != "human_gate":
            allowed_decisions.insert(1, "ready_for_testing")
        example_decision = "done" if execution_mode != "human_gate" else "needs_human_review"
        orchestration_guidance = self._build_kanban_guidance_block(session) if execution_mode == "kanban_orchestrated" else ""
        return (
            "A delegated Linear issue is already in progress and must continue autonomously now.\n\n"
            f"Issue: {issue_identifier} {issue_title}\n"
            f"Project: {project_name or '(none)'}\n"
            f"Task type: {session.get('task_type', DEFAULT_TASK_TYPE)}\n"
            f"Execution mode: {execution_mode}\n\n"
            "Description:\n"
            f"{issue_description or '(no description)'}\n\n"
            f"{orchestration_guidance}"
            "Testing semantics are strict: choose `ready_for_testing` only when actual testing is still pending. "
            "If manual testing is required, your visible response must include a concrete QA handoff covering what changed, where to test, exact steps, expected result, what you already validated, and remaining risk. "
            "If you already validated the slice sufficiently and no real testing action remains, choose `done` instead of `ready_for_testing`.\n\n"
            "At the end of your visible response, append a fenced `hermes_workflow` JSON block like:\n"
            "```hermes_workflow\n"
            f'{{"decision": "{example_decision}", "reason": "brief rationale"}}\n'
            "```\n"
            f"Allowed decisions: {', '.join(allowed_decisions)}."
        )

    def _store_reconciled_issue_session(self, issue: Dict[str, Any], app_user_id: str) -> Dict[str, Any]:
        issue_id = str(issue.get("id") or issue.get("identifier") or "").strip()
        chat_id = self._build_reconciled_issue_chat_id(issue_id)
        project = issue.get("project") or {}
        assignee = issue.get("assignee") or {}
        creator = issue.get("creator") or {}
        policy = self._build_execution_policy(issue)
        kanban_board_slug, kanban_board_source = self._derive_kanban_board(issue)
        session = {
            "context_type": "linear_issue_reconcile",
            "app_user_id": str(app_user_id or ""),
            "chat_name": issue.get("identifier") or issue.get("title") or chat_id,
            "issue_id": issue_id,
            "issue_identifier": str(issue.get("identifier") or issue_id),
            "issue_title": str(issue.get("title") or ""),
            "team_id": str((issue.get("team") or {}).get("id") or issue.get("teamId") or ""),
            "team_name": str((issue.get("team") or {}).get("name") or issue.get("team") or ""),
            "project_id": str(project.get("id") or ""),
            "project_name": str(project.get("name") or ""),
            "project_key": self._normalize_project_key(project.get("name") or "") or None,
            "label_names": self._extract_label_names(issue),
            "creator_id": str(creator.get("id") or ""),
            "creator_name": str(creator.get("name") or ""),
            "current_assignee_id": str(assignee.get("id") or issue.get("assigneeId") or "") or None,
            "current_assignee_name": str(assignee.get("name") or issue.get("assignee") or "") or None,
            "kanban_board_slug": kanban_board_slug,
            "kanban_board_source": kanban_board_source,
            "updated_at": time.time(),
            **policy,
        }
        self._ensure_kanban_board_ready(session)
        self._session_info[chat_id] = session
        return session

    async def _create_issue_comment(self, *, app_user_id: str, issue_id: str, body: str) -> Dict[str, Any]:
        access_token = await self._ensure_access_token(app_user_id)
        payload = {
            "query": (
                "mutation($input: CommentCreateInput!) { "
                "commentCreate(input: $input) { success comment { id body } } }"
            ),
            "variables": {"input": {"issueId": issue_id, "body": body}},
        }
        result = await asyncio.to_thread(
            self._http_json,
            "https://api.linear.app/graphql",
            payload,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        errors = result.get("errors") or []
        if errors:
            raise RuntimeError(errors[0].get("message") or str(errors[0]))
        create_payload = ((result.get("data") or {}).get("commentCreate") or {})
        if not create_payload.get("success"):
            raise RuntimeError(f"commentCreate failed: {result}")
        return (result.get("data") or {})

    async def _list_delegated_started_issues(self, app_user_id: str) -> List[Dict[str, Any]]:
        access_token = await self._ensure_access_token(app_user_id)
        issues: List[Dict[str, Any]] = []
        after: Optional[str] = None
        while True:
            payload = {
                "query": (
                    "query($delegateId: ID!, $after: String) { "
                    "issues(filter: { delegate: { id: { eq: $delegateId } }, state: { type: { eq: \"started\" } } }, first: 25, after: $after) { nodes { "
                    "id identifier title description updatedAt state { name type } team { id name } project { id name } assignee { id name } creator { id name } labels { nodes { name } } "
                    "} pageInfo { hasNextPage endCursor } } }"
                ),
                "variables": {"delegateId": app_user_id, "after": after},
            }
            result = await asyncio.to_thread(
                self._http_json,
                "https://api.linear.app/graphql",
                payload,
                {
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
            errors = result.get("errors") or []
            if errors:
                raise RuntimeError(errors[0].get("message") or str(errors[0]))
            issues_payload = ((result.get("data") or {}).get("issues") or {})
            issues.extend(issues_payload.get("nodes") or [])
            page_info = issues_payload.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = str(page_info.get("endCursor") or "").strip() or None
            if not after:
                break
        return issues

    async def _reconcile_delegated_started_issues_once(self) -> None:
        stored = self._load_json(self._tokens_path)
        for app_user_id in list(stored.keys()):
            if not str(app_user_id).strip():
                continue
            try:
                issues = await self._list_delegated_started_issues(str(app_user_id))
            except Exception:
                logger.debug("[linear] Failed to reconcile delegated started issues for %s", app_user_id, exc_info=True)
                continue
            for issue in issues:
                issue_id = str(issue.get("id") or issue.get("identifier") or "").strip()
                if not issue_id or not self._should_reconcile_issue(issue):
                    continue
                chat_id = self._build_reconciled_issue_chat_id(issue_id)
                source = self.build_source(
                    chat_id=chat_id,
                    chat_name=issue.get("identifier") or issue.get("title") or chat_id,
                    chat_type="thread",
                    user_id=f"linear:reconcile:{app_user_id}",
                    user_name="Linear reconciler",
                )
                session_key = build_session_key(
                    source,
                    group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
                    thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
                )
                if session_key in self._active_sessions or session_key in self._pending_messages:
                    continue
                session = self._store_reconciled_issue_session(issue, str(app_user_id))
                self._reconciled_issue_leases[issue_id] = {
                    "updated_at": str(issue.get("updatedAt") or "").strip(),
                    "leased_at": time.time(),
                }
                source = self.build_source(
                    chat_id=chat_id,
                    chat_name=session.get("chat_name") or chat_id,
                    chat_type="thread",
                    user_id=f"linear:reconcile:{app_user_id}",
                    user_name="Linear reconciler",
                )
                event = MessageEvent(
                    text=self._build_reconciled_issue_prompt(issue, session),
                    message_type=MessageType.TEXT,
                    source=source,
                    raw_message={"action": "reconcile", "issue": issue},
                    message_id=f"{issue_id}:reconcile:{int(time.time() * 1000)}",
                    internal=True,
                )
                await self.handle_message(event)

    async def _reconcile_delegated_started_issues_loop(self) -> None:
        try:
            while True:
                await self._reconcile_delegated_started_issues_once()
                await asyncio.sleep(self._reconcile_interval_seconds)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        return web.json_response({"status": "ok", "platform": "linear"})

    async def _handle_authorize(self, request: "web.Request") -> "web.Response":
        state = secrets.token_urlsafe(32)
        states = self._load_json(self._states_path)
        now = time.time()
        states = {
            key: value for key, value in states.items()
            if isinstance(value, dict) and now - float(value.get("created_at", 0)) < STATE_TTL_SECONDS
        }
        states[state] = {"created_at": now}
        self._save_json(self._states_path, states)
        raise web.HTTPFound(self._build_authorize_url(state))

    async def _handle_callback(self, request: "web.Request") -> "web.Response":
        code = request.query.get("code", "")
        state = request.query.get("state", "")
        error = request.query.get("error", "")
        if error:
            return web.Response(status=400, text=f"Linear authorization failed: {error}\n")
        if not code or not state:
            return web.Response(status=400, text="Missing code/state query parameters.\n")

        states = self._load_json(self._states_path)
        state_entry = states.pop(state, None)
        self._save_json(self._states_path, states)
        if not state_entry:
            return web.Response(status=400, text="Invalid or expired OAuth state.\n")
        created_at = float((state_entry or {}).get("created_at") or 0)
        if not created_at or time.time() - created_at >= STATE_TTL_SECONDS:
            return web.Response(status=400, text="Invalid or expired OAuth state.\n")

        try:
            token_data = await self._exchange_code_for_token(code)
            viewer = await self._query_viewer(token_data["access_token"])
            app_user_id = str(viewer["id"])
            stored = self._load_json(self._tokens_path)
            token_data["app_user_id"] = app_user_id
            token_data["viewer_name"] = viewer.get("name") or "Linear App"
            token_data["stored_at"] = time.time()
            stored[app_user_id] = token_data
            self._save_json(self._tokens_path, stored)
        except Exception as exc:
            logger.error("[linear] OAuth callback failed: %s", exc)
            return web.Response(status=400, text=f"Linear OAuth setup failed: {exc}\n")

        return web.Response(
            text=(
                "Linear OAuth setup complete.\n\n"
                f"App user ID: {app_user_id}\n"
                f"Webhook URL: {self._public_url(self._webhook_path)}\n"
                "Enable Linear Agent Session events and point them at the webhook URL above.\n"
            )
        )

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        content_length = request.content_length or 0
        if content_length > self._max_body_bytes:
            return web.json_response({"error": "Payload too large"}, status=413)

        try:
            raw_body = await request.read()
        except Exception:
            return web.json_response({"error": "Failed to read body"}, status=400)

        if not self._validate_signature(raw_body, request.headers.get("Linear-Signature", "")):
            return web.json_response({"error": "Invalid signature"}, status=401)

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        action = str(payload.get("action") or "")
        if action not in {"created", "prompted"}:
            return web.json_response({"status": "ignored", "action": action or "unknown"}, status=200)

        agent_session = payload.get("agentSession") or {}
        agent_session_id = str(agent_session.get("id") or "")
        app_user_id = str(payload.get("appUserId") or agent_session.get("appUserId") or "")
        if not agent_session_id or not app_user_id:
            return web.json_response({"error": "Missing agent session/app user IDs"}, status=400)

        try:
            await self._ensure_access_token(app_user_id)
        except Exception as exc:
            logger.error("[linear] No usable OAuth token for app user %s: %s", app_user_id, exc)
            return web.json_response({"error": f"OAuth token unavailable: {exc}"}, status=503)

        chat_id = f"linear:{agent_session_id}"
        issue = agent_session.get("issue") or {}
        chat_name = issue.get("identifier") or issue.get("title") or agent_session.get("url") or chat_id
        session = self._store_session_metadata(payload)
        session["chat_name"] = chat_name

        try:
            await self._create_activity(
                app_user_id=app_user_id,
                agent_session_id=agent_session_id,
                activity_type="thought",
                body="Jax is looking into this…",
                ephemeral=True,
            )
        except Exception:
            logger.debug("[linear] Initial ephemeral acknowledgement failed", exc_info=True)

        prompt = self._build_prompt(payload)
        creator = agent_session.get("creator") or {}
        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type="thread",
            user_id=str(agent_session.get("creatorId") or f"linear:{app_user_id}"),
            user_name=str(creator.get("name") or "Linear user"),
        )
        message_id = str(payload.get("webhookId") or f"{agent_session_id}:{action}:{int(time.time() * 1000)}")
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=message_id,
        )

        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return web.json_response(
            {
                "status": "accepted",
                "platform": "linear",
                "action": action,
                "agent_session_id": agent_session_id,
            },
            status=202,
        )

    async def _process_message_background(self, event: MessageEvent, session_key: str) -> None:
        session = self._session_info.get(event.source.chat_id) or {}
        if session and not session.get("can_execute", True):
            reason = session.get("block_reason") or "Task is not executable automatically."
            assignee_id = self._determine_handoff_assignee(session)
            await self._transition_issue_for_session(
                session,
                self._blocked_state_name,
                assignee_id=assignee_id,
                comment=f"Jax cannot execute this issue automatically: {reason}",
            )
            await self._maybe_send_queue_activity(
                event.source.chat_id,
                f"Jax cannot execute this task automatically and moved it to {self._blocked_state_name} for Pablo. Reason: {reason}",
            )
            return

        if session:
            unresolved_dependencies = await self._list_unresolved_dependencies(session)
            if unresolved_dependencies:
                assignee_id = self._determine_handoff_assignee(session)
                block_message = self._build_dependency_block_message(unresolved_dependencies)
                session["unresolved_dependencies"] = unresolved_dependencies
                await self._transition_issue_for_session(
                    session,
                    self._blocked_state_name,
                    assignee_id=assignee_id,
                    comment=block_message,
                )
                await self._maybe_send_queue_activity(event.source.chat_id, block_message)
                return
            if self._kanban_post_start_comment and not session.get("kanban_start_comment_posted"):
                start_comment = self._build_kanban_start_comment(session)
                if start_comment:
                    await self._transition_issue_for_session(
                        session,
                        self._in_progress_state_name,
                        comment=start_comment,
                    )
                    session["kanban_start_comment_posted"] = True

        queue_notice_needed = False

        async with self._session_counter_lock:
            if self._running_session_count >= self._max_concurrent_sessions:
                self._queued_session_count += 1
                queue_notice_needed = True

        if queue_notice_needed:
            await self._maybe_send_queue_activity(
                event.source.chat_id,
                f"Jax queued this session and will pick it up once one of the {self._max_concurrent_sessions} active slots frees up.",
            )

        acquired = False
        counted_running = False
        queued_count_decremented = not queue_notice_needed
        try:
            await self._session_semaphore.acquire()
            acquired = True

            async with self._session_counter_lock:
                if queue_notice_needed and self._queued_session_count > 0:
                    self._queued_session_count -= 1
                    queued_count_decremented = True
                self._running_session_count += 1
                counted_running = True

            if queue_notice_needed:
                await self._maybe_send_queue_activity(
                    event.source.chat_id,
                    "Jax is starting work on this session now.",
                )

            await super()._process_message_background(event, session_key)
        finally:
            async with self._session_counter_lock:
                if not queued_count_decremented and self._queued_session_count > 0:
                    self._queued_session_count -= 1
                if counted_running and self._running_session_count > 0:
                    self._running_session_count -= 1
            if acquired:
                self._session_semaphore.release()

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def _build_prompt(self, payload: Dict[str, Any]) -> str:
        action = str(payload.get("action") or "")
        agent_session = payload.get("agentSession") or {}
        issue = agent_session.get("issue") or {}
        session_url = agent_session.get("url") or ""
        issue_identifier = issue.get("identifier") or ""
        issue_title = issue.get("title") or ""
        guidance = payload.get("guidance") or []
        guidance_text = json.dumps(guidance, indent=2)[:3000] if guidance else "[]"
        session = self._session_info.get(f"linear:{agent_session.get('id') or ''}", {})
        flow_context = (
            f"Task type: {session.get('task_type', DEFAULT_TASK_TYPE)}\n"
            f"Project execution mode: {session.get('execution_mode', self._default_execution_mode)}\n"
            f"Auto-executable by current Jax executor: {'yes' if session.get('can_execute', True) else 'no'}\n"
        )
        execution_mode = str(session.get("execution_mode") or self._default_execution_mode)
        allowed_decisions = ["done", "backlog", "stay_in_progress", "change_scope", "needs_human_review"]
        if execution_mode != "human_gate":
            allowed_decisions.insert(1, "ready_for_testing")
        example_decision = "done" if execution_mode != "human_gate" else "needs_human_review"
        orchestration_guidance = self._build_kanban_guidance_block(session) if execution_mode == "kanban_orchestrated" else ""

        if action == "created":
            prompt_context = payload.get("promptContext") or ""
            return (
                "A new Linear Agent Session was created for you. "
                "Reply as Jax in the Linear session.\n\n"
                f"Session URL: {session_url or '(unknown)'}\n"
                f"Issue: {issue_identifier} {issue_title}\n"
                f"{flow_context}"
                f"Guidance:\n```json\n{guidance_text}\n```\n\n"
                "Use the following Linear-provided promptContext as the authoritative context:\n\n"
                f"```text\n{prompt_context[:12000]}\n```\n\n"
                f"{orchestration_guidance}"
                "Testing semantics are strict: choose `ready_for_testing` only when actual testing is still pending. If manual testing is required, your visible response must include a concrete QA handoff covering what changed, where to test, exact steps, expected result, what you already validated, and remaining risk. If you already validated the slice sufficiently and no real testing action remains, choose `done` instead of `ready_for_testing`.\n\n"
                "At the end of your visible response, append a machine-readable workflow block so Jax can route the issue correctly.\n"
                "Format exactly like:\n"
                "```hermes_workflow\n"
                f'{{"decision": "{example_decision}", "reason": "brief rationale"}}\n'
                "```\n"
                f"Allowed decisions: {', '.join(allowed_decisions)}."
            )

        activity = payload.get("agentActivity") or {}
        content = activity.get("content") or {}
        body = content.get("body") or json.dumps(content, indent=2)[:4000]
        signal = activity.get("signal")
        return (
            "A user added a follow-up prompt to an existing Linear Agent Session.\n\n"
            f"Session URL: {session_url or '(unknown)'}\n"
            f"Issue: {issue_identifier} {issue_title}\n"
            f"Signal: {signal or '(none)'}\n"
            f"User message:\n\n{body}\n\n"
            f"{orchestration_guidance}"
            "Testing semantics are strict: choose `ready_for_testing` only when actual testing is still pending. If manual testing is required, your visible response must include a concrete QA handoff covering what changed, where to test, exact steps, expected result, what you already validated, and remaining risk. If you already validated the slice sufficiently and no real testing action remains, choose `done` instead of `ready_for_testing`.\n\n"
            "At the end of your visible response, append a machine-readable workflow block so Jax can route the issue correctly.\n"
            "Format exactly like:\n"
            "```hermes_workflow\n"
            f'{{"decision": "{example_decision}", "reason": "brief rationale"}}\n'
            "```\n"
            f"Allowed decisions: {', '.join(allowed_decisions)}."
        )

    # ------------------------------------------------------------------
    # Linear API helpers
    # ------------------------------------------------------------------

    def _validate_signature(self, body: bytes, signature: str) -> bool:
        if not signature:
            return False
        expected = hmac.new(self._webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    def _build_authorize_url(self, state: str) -> str:
        params = urllib.parse.urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._public_url(self._callback_path),
                "response_type": "code",
                "scope": ",".join(self._scopes),
                "state": state,
                "actor": "app",
                "prompt": "consent",
            }
        )
        return f"https://linear.app/oauth/authorize?{params}"

    def _public_url(self, path: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url}{path}"
        host = self._host
        display_host = "127.0.0.1" if host == "0.0.0.0" else host
        return f"http://{display_host}:{self._port}{path}"

    async def _exchange_code_for_token(self, code: str) -> Dict[str, Any]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._public_url(self._callback_path),
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        token_data = await asyncio.to_thread(
            self._http_form,
            "https://api.linear.app/oauth/token",
            form,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_data["obtained_at"] = time.time()
        expires_in = int(token_data.get("expires_in") or 0)
        if expires_in > 0:
            token_data["expires_at"] = token_data["obtained_at"] + expires_in
        return token_data

    async def _refresh_token(self, app_user_id: str, refresh_token: str) -> Dict[str, Any]:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        token_data = await asyncio.to_thread(
            self._http_form,
            "https://api.linear.app/oauth/token",
            form,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        stored = self._load_json(self._tokens_path)
        existing = stored.get(app_user_id, {})
        token_data["obtained_at"] = time.time()
        expires_in = int(token_data.get("expires_in") or 0)
        if expires_in > 0:
            token_data["expires_at"] = token_data["obtained_at"] + expires_in
        if not token_data.get("refresh_token"):
            token_data["refresh_token"] = refresh_token
        token_data["app_user_id"] = app_user_id
        token_data["viewer_name"] = existing.get("viewer_name") or existing.get("name")
        token_data["stored_at"] = time.time()
        stored[app_user_id] = token_data
        self._save_json(self._tokens_path, stored)
        return token_data

    async def _ensure_access_token(self, app_user_id: str) -> str:
        stored = self._load_json(self._tokens_path)
        token = stored.get(app_user_id)
        if not token:
            raise RuntimeError(f"No stored OAuth token for app user {app_user_id}")
        access_token = str(token.get("access_token") or "")
        expires_at = float(token.get("expires_at") or 0)
        refresh_token = str(token.get("refresh_token") or "")
        if access_token and (not expires_at or expires_at - time.time() > TOKEN_REFRESH_SKEW_SECONDS):
            return access_token
        if not refresh_token:
            if access_token:
                return access_token
            raise RuntimeError(f"Stored token for {app_user_id} has expired and no refresh token is available")
        try:
            refreshed = await self._refresh_token(app_user_id, refresh_token)
        except RuntimeError as exc:
            detail = str(exc).lower()
            if "invalid_grant" in detail or "refresh token revoked" in detail or "revoked" in detail:
                stored.pop(app_user_id, None)
                self._save_json(self._tokens_path, stored)
                raise RuntimeError(
                    f"Stored OAuth token for app user {app_user_id} was revoked; re-authorize the Linear app"
                ) from exc
            raise
        return str(refreshed.get("access_token") or "")

    async def _query_viewer(self, access_token: str) -> Dict[str, Any]:
        payload = {
            "query": "query { viewer { id name } }",
        }
        result = await asyncio.to_thread(
            self._http_json,
            "https://api.linear.app/graphql",
            payload,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        viewer = ((result.get("data") or {}).get("viewer") or {})
        if not viewer.get("id"):
            raise RuntimeError(f"Viewer query returned no app user ID: {result}")
        return viewer

    async def _create_activity(
        self,
        *,
        app_user_id: str,
        agent_session_id: str,
        activity_type: str,
        body: str,
        ephemeral: bool = False,
        signal: Optional[str] = None,
    ) -> Dict[str, Any]:
        access_token = await self._ensure_access_token(app_user_id)
        content = {
            "type": activity_type,
            "body": body,
        }
        payload = {
            "query": (
                "mutation($input: AgentActivityCreateInput!) { "
                "agentActivityCreate(input: $input) { success agentActivity { id } } }"
            ),
            "variables": {
                "input": {
                    "agentSessionId": agent_session_id,
                    "content": content,
                    "ephemeral": ephemeral,
                }
            },
        }
        if signal:
            payload["variables"]["input"]["signal"] = signal
        result = await asyncio.to_thread(
            self._http_json,
            "https://api.linear.app/graphql",
            payload,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        errors = result.get("errors") or []
        if errors:
            raise RuntimeError(errors[0].get("message") or str(errors[0]))
        create_payload = ((result.get("data") or {}).get("agentActivityCreate") or {})
        if not create_payload.get("success"):
            raise RuntimeError(f"agentActivityCreate failed: {result}")
        return (result.get("data") or {})

    async def _maybe_send_queue_activity(self, chat_id: str, body: str) -> None:
        session = self._session_info.get(chat_id)
        if not session:
            return
        try:
            await self._create_activity(
                app_user_id=session["app_user_id"],
                agent_session_id=session["agent_session_id"],
                activity_type="thought",
                body=body,
                ephemeral=True,
            )
        except Exception:
            logger.debug("[linear] Queue status activity failed for %s", chat_id, exc_info=True)

    async def _schedule_autonomous_rerun(
        self,
        event: MessageEvent,
        session: Dict[str, Any],
        *,
        rerun_kind: str,
        reason: str,
    ) -> None:
        if rerun_kind == "retry":
            prompt = (
                "Retry the current issue autonomously. "
                f"Previous workflow reason: {reason}. "
                "Re-use the current context and continue from the last attempt."
            )
        elif rerun_kind == "continue":
            prompt = (
                "Continue the current issue autonomously from the last completed slice. "
                f"Previous workflow reason: {reason}. "
                "Do not restart from scratch; use the current context and execute the next concrete step."
            )
        else:
            prompt = (
                "Refit the current issue into a smaller executable slice and continue autonomously. "
                f"Previous workflow reason: {reason}. "
                "Rewrite the immediate objective into the next narrow step before continuing."
            )
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        if session_key in self._pending_messages:
            logger.debug("[linear] Skipping autonomous rerun for %s because a pending follow-up already exists", session_key)
            return
        rerun_event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=event.source,
            raw_message={
                "autonomous_rerun": True,
                "rerun_kind": rerun_kind,
                "reason": reason,
                "issue_identifier": session.get("issue_identifier"),
            },
            message_id=f"{event.message_id or session.get('issue_identifier') or 'linear'}:rerun:{rerun_kind}:{int(time.time() * 1000)}",
            internal=True,
        )
        task = asyncio.create_task(self.handle_message(rerun_event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def on_processing_start(self, event: MessageEvent) -> None:
        session = self._session_info.get(event.source.chat_id)
        if not session or not session.get("can_execute", True):
            return
        await self._transition_issue_for_session(
            session,
            self._in_progress_state_name,
            assignee_id=session.get("current_assignee_id"),
            comment=(
                "Jax started working on this issue automatically "
                f"(task type: {session.get('task_type', DEFAULT_TASK_TYPE)}, "
                f"mode: {session.get('execution_mode', self._default_execution_mode)})."
            ),
        )

    async def on_processing_complete(self, event: MessageEvent, outcome) -> None:
        session = self._session_info.get(event.source.chat_id)
        if not session or not session.get("can_execute", True):
            return

        assignee_id = session.get("current_assignee_id")
        execution_mode = session.get("execution_mode", self._default_execution_mode)
        if outcome.name == "SUCCESS":
            session["retry_requested"] = False
            session["retry_last_error_class"] = None
            workflow_decision = self._resolve_success_workflow_decision(session)
            session["workflow_decision"] = workflow_decision
            if workflow_decision not in {"stay_in_progress", "change_scope"}:
                session["success_rerun_count"] = 0
            if workflow_decision == "done":
                await self._transition_issue_for_session(
                    session,
                    self._done_state_name,
                    assignee_id=assignee_id,
                    comment="Jax finished implementation work and marked this issue Done automatically.",
                )
                return

            if workflow_decision == "needs_human_review":
                await self._transition_issue_for_session(
                    session,
                    self._in_review_state_name,
                    assignee_id=assignee_id,
                    comment="Jax needs human review before continuing this issue.",
                )
                return

            if workflow_decision == "backlog":
                await self._transition_issue_for_session(
                    session,
                    self._backlog_state_name,
                    assignee_id=assignee_id,
                    comment="Jax decided this issue should return to Backlog before more autonomous execution.",
                )
                return

            if workflow_decision == "stay_in_progress":
                rerun_budget = self._classify_success_rerun_budget(session)
                if not rerun_budget["allowed"]:
                    session["retrigger_requested"] = False
                    await self._transition_issue_for_session(
                        session,
                        self._blocked_state_name,
                        assignee_id=assignee_id,
                        comment="Jax exhausted autonomous continuation attempts for this issue and moved it to Blocked for follow-up.",
                    )
                    return
                session["success_rerun_count"] = rerun_budget["next_count"]
                session["retrigger_requested"] = True
                reason = str(session.get("workflow_decision_reason") or "partial_progress").strip() or "partial_progress"
                await self._transition_issue_for_session(
                    session,
                    self._in_progress_state_name,
                    assignee_id=assignee_id,
                    comment="Jax made partial progress and is keeping this issue in In Progress for continued autonomous execution.",
                )
                await self._schedule_autonomous_rerun(
                    event,
                    session,
                    rerun_kind="continue",
                    reason=f"workflow_decision:stay_in_progress:{reason}",
                )
                return

            if workflow_decision == "change_scope":
                rerun_budget = self._classify_success_rerun_budget(session)
                if not rerun_budget["allowed"]:
                    session["retrigger_requested"] = False
                    await self._transition_issue_for_session(
                        session,
                        self._blocked_state_name,
                        assignee_id=assignee_id,
                        comment="Jax exhausted autonomous continuation attempts for this issue and moved it to Blocked for follow-up.",
                    )
                    return
                session["success_rerun_count"] = rerun_budget["next_count"]
                session["retrigger_requested"] = True
                followup = None
                comment = "Jax narrowed the active work on this issue into a smaller executable slice and will continue autonomously."
                try:
                    followup = await self._ensure_scope_followup_issue(session)
                except Exception:
                    logger.debug("[linear] Failed to create change-scope follow-up issue for %s", session.get("issue_identifier"), exc_info=True)
                    comment += " Jax could not create the deferred-scope follow-up issue automatically."
                if followup and followup.get("identifier"):
                    comment += f" Created follow-up issue {followup['identifier']} to track the deferred remaining scope."
                await self._transition_issue_for_session(
                    session,
                    self._in_progress_state_name,
                    assignee_id=assignee_id,
                    comment=comment,
                )
                await self._schedule_autonomous_rerun(
                    event,
                    session,
                    rerun_kind="refit",
                    reason="workflow_decision:change_scope",
                )
                return

            target_state = self._testing_state_name
            comment = (
                "Jax finished implementation work and moved this issue to "
                f"{self._testing_state_name} (task type: {session.get('task_type', DEFAULT_TASK_TYPE)}, "
                f"mode: {execution_mode})."
            )
            if not await self._state_name_exists(session.get("team_id"), self._testing_state_name, session.get("app_user_id")):
                target_state = self._testing_fallback_state_name
                comment = (
                    "Jax finished implementation work and moved this issue to "
                    f"{self._testing_fallback_state_name} because the team has no {self._testing_state_name} state configured."
                )
            await self._transition_issue_for_session(
                session,
                target_state,
                assignee_id=assignee_id,
                comment=comment,
            )
            return

        retry_decision = self._classify_retry_decision(session)
        session["retry_last_error_class"] = retry_decision["error_class"]
        if retry_decision["should_retry"]:
            session["retry_attempt_count"] = retry_decision["next_attempt"]
            session["retry_requested"] = True
            await self._transition_issue_for_session(
                session,
                self._in_progress_state_name,
                assignee_id=assignee_id,
                comment=(
                    "Jax hit a "
                    f"{retry_decision['error_class']} failure and left this issue in {self._in_progress_state_name} "
                    f"for retry (attempt {retry_decision['next_attempt']}/{retry_decision['max_attempts']})."
                ),
            )
            await self._schedule_autonomous_rerun(
                event,
                session,
                rerun_kind="retry",
                reason=retry_decision["error_class"],
            )
            return

        session["retry_requested"] = False
        refit_decision = self._classify_refit_decision(session)
        if refit_decision["should_refit"]:
            session["refit_attempt_count"] = refit_decision["next_attempt"]
            session["workflow_decision"] = "change_scope"
            session["retrigger_requested"] = True
            await self._transition_issue_for_session(
                session,
                self._in_progress_state_name,
                assignee_id=assignee_id,
                comment=(
                    "Jax hit an "
                    f"{refit_decision['error_class']} failure, narrowed the scope into a smaller executable slice, "
                    f"and left this issue in {self._in_progress_state_name} for an autonomous rerun "
                    f"(refit {refit_decision['next_attempt']}/{refit_decision['max_refits']})."
                ),
            )
            await self._schedule_autonomous_rerun(
                event,
                session,
                rerun_kind="refit",
                reason=refit_decision["error_class"],
            )
            return
        session["retrigger_requested"] = False
        if refit_decision["refittable"] and refit_decision["max_refits"]:
            comment = (
                "Jax could not retrofit this issue into a smaller executable slice after an "
                f"{refit_decision['error_class']} failure and moved it to Blocked for follow-up."
            )
        elif retry_decision["retryable"] and retry_decision["max_attempts"]:
            comment = (
                "Jax exhausted retry attempts after a "
                f"{retry_decision['error_class']} failure and moved this issue to Blocked for follow-up."
            )
        else:
            comment = "Jax could not finish this issue and moved it to Blocked for follow-up."
        await self._transition_issue_for_session(
            session,
            self._blocked_state_name,
            assignee_id=assignee_id,
            comment=comment,
        )

    async def _transition_issue_for_session(
        self,
        session: Dict[str, Any],
        target_state: str,
        *,
        assignee_id: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> None:
        issue_id = str(session.get("issue_id") or session.get("issue_identifier") or "")
        team_id = str(session.get("team_id") or "")
        app_user_id = str(session.get("app_user_id") or "")
        if issue_id and team_id and target_state:
            await self._update_issue_state(issue_id, team_id, app_user_id, target_state, assignee_id=assignee_id)
        if comment:
            try:
                if session.get("agent_session_id"):
                    await self._create_activity(
                        app_user_id=app_user_id,
                        agent_session_id=str(session.get("agent_session_id") or ""),
                        activity_type="thought",
                        body=comment,
                        ephemeral=False,
                    )
                elif issue_id:
                    await self._create_issue_comment(
                        app_user_id=app_user_id,
                        issue_id=issue_id,
                        body=comment,
                    )
            except Exception:
                logger.debug("[linear] Issue transition activity failed for %s", issue_id, exc_info=True)

    async def _state_name_exists(self, team_id: str, state_name: str, app_user_id: Optional[str] = None) -> bool:
        if not team_id or not app_user_id:
            return False
        states = await self._list_issue_states(team_id, app_user_id)
        target = state_name.strip().lower()
        return any(str(state.get("name") or "").strip().lower() == target for state in states)

    async def _list_issue_states(self, team_id: str, app_user_id: str) -> List[Dict[str, Any]]:
        if not team_id:
            return []
        if team_id in self._issue_state_cache:
            return self._issue_state_cache[team_id]
        access_token = await self._ensure_access_token(app_user_id)
        payload = {
            "query": (
                "query { workflowStates(filter: { team: { id: { eq: \""
                + team_id
                + "\" } } }) { nodes { id name type } } }"
            )
        }
        result = await asyncio.to_thread(
            self._http_json,
            "https://api.linear.app/graphql",
            payload,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        states = ((result.get("data") or {}).get("workflowStates") or {}).get("nodes") or []
        self._issue_state_cache[team_id] = states
        return states

    async def _update_issue_state(
        self,
        issue_id: str,
        team_id: str,
        app_user_id: str,
        state_name: str,
        *,
        assignee_id: Optional[str] = None,
    ) -> None:
        states = await self._list_issue_states(team_id, app_user_id)
        target_state = next(
            (state for state in states if str(state.get("name") or "").strip().lower() == state_name.strip().lower()),
            None,
        )
        if not target_state:
            raise RuntimeError(f"Linear state '{state_name}' not found for team {team_id}")
        access_token = await self._ensure_access_token(app_user_id)
        input_parts = [f'stateId: "{target_state["id"]}"']
        if assignee_id:
            input_parts.append(f'assigneeId: "{assignee_id}"')
        payload = {
            "query": (
                "mutation { issueUpdate(id: \""
                + issue_id
                + "\", input: { "
                + ", ".join(input_parts)
                + " }) { success issue { id identifier state { name type } assignee { id name } } } }"
            )
        }
        result = await asyncio.to_thread(
            self._http_json,
            "https://api.linear.app/graphql",
            payload,
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        errors = result.get("errors") or []
        if errors:
            raise RuntimeError(errors[0].get("message") or str(errors[0]))
        update_payload = ((result.get("data") or {}).get("issueUpdate") or {})
        if not update_payload.get("success"):
            raise RuntimeError(f"issueUpdate failed: {result}")

    async def _revoke_token(self, access_token: str) -> None:
        if not access_token:
            return
        await asyncio.to_thread(
            self._http_form,
            "https://api.linear.app/oauth/revoke",
            {},
            {"Authorization": f"Bearer {access_token}"},
        )

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_json(self, path: Path) -> Dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("[linear] Failed to load JSON store %s", path)
            return {}

    def _save_json(self, path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # Blocking HTTP helpers (run via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _http_json(self, url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {payload}") from exc
        return json.loads(data) if data else {}

    def _http_form(self, url: str, form: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        body = urllib.parse.urlencode({k: str(v) for k, v in form.items()}).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {payload}") from exc
        return json.loads(data) if data else {}
