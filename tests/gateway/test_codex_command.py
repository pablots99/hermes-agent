"""Tests for gateway /codex command."""

import threading
from unittest.mock import MagicMock, patch

import pytest


SK = "agent:main:discord:thread:123"


class TestCodexCommand:
    @pytest.mark.asyncio
    async def test_handle_codex_command_returns_tracker_output(self):
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner._running_agents = {}
        runner._running_agents_ts = {}
        runner._agent_cache = {}
        runner._agent_cache_lock = threading.Lock()

        event = MagicMock()
        event.get_command_args.return_value = "--source discord"

        fake_tracker = MagicMock()
        fake_tracker.generate.return_value = {"empty": False}
        fake_tracker.format_gateway.return_value = "codex usage summary"

        with patch("hermes_state.SessionDB") as mock_db_cls, \
             patch("agent.codex_usage.CodexUsageTracker", return_value=fake_tracker):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db

            result = await runner._handle_codex_command(event)

        assert result == "codex usage summary"
        fake_tracker.generate.assert_called_once_with(source="discord")
        fake_tracker.format_gateway.assert_called_once()
        mock_db.close.assert_called_once()
