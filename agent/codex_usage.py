"""Codex subscription-style usage tracking over rolling windows.

Summarizes Hermes session history for Codex usage so users can monitor rolling
windows similar to plan limits (5h / 7d / 30d).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agent.usage_pricing import format_duration_compact


_WINDOW_DEFS = (
    ("5h", "Last 5h", 5 * 3600),
    ("7d", "Last 7d", 7 * 86400),
    ("30d", "Last 30d", 30 * 86400),
)

_CODEX_SESSION_WHERE = """
(
    COALESCE(s.billing_provider, '') = 'openai-codex'
    OR LOWER(COALESCE(s.model, '')) LIKE '%codex%'
)
"""


class CodexUsageTracker:
    """Query SessionDB for Codex usage over rolling windows."""

    def __init__(self, db):
        self.db = db
        self._conn = db._conn

    def generate(self, source: str | None = None) -> Dict[str, Any]:
        windows = [self._window_stats(seconds=seconds, key=key, label=label, source=source)
                   for key, label, seconds in _WINDOW_DEFS]
        models = self._model_breakdown(seconds=_WINDOW_DEFS[-1][2], source=source)
        lifetime = self._window_stats(seconds=None, key="all", label="All time", source=source)
        last_activity_at = lifetime.get("last_activity_at")

        live_rate_limits = self._load_live_rate_limits()
        has_any = any(window["session_count"] > 0 for window in windows)
        if not has_any:
            return {
                "empty": True,
                "generated_at": time.time(),
                "source_filter": source,
                "windows": [],
                "models": [],
                "last_activity_at": None,
                "live_rate_limits": live_rate_limits,
            }

        return {
            "empty": False,
            "generated_at": time.time(),
            "source_filter": source,
            "windows": windows,
            "models": models,
            "last_activity_at": last_activity_at,
            "live_rate_limits": live_rate_limits,
        }

    def _window_stats(self, *, seconds: int | None, key: str, label: str, source: str | None) -> Dict[str, Any]:
        params: List[Any] = []
        session_where = [_CODEX_SESSION_WHERE]
        message_where = ["m.role = 'assistant'", _CODEX_SESSION_WHERE]

        if seconds is not None:
            cutoff = time.time() - seconds
            session_where.append("s.started_at >= ?")
            message_where.append("m.timestamp >= ?")
            params.append(cutoff)
            message_params: List[Any] = [cutoff]
        else:
            message_params = []

        if source:
            session_where.append("s.source = ?")
            message_where.append("s.source = ?")
            params.append(source)
            message_params.append(source)

        session_sql = f"""
            SELECT
                COUNT(*) AS session_count,
                COALESCE(SUM(s.input_tokens), 0) AS input_tokens,
                COALESCE(SUM(s.output_tokens), 0) AS output_tokens,
                COALESCE(SUM(s.cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(s.cache_write_tokens), 0) AS cache_write_tokens,
                COALESCE(SUM(CASE
                    WHEN s.ended_at IS NOT NULL AND s.ended_at > s.started_at
                    THEN s.ended_at - s.started_at
                    ELSE 0
                END), 0) AS active_seconds,
                MAX(COALESCE(s.ended_at, s.started_at)) AS last_activity_at
            FROM sessions s
            WHERE {' AND '.join(session_where)}
        """
        row = self._conn.execute(session_sql, tuple(params)).fetchone()

        message_sql = f"""
            SELECT COUNT(*) AS api_calls
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            WHERE {' AND '.join(message_where)}
        """
        msg_row = self._conn.execute(message_sql, tuple(message_params)).fetchone()

        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        cache_read_tokens = int(row["cache_read_tokens"] or 0)
        cache_write_tokens = int(row["cache_write_tokens"] or 0)
        total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
        active_seconds = float(row["active_seconds"] or 0)

        return {
            "key": key,
            "label": label,
            "window_seconds": seconds,
            "session_count": int(row["session_count"] or 0),
            "api_calls": int(msg_row["api_calls"] or 0),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "total_tokens": total_tokens,
            "active_seconds": active_seconds,
            "last_activity_at": row["last_activity_at"],
        }

    def _model_breakdown(self, *, seconds: int, source: str | None) -> List[Dict[str, Any]]:
        cutoff = time.time() - seconds
        params: List[Any] = [cutoff]
        where = [_CODEX_SESSION_WHERE, "s.started_at >= ?"]
        if source:
            where.append("s.source = ?")
            params.append(source)

        sql = f"""
            SELECT
                COALESCE(s.model, '(unknown)') AS model,
                COUNT(*) AS session_count,
                COALESCE(SUM(s.input_tokens + s.output_tokens + s.cache_read_tokens + s.cache_write_tokens), 0) AS total_tokens,
                MAX(COALESCE(s.ended_at, s.started_at)) AS last_activity_at
            FROM sessions s
            WHERE {' AND '.join(where)}
            GROUP BY COALESCE(s.model, '(unknown)')
            ORDER BY total_tokens DESC, session_count DESC, model ASC
        """
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "model": row["model"],
                "session_count": int(row["session_count"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "last_activity_at": row["last_activity_at"],
            }
            for row in rows
        ]

    def _load_live_rate_limits(self) -> Dict[str, Any] | None:
        codex_home = os.getenv("CODEX_HOME", "").strip() or str(Path.home() / ".codex")
        sessions_root = Path(codex_home).expanduser() / "sessions"
        if not sessions_root.exists():
            return None

        latest: Dict[str, Any] | None = None
        latest_ts: str = ""
        latest_path: str = ""
        for path in sorted(sessions_root.glob("**/*.jsonl"), reverse=True):
            try:
                with path.open(encoding="utf-8") as handle:
                    for raw_line in handle:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = item.get("payload") or {}
                        rate_limits = payload.get("rate_limits") or {}
                        if payload.get("type") != "token_count" or rate_limits.get("limit_id") != "codex":
                            continue
                        ts = str(item.get("timestamp") or "")
                        if ts >= latest_ts:
                            latest_ts = ts
                            latest_path = str(path)
                            latest = self._normalize_live_rate_limits(rate_limits, source=latest_path, timestamp=ts)
            except OSError:
                continue

        return latest

    def _normalize_live_rate_limits(self, rate_limits: Dict[str, Any], *, source: str, timestamp: str) -> Dict[str, Any]:
        result = {
            "source": source,
            "captured_at": timestamp,
            "plan_type": rate_limits.get("plan_type"),
            "primary": self._normalize_limit_bucket(rate_limits.get("primary")),
            "secondary": self._normalize_limit_bucket(rate_limits.get("secondary")),
            "credits": rate_limits.get("credits"),
        }
        return result

    def _normalize_limit_bucket(self, bucket: Dict[str, Any] | None) -> Dict[str, Any] | None:
        if not bucket:
            return None
        used = float(bucket.get("used_percent") or 0.0)
        return {
            "used_percent": used,
            "remaining_percent": max(0.0, 100.0 - used),
            "window_minutes": bucket.get("window_minutes"),
            "resets_at": bucket.get("resets_at"),
        }

    def _format_live_limit_line(self, label: str, bucket: Dict[str, Any] | None) -> str | None:
        if not bucket:
            return None
        reset_text = "unknown reset"
        if bucket.get("resets_at"):
            reset_text = datetime.fromtimestamp(bucket["resets_at"]).strftime("%Y-%m-%d %H:%M")
        return (
            f"{label}: {bucket['remaining_percent']:.1f}% left "
            f"({bucket['used_percent']:.1f}% used, reset {reset_text})"
        )

    def format_terminal(self, report: Dict[str, Any]) -> str:
        if report.get("empty"):
            return "  No Codex usage found in Hermes history yet."

        lines = [
            "",
            "  ╔══════════════════════════════════════════════════════════╗",
            "  ║                     🤖 Codex Usage                      ║",
            "  ╚══════════════════════════════════════════════════════════╝",
            "",
            "  Rolling subscription-style usage windows from Hermes session history.",
            "",
        ]
        if report.get("source_filter"):
            lines.append(f"  Source filter: {report['source_filter']}")
            lines.append("")

        live_rate_limits = report.get("live_rate_limits") or {}
        primary_line = self._format_live_limit_line("5h", live_rate_limits.get("primary"))
        secondary_line = self._format_live_limit_line("7d", live_rate_limits.get("secondary"))
        if primary_line or secondary_line:
            lines.append("  Live limits")
            lines.append("  " + "─" * 56)
            if live_rate_limits.get("plan_type"):
                lines.append(f"  Plan: {live_rate_limits['plan_type']}")
            if primary_line:
                lines.append(f"  {primary_line}")
            if secondary_line:
                lines.append(f"  {secondary_line}")
            lines.append("")

        for window in report["windows"]:
            lines.append(f"  {window['label']}")
            lines.append("  " + "─" * 56)
            lines.append(
                f"  Sessions: {window['session_count']:<8}  Assistant turns: {window['api_calls']:<8}  Total tokens: {window['total_tokens']:,}"
            )
            lines.append(
                f"  Input:    {window['input_tokens']:<8,}  Output:          {window['output_tokens']:<8,}  Active: ~{format_duration_compact(window['active_seconds'])}"
            )
            cache_total = window["cache_read_tokens"] + window["cache_write_tokens"]
            if cache_total:
                lines.append(
                    f"  Cache:    {cache_total:<8,}  (read {window['cache_read_tokens']:,} / write {window['cache_write_tokens']:,})"
                )
            lines.append("")

        if report.get("models"):
            lines.append("  Top Codex models (30d)")
            lines.append("  " + "─" * 56)
            for model in report["models"][:5]:
                lines.append(
                    f"  {model['model'][:32]:<32} {model['session_count']:>4} sessions  {model['total_tokens']:>12,} tokens"
                )
            lines.append("")

        if report.get("last_activity_at"):
            ts = datetime.fromtimestamp(report["last_activity_at"]).strftime("%b %d, %Y %H:%M")
            lines.append(f"  Last Codex activity: {ts}")

        return "\n".join(lines)

    def format_gateway(self, report: Dict[str, Any]) -> str:
        if report.get("empty"):
            return "No Codex usage found in Hermes history yet."

        lines = [
            "🤖 **Codex Usage**",
            "Rolling subscription-style windows from Hermes session history.",
        ]
        if report.get("source_filter"):
            lines.append(f"Source: `{report['source_filter']}`")
        lines.append("")

        live_rate_limits = report.get("live_rate_limits") or {}
        primary_line = self._format_live_limit_line("5h", live_rate_limits.get("primary"))
        secondary_line = self._format_live_limit_line("7d", live_rate_limits.get("secondary"))
        if primary_line or secondary_line:
            lines.append("**Live limits:**")
            if live_rate_limits.get("plan_type"):
                lines.append(f"Plan: `{live_rate_limits['plan_type']}`")
            if primary_line:
                lines.append(f"- {primary_line}")
            if secondary_line:
                lines.append(f"- {secondary_line}")
            lines.append("")

        for window in report["windows"]:
            lines.append(
                f"**{window['label']}** — {window['session_count']} sessions, {window['api_calls']} assistant turns, {window['total_tokens']:,} tokens, ~{format_duration_compact(window['active_seconds'])} active"
            )

        if report.get("models"):
            lines.append("")
            lines.append("**Top models (30d):**")
            for model in report["models"][:5]:
                lines.append(
                    f"  {model['model']} — {model['session_count']} sessions, {model['total_tokens']:,} tokens"
                )

        if report.get("last_activity_at"):
            ts = datetime.fromtimestamp(report["last_activity_at"]).strftime("%Y-%m-%d %H:%M")
            lines.append("")
            lines.append(f"Last Codex activity: {ts}")

        return "\n".join(lines)
