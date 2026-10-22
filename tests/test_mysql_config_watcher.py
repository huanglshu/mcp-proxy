"""Tests for polling MySQL-backed named server configuration."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from mcp.client.stdio import StdioServerParameters

from mcp_proxy.config_loader import MySQLNamedServerConfig, MySQLNamedServerSnapshot
from mcp_proxy.mysql_config_watcher import MySQLNamedServerConfigWatcher


async def test_unchanged_poll_does_not_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful poll with no service changes stays quiet, even at DEBUG."""
    snapshot = MySQLNamedServerSnapshot(
        servers={
            "visible-name": MySQLNamedServerConfig(
                enabled=True,
                version="v1",
                params=StdioServerParameters(
                    command="secret-command",
                    args=["secret-argument"],
                    env={"TOKEN": "secret-token"},
                ),
            ),
        },
    )
    reconcile = AsyncMock()
    watcher = MySQLNamedServerConfigWatcher("config.json", {}, reconcile)

    with (
        caplog.at_level("DEBUG", logger="mcp_proxy.mysql_config_watcher"),
        patch(
            "mcp_proxy.mysql_config_watcher.load_named_server_config_snapshot_from_mysql",
            return_value=snapshot,
        ) as load_snapshot,
    ):
        result = await watcher.poll_once()

    assert result is True
    load_snapshot.assert_called_once_with("config.json", {}, log_details=False)
    reconcile.assert_awaited_once_with(snapshot)
    assert caplog.text == ""
    assert "secret-command" not in caplog.text
    assert "secret-argument" not in caplog.text
    assert "secret-token" not in caplog.text


async def test_poll_once_keeps_current_state_on_database_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed database read does not reconcile an empty or partial snapshot."""
    reconcile = AsyncMock()
    watcher = MySQLNamedServerConfigWatcher("config.json", {}, reconcile)

    with (
        caplog.at_level("INFO", logger="mcp_proxy.mysql_config_watcher"),
        patch(
            "mcp_proxy.mysql_config_watcher.load_named_server_config_snapshot_from_mysql",
            side_effect=RuntimeError("database unavailable"),
        ),
    ):
        result = await watcher.poll_once()

    assert result is False
    reconcile.assert_not_awaited()
    assert "poll failed (RuntimeError)" in caplog.text
    assert "database unavailable" not in caplog.text


async def test_poll_once_contains_reconcile_failure() -> None:
    """A reconcile failure is reported to the poller without escaping."""
    snapshot = MySQLNamedServerSnapshot(servers={})
    reconcile = AsyncMock(side_effect=RuntimeError("temporary startup failure"))
    watcher = MySQLNamedServerConfigWatcher("config.json", {}, reconcile)

    with patch(
        "mcp_proxy.mysql_config_watcher.load_named_server_config_snapshot_from_mysql",
        return_value=snapshot,
    ):
        result = await watcher.poll_once()

    assert result is False
    reconcile.assert_awaited_once_with(snapshot)


def test_watcher_requires_positive_poll_interval() -> None:
    """Reject zero and negative poll intervals instead of busy-looping."""
    with pytest.raises(ValueError, match="greater than zero"):
        MySQLNamedServerConfigWatcher("config.json", {}, AsyncMock(), interval=0)


async def test_run_polls_immediately_before_waiting() -> None:
    """The first watcher action is a poll, without an initial interval delay."""
    snapshot = MySQLNamedServerSnapshot(servers={}, poll_interval=4)
    reconcile = AsyncMock()
    watcher = MySQLNamedServerConfigWatcher("config.json", {}, reconcile)
    sleep = AsyncMock(side_effect=asyncio.CancelledError)

    with (
        patch(
            "mcp_proxy.mysql_config_watcher.load_named_server_config_snapshot_from_mysql",
            return_value=snapshot,
        ) as load_snapshot,
        patch(
            "mcp_proxy.mysql_config_watcher.asyncio.sleep",
            new=sleep,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await watcher.run()

    load_snapshot.assert_called_once_with("config.json", {}, log_details=False)
    reconcile.assert_awaited_once_with(snapshot)
    sleep.assert_awaited_once_with(4)
