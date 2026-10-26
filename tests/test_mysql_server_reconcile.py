"""Tests for reconciling running named servers with MySQL snapshots."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from mcp.client.stdio import StdioServerParameters
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount, Route
from starlette.testclient import TestClient

from mcp_proxy.config_loader import MySQLNamedServerConfig, MySQLNamedServerSnapshot
from mcp_proxy.mcp_server import (
    _global_status,
    _handle_status,
    _ManagedNamedServer,
    _NamedServerInitFailure,
    _NamedServerRegistry,
)
from mcp_proxy.mysql_config_watcher import MySQLNamedServerConfigWatcher

# ruff: noqa: PLR2004, SLF001


def _managed_server(
    name: str,
    version: str | None,
    params: StdioServerParameters,
    closed: list[str] | None = None,
) -> _ManagedNamedServer:
    stack = contextlib.AsyncExitStack()
    if closed is not None:
        async def record_close() -> None:
            closed.append(name)

        stack.push_async_callback(record_close)
    return _ManagedNamedServer(
        params=params,
        version=version,
        route=Mount(f"/servers/{name}", routes=[]),
        stack=stack,
    )


def _registry_with_server(
    name: str,
    server: _ManagedNamedServer,
) -> tuple[_NamedServerRegistry, list[Mount]]:
    routes = [server.route]
    registry = _NamedServerRegistry(
        routes,
        stateless_instance=False,
        base_url="http://127.0.0.1:8080",
    )
    registry._servers[name] = server
    return registry, routes


async def test_version_change_replaces_route_and_logs_only_server_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Switch to a new server only after its MCP initialization succeeds."""
    old_closed: list[str] = []
    old_params = StdioServerParameters(command="old", args=[], env={})
    new_params = StdioServerParameters(command="new", args=[], env={})
    current = _managed_server("alpha", "1", old_params, old_closed)
    candidate = _managed_server("alpha", "2", new_params)
    registry, routes = _registry_with_server("alpha", current)

    with (
        caplog.at_level("INFO", logger="mcp_proxy.mcp_server"),
        patch(
            "mcp_proxy.mcp_server._prepare_named_server",
            new=AsyncMock(return_value=candidate),
        ),
    ):
        await registry.reconcile(
            MySQLNamedServerSnapshot(
                servers={
                    "alpha": MySQLNamedServerConfig(
                        enabled=True,
                        version="2",
                        params=new_params,
                    ),
                },
            ),
        )

    assert routes == [candidate.route]
    assert registry._servers["alpha"] is candidate
    assert old_closed == ["alpha"]
    assert [record.getMessage() for record in caplog.records] == [
        "Updated named server 'alpha'.",
    ]


async def test_failed_version_retries_only_after_observed_version_changes() -> None:
    """Keep current process and retry a failed version only after a version transition."""
    params = StdioServerParameters(command="current", args=[], env={})
    current = _managed_server("alpha", "1", params)
    registry, routes = _registry_with_server("alpha", current)
    replacement = StdioServerParameters(command="replacement", args=[], env={})
    replacement_candidate = _managed_server("alpha", "2", replacement)

    with patch(
        "mcp_proxy.mcp_server._prepare_named_server",
        new=AsyncMock(side_effect=[None, replacement_candidate]),
    ) as prepare:
        for _ in range(2):
            await registry.reconcile(
                MySQLNamedServerSnapshot(
                    servers={
                        "alpha": MySQLNamedServerConfig(
                            enabled=True,
                            version="2",
                            params=replacement,
                        ),
                    },
                ),
            )

        assert prepare.await_count == 1
        assert routes == [current.route]
        assert registry._servers["alpha"] is current

        await registry.reconcile(
            MySQLNamedServerSnapshot(
                servers={
                    "alpha": MySQLNamedServerConfig(
                        enabled=True,
                        version="1",
                        params=params,
                    ),
                },
            ),
        )
        assert prepare.await_count == 1

        await registry.reconcile(
            MySQLNamedServerSnapshot(
                servers={
                    "alpha": MySQLNamedServerConfig(
                        enabled=True,
                        version="2",
                        params=replacement,
                    ),
                },
            ),
        )

    assert prepare.await_count == 2
    assert routes == [replacement_candidate.route]
    assert registry._servers["alpha"] is replacement_candidate


async def test_failed_new_server_is_not_retried_until_version_changes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Skip polling retries after an initial failure, but retry a changed version."""
    params_v1 = StdioServerParameters(command="server-v1", args=[], env={})
    params_v2 = StdioServerParameters(command="server-v2", args=[], env={})
    candidate = _managed_server("alpha", "2", params_v2)
    registry = _NamedServerRegistry(
        [],
        stateless_instance=False,
        base_url="http://127.0.0.1:8080",
    )

    with (
        caplog.at_level("INFO", logger="mcp_proxy.mcp_server"),
        patch(
            "mcp_proxy.mcp_server._prepare_named_server",
            new=AsyncMock(side_effect=[None, candidate]),
        ) as prepare,
    ):
        same_version_snapshot = MySQLNamedServerSnapshot(
            servers={
                "alpha": MySQLNamedServerConfig(
                    enabled=True,
                    version="1",
                    params=params_v1,
                ),
            },
        )
        await registry.reconcile(same_version_snapshot)
        await registry.reconcile(same_version_snapshot)
        assert prepare.await_count == 1
        assert caplog.text == ""

        await registry.reconcile(
            MySQLNamedServerSnapshot(
                servers={
                    "alpha": MySQLNamedServerConfig(
                        enabled=True,
                        version="2",
                        params=params_v2,
                    ),
                },
            ),
        )

    assert prepare.await_count == 2
    assert registry._servers["alpha"] is candidate
    assert [record.getMessage() for record in caplog.records] == [
        "Added named server 'alpha'.",
    ]


async def test_unchanged_version_does_not_restart_server(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stable version is treated as the same live server configuration."""
    params = StdioServerParameters(command="current", args=[], env={})
    current = _managed_server("alpha", "1", params)
    registry, routes = _registry_with_server("alpha", current)

    with (
        caplog.at_level("DEBUG", logger="mcp_proxy.mcp_server"),
        patch("mcp_proxy.mcp_server._prepare_named_server", new=AsyncMock()) as prepare,
    ):
        await registry.reconcile(
            MySQLNamedServerSnapshot(
                servers={
                    "alpha": MySQLNamedServerConfig(
                        enabled=True,
                        version="1",
                        params=params,
                    ),
                },
            ),
        )

    prepare.assert_not_awaited()
    assert routes == [current.route]
    assert registry._servers["alpha"] is current
    assert caplog.text == ""


async def test_invalid_named_row_preserves_running_server() -> None:
    """An invalid row with a known name must not remove its working instance."""
    params = StdioServerParameters(command="current", args=[], env={})
    current = _managed_server("alpha", "1", params)
    registry, routes = _registry_with_server("alpha", current)

    await registry.reconcile(
        MySQLNamedServerSnapshot(
            servers={},
            invalid_names=frozenset({"alpha"}),
        ),
    )

    assert routes == [current.route]
    assert registry._servers["alpha"] is current


async def test_disabled_and_deleted_rows_remove_running_servers() -> None:
    """Disable or physically delete a database row to unpublish and close it."""
    params = StdioServerParameters(command="server", args=[], env={})
    closed: list[str] = []
    current = _managed_server("alpha", "1", params, closed)
    registry, routes = _registry_with_server("alpha", current)

    await registry.reconcile(
        MySQLNamedServerSnapshot(
            servers={
                "alpha": MySQLNamedServerConfig(
                    enabled=False,
                    version="2",
                    params=None,
                ),
            },
        ),
    )
    assert routes == []
    assert "alpha" not in registry._servers
    assert closed == ["alpha"]
    assert "alpha" not in _global_status["server_instances"]

    second = _managed_server("beta", "1", params, closed)
    registry._routes.append(second.route)
    registry._servers["beta"] = second
    await registry.reconcile(MySQLNamedServerSnapshot(servers={}))

    assert second.route not in routes
    assert "beta" not in registry._servers
    assert closed == ["alpha", "beta"]
    assert "beta" not in _global_status["server_instances"]


async def test_new_disabled_row_updates_status_without_change_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A disabled row is omitted from status unless it is later removed from MySQL."""
    registry = _NamedServerRegistry(
        [],
        stateless_instance=False,
        base_url="http://127.0.0.1:8080",
    )

    with caplog.at_level("DEBUG", logger="mcp_proxy.mcp_server"):
        await registry.reconcile(
            MySQLNamedServerSnapshot(
                servers={
                    "disabled-server": MySQLNamedServerConfig(
                        enabled=False,
                        version="1",
                        params=None,
                    ),
                },
            ),
        )
        assert "disabled-server" not in _global_status["server_instances"]
        assert caplog.text == ""
        caplog.clear()

        await registry.reconcile(MySQLNamedServerSnapshot(servers={}))

    assert "disabled-server" not in _global_status["server_instances"]
    assert [record.getMessage() for record in caplog.records] == [
        "Removed named server 'disabled-server'.",
    ]


async def test_deleted_failed_server_is_removed_from_status() -> None:
    """A deleted failed row is removed from status rather than retained as history."""
    registry = _NamedServerRegistry(
        [],
        stateless_instance=False,
        base_url="http://127.0.0.1:8080",
        known_names={"failed-server"},
    )
    registry._observed_versions["failed-server"] = "1"
    _global_status["server_instances"]["failed-server"] = "failed"

    await registry.reconcile(MySQLNamedServerSnapshot(servers={}))

    assert "failed-server" not in _global_status["server_instances"]


async def test_new_enabled_server_is_added() -> None:
    """A newly enabled row is initialized and published at its named route."""
    params = StdioServerParameters(command="new", args=[], env={})
    candidate = _managed_server("new-server", "7", params)
    registry = _NamedServerRegistry(
        [],
        stateless_instance=False,
        base_url="http://127.0.0.1:8080",
    )

    with patch(
        "mcp_proxy.mcp_server._prepare_named_server",
        new=AsyncMock(return_value=candidate),
    ):
        await registry.reconcile(
            MySQLNamedServerSnapshot(
                servers={
                    "new-server": MySQLNamedServerConfig(
                        enabled=True,
                        version="7",
                        params=params,
                    ),
                },
            ),
        )

    assert registry._routes == [candidate.route]
    assert registry._servers["new-server"] is candidate


async def test_status_route_survives_named_server_changes() -> None:
    """Keep /status available while dynamic named-server mounts are changed."""
    routes = [Route("/status", endpoint=_handle_status)]
    app = Starlette(routes=routes)
    registry = _NamedServerRegistry(
        routes,
        stateless_instance=False,
        base_url="http://127.0.0.1:8080",
    )
    registry.attach(app)
    params_v1 = StdioServerParameters(command="server-v1", args=[], env={})
    params_v2 = StdioServerParameters(command="server-v2", args=[], env={})
    candidate_v1 = _managed_server("alpha", "1", params_v1)
    candidate_v2 = _managed_server("alpha", "2", params_v2)

    with TestClient(app) as client:
        assert client.get("/status").status_code == 200

        with patch(
            "mcp_proxy.mcp_server._prepare_named_server",
            new=AsyncMock(side_effect=[candidate_v1, candidate_v2]),
        ):
            await registry.reconcile(
                MySQLNamedServerSnapshot(
                    servers={
                        "alpha": MySQLNamedServerConfig(
                            enabled=True,
                            version="1",
                            params=params_v1,
                        ),
                    },
                ),
            )
            assert client.get("/status").status_code == 200

            await registry.reconcile(
                MySQLNamedServerSnapshot(
                    servers={
                        "alpha": MySQLNamedServerConfig(
                            enabled=True,
                            version="2",
                            params=params_v2,
                        ),
                    },
                ),
            )
            assert client.get("/status").status_code == 200

        await registry.reconcile(
            MySQLNamedServerSnapshot(
                servers={
                    "alpha": MySQLNamedServerConfig(
                        enabled=False,
                        version="2",
                        params=None,
                    ),
                },
            ),
        )
        assert client.get("/status").status_code == 200


async def test_status_route_survives_reconcile_runtime_error() -> None:
    """A failed old-server shutdown must not take the global status route offline."""
    routes = [Route("/status", endpoint=_handle_status)]
    app = Starlette(routes=routes)
    registry = _NamedServerRegistry(
        routes,
        stateless_instance=False,
        base_url="http://127.0.0.1:8080",
    )
    registry.attach(app)

    old_params = StdioServerParameters(command="old", args=[], env={})
    new_params = StdioServerParameters(command="new", args=[], env={})
    current = _managed_server("alpha", "1", old_params)

    async def fail_shutdown() -> None:
        raise RuntimeError("simulated old server shutdown failure")

    current.stack.push_async_callback(fail_shutdown)
    registry._servers["alpha"] = current
    registry._observed_versions["alpha"] = "1"
    replacement = _managed_server("alpha", "2", new_params)
    snapshot = MySQLNamedServerSnapshot(
        servers={
            "alpha": MySQLNamedServerConfig(
                enabled=True,
                version="2",
                params=new_params,
            ),
        },
    )
    watcher = MySQLNamedServerConfigWatcher(
        "config.json",
        {},
        registry.reconcile,
    )

    with (
        TestClient(app) as client,
        patch(
            "mcp_proxy.mysql_config_watcher.load_named_server_config_snapshot_from_mysql",
            return_value=snapshot,
        ),
        patch(
            "mcp_proxy.mcp_server._prepare_named_server",
            new=AsyncMock(return_value=replacement),
        ),
    ):
        assert client.get("/status").status_code == 200
        assert await watcher.poll_once() is False
        assert client.get("/status").status_code == 200


async def test_cross_task_reconcile_closes_anyio_task_group_cleanly() -> None:
    """Reconciling from a watcher task must not exit AnyIO cancel scopes across tasks."""
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_initialize_named_server(
        params: StdioServerParameters,
        *,
        stateless_instance: bool,  # noqa: ARG001
        failure_state: _NamedServerInitFailure,  # noqa: ARG001
    ) -> AsyncIterator[list[BaseRoute]]:
        async with anyio.create_task_group():
            events.append(f"enter:{params.command}")
            try:
                yield []
            finally:
                events.append(f"exit:{params.command}")

    registry = _NamedServerRegistry(
        [],
        stateless_instance=True,
        base_url="http://127.0.0.1:8080",
    )
    params_v1 = StdioServerParameters(command="v1", args=[], env={})
    params_v2 = StdioServerParameters(command="v2", args=[], env={})

    with patch(
        "mcp_proxy.mcp_server._initialize_named_server",
        side_effect=fake_initialize_named_server,
    ):
        assert await registry.add("alpha", params_v1, version="1") is True

        update_task = asyncio.create_task(
            registry.reconcile(
                MySQLNamedServerSnapshot(
                    servers={
                        "alpha": MySQLNamedServerConfig(
                            enabled=True,
                            version="2",
                            params=params_v2,
                        ),
                    },
                ),
            ),
        )
        await update_task

        disable_task = asyncio.create_task(
            registry.reconcile(
                MySQLNamedServerSnapshot(
                    servers={
                        "alpha": MySQLNamedServerConfig(
                            enabled=False,
                            version="2",
                            params=None,
                        ),
                    },
                ),
            ),
        )
        await disable_task

    assert events == ["enter:v1", "enter:v2", "exit:v1", "exit:v2"]
    assert "alpha" not in registry._servers
