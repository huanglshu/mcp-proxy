"""Tests for the sse server."""
# ruff: noqa: PLR2004

import asyncio
import contextlib
import typing as t
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from mcp import types
from mcp_proxy.config_loader import MySQLNamedServerSnapshot
from mcp_proxy.mcp_server import (
    DEFAULT_EXPOSE_HEADERS,
    MCPServerSettings,
    MySQLNamedServerRuntime,
    create_single_instance_routes,
    run_mcp_server,
)


def create_starlette_app(
    mcp_server: Server[t.Any],
    allow_origins: list[str] | None = None,
    *,
    debug: bool = False,
    stateless: bool = False,
) -> Starlette:
    """Create a Starlette application for the MCP server.

    Args:
        mcp_server: The MCP server instance to wrap
        allow_origins: List of allowed CORS origins
        debug: Enable debug mode
        stateless: Whether to use stateless HTTP sessions

    Returns:
        Starlette application instance
    """
    routes, http_manager = create_single_instance_routes(mcp_server, stateless_instance=stateless)

    middleware: list[Middleware] = []
    if allow_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=allow_origins,
                allow_methods=["*"],
                allow_headers=["*"],
            ),
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> t.AsyncIterator[None]:
        async with http_manager.run():
            yield

    app = Starlette(
        debug=debug,
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )
    app.router.redirect_slashes = False
    return app


class BackgroundServer(uvicorn.Server):
    """A test server that runs in a background thread."""

    def install_signal_handlers(self) -> None:
        """Do not install signal handlers."""

    @contextlib.asynccontextmanager
    async def run_in_background(self) -> t.AsyncIterator[None]:
        """Run the server in a background thread."""
        task = asyncio.create_task(self.serve())
        try:
            while not self.started:  # noqa: ASYNC110
                await asyncio.sleep(1e-3)
            yield
        finally:
            self.should_exit = self.force_exit = True
            await task

    @property
    def url(self) -> str:
        """Return the url of the started server."""
        hostport = next(
            iter([socket.getsockname() for server in self.servers for socket in server.sockets]),
        )
        return f"http://{hostport[0]}:{hostport[1]}"


def make_background_server(*, debug: bool = False, stateless: bool = False) -> BackgroundServer:
    """Create a BackgroundServer instance with specified parameters."""
    mcp_server: Server[object, t.Any] = Server("TestServer")

    @mcp_server.list_prompts()  # type: ignore[misc,no-untyped-call]
    async def list_prompts() -> list[types.Prompt]:
        return [types.Prompt(name="prompt1")]

    @mcp_server.list_tools()  # type: ignore[misc,no-untyped-call]
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="echo",
                description="Echo tool",
                inputSchema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
            ),
        ]

    @mcp_server.call_tool()  # type: ignore[misc]
    async def call_tool(
        name: str,
        arguments: dict[str, t.Any] | None,
    ) -> list[types.Content]:
        assert name == "echo"
        message_value = ""
        if arguments:
            message_value = str(arguments.get("message", ""))
        return [types.TextContent(type="text", text=f"Echo: {message_value}")]

    app = create_starlette_app(
        mcp_server,
        allow_origins=["*"],
        debug=debug,
        stateless=stateless,
    )

    config = uvicorn.Config(app, port=0, log_level="info")
    return BackgroundServer(config)


async def test_sse_transport() -> None:
    """Test basic glue code for the SSE transport and a fake MCP server."""
    server = make_background_server(debug=True)
    async with server.run_in_background():
        sse_url = f"{server.url}/sse"
        async with sse_client(url=sse_url) as streams, ClientSession(*streams) as session:
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"


@pytest.mark.parametrize("path_suffix", ["/mcp/", "/mcp"])
async def test_http_transport(path_suffix: str) -> None:
    """Test HTTP transport layer functionality."""
    server = make_background_server(debug=True)
    async with server.run_in_background():
        http_url = f"{server.url}{path_suffix}"
        async with (
            streamablehttp_client(url=http_url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"

            for i in range(3):
                tool_result = await session.call_tool("echo", {"message": f"test_{i}"})
                assert len(tool_result.content) == 1
                assert isinstance(tool_result.content[0], types.TextContent)
                assert tool_result.content[0].text == f"Echo: test_{i}"


async def test_stateless_http_transport() -> None:
    """Test stateless HTTP transport functionality."""
    server = make_background_server(debug=True, stateless=True)
    async with server.run_in_background():
        http_url = f"{server.url}/mcp/"
        async with (
            streamablehttp_client(url=http_url) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            response = await session.list_prompts()
            assert len(response.prompts) == 1
            assert response.prompts[0].name == "prompt1"

            for i in range(3):
                tool_result = await session.call_tool("echo", {"message": f"test_{i}"})
                assert len(tool_result.content) == 1
                assert isinstance(tool_result.content[0], types.TextContent)
                assert tool_result.content[0].text == f"Echo: test_{i}"


# Unit tests for run_mcp_server method


@pytest.fixture
def mock_settings() -> MCPServerSettings:
    """Create mock MCP server settings for testing."""
    return MCPServerSettings(
        bind_host="127.0.0.1",
        port=8080,
        stateless=False,
        allow_origins=["*"],
        log_level="INFO",
    )


@pytest.fixture
def mock_stdio_params() -> StdioServerParameters:
    """Create mock stdio server parameters for testing."""
    return StdioServerParameters(
        command="echo",
        args=["hello"],
        env={"TEST_VAR": "test_value"},
        cwd="/tmp",  # noqa: S108
    )


def setup_async_context_mocks() -> tuple[
    contextlib.AbstractContextManager[tuple[AsyncMock, AsyncMock]],
    contextlib.AbstractContextManager[tuple[AsyncMock, AsyncMock]],
    AsyncMock,
    MagicMock,
    list[MagicMock],
]:
    """Helper function to set up async context manager mocks."""
    # Setup stdio client mock
    mock_streams = (AsyncMock(), AsyncMock())

    # Setup client session mock
    mock_session = AsyncMock()

    # Setup HTTP manager mock
    mock_http_manager = MagicMock()
    session_manager = create_autospec(StreamableHTTPSessionManager, spec_set=True)
    mock_http_manager.run.return_value = contextlib.nullcontext(session_manager)
    mock_routes = [MagicMock()]

    return (
        contextlib.nullcontext(mock_streams),
        contextlib.nullcontext(mock_session),
        mock_session,
        mock_http_manager,
        mock_routes,
    )


async def test_run_mcp_server_no_servers_configured(mock_settings: MCPServerSettings) -> None:
    """Test run_mcp_server when no servers are configured."""
    with patch("mcp_proxy.mcp_server.logger") as mock_logger:
        await run_mcp_server(mock_settings, None, {})
        mock_logger.error.assert_called_once_with("No servers configured to run.")


async def test_mysql_mode_starts_with_empty_snapshot_and_runs_watcher(
    mock_settings: MCPServerSettings,
) -> None:
    """Keep the proxy alive when all rows are disabled and start its watcher."""
    watcher = AsyncMock()
    uvicorn_config = MagicMock()
    uvicorn_config_factory = MagicMock(return_value=uvicorn_config)
    server = AsyncMock()

    async def serve_with_lifespan() -> None:
        app = uvicorn_config_factory.call_args.args[0]
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)

    server.serve.side_effect = serve_with_lifespan
    runtime = MySQLNamedServerRuntime(
        config_file_path="mysql.json",
        base_env={},
        snapshot=MySQLNamedServerSnapshot(servers={}),
    )

    with (
        patch("mcp_proxy.mcp_server.MySQLNamedServerConfigWatcher") as watcher_class,
        patch("uvicorn.Config", new=uvicorn_config_factory),
        patch("uvicorn.Server", return_value=server),
    ):
        watcher_class.return_value = watcher
        await run_mcp_server(mock_settings, None, {}, mysql_config=runtime)

    server.serve.assert_awaited_once()
    watcher.run.assert_awaited_once()


async def test_run_mcp_server_with_default_server(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with a default server configuration."""
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, mock_session, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, {})

        # Verify calls
        mock_stdio_client.assert_called_once_with(mock_stdio_params)
        mock_create_proxy.assert_called_once_with(mock_session)
        mock_create_routes.assert_called_once_with(
            mock_proxy,
            stateless_instance=mock_settings.stateless,
        )
        mock_logger.info.assert_any_call("Setting up default server.")
        mock_server_instance.serve.assert_called_once()


async def test_run_mcp_server_with_named_servers(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with named servers configuration."""
    named_servers = {
        "server1": mock_stdio_params,
        "server2": StdioServerParameters(
            command="python",
            args=["-m", "mcp_server"],
            env={"PYTHON_PATH": "/usr/bin/python"},
            cwd="/home/user",
        ),
    }

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, None, named_servers)

        # Verify calls
        assert mock_stdio_client.call_count == 2
        assert mock_create_proxy.call_count == 2
        assert mock_create_routes.call_count == 2

        # Check that named servers were logged
        mock_logger.info.assert_any_call(
            "Setting up named server '%s'.",
            "server1",
        )
        mock_logger.info.assert_any_call(
            "Setting up named server '%s'.",
            "server2",
        )

        mock_server_instance.serve.assert_called_once()


async def test_run_mcp_server_with_cors_middleware(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server adds CORS middleware when allow_origins is set."""
    settings_with_cors = MCPServerSettings(
        bind_host="0.0.0.0",  # noqa: S104
        port=9090,
        allow_origins=["http://localhost:3000", "https://example.com"],
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("mcp_proxy.mcp_server.Starlette") as mock_starlette,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(settings_with_cors, mock_stdio_params, {})

        # Verify Starlette was called with middleware
        mock_starlette.assert_called_once()
        call_args = mock_starlette.call_args
        middleware = call_args.kwargs["middleware"]

        assert len(middleware) == 1
        assert middleware[0].cls == CORSMiddleware
        assert middleware[0].kwargs["expose_headers"] == list(DEFAULT_EXPOSE_HEADERS)


async def test_run_mcp_server_custom_expose_headers(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server honors custom expose headers."""
    settings_with_custom_headers = MCPServerSettings(
        bind_host="0.0.0.0",  # noqa: S104
        port=9090,
        allow_origins=["http://localhost"],
        expose_headers=["Header-One", "Header-Two"],
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("mcp_proxy.mcp_server.Starlette") as mock_starlette,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        (
            mock_stdio_context,
            mock_session_context,
            _,
            mock_http_manager,
            mock_routes,
        ) = setup_async_context_mocks()
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        await run_mcp_server(settings_with_custom_headers, mock_stdio_params, {})

        middleware = mock_starlette.call_args.kwargs["middleware"]

        assert len(middleware) == 1
        assert middleware[0].kwargs["expose_headers"] == ["Header-One", "Header-Two"]


async def test_run_mcp_server_debug_mode(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with debug mode enabled."""
    debug_settings = MCPServerSettings(
        bind_host="127.0.0.1",
        port=8080,
        log_level="DEBUG",
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("mcp_proxy.mcp_server.Starlette") as mock_starlette,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(debug_settings, mock_stdio_params, {})

        # Verify Starlette was called with debug=True
        mock_starlette.assert_called_once()
        call_args = mock_starlette.call_args
        assert call_args.kwargs["debug"] is True


async def test_run_mcp_server_stateless_mode(
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with stateless mode enabled."""
    stateless_settings = MCPServerSettings(
        bind_host="127.0.0.1",
        port=8080,
        stateless=True,
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(stateless_settings, mock_stdio_params, {})

        # Verify create_single_instance_routes was called with stateless_instance=True
        mock_create_routes.assert_called_once_with(
            mock_proxy,
            stateless_instance=True,
        )


async def test_run_mcp_server_uvicorn_config(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server creates correct uvicorn configuration."""
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Config") as mock_uvicorn_config,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_config = MagicMock()
        mock_uvicorn_config.return_value = mock_config

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, {})

        # Verify uvicorn.Config was called with correct parameters
        mock_uvicorn_config.assert_called_once()
        call_args = mock_uvicorn_config.call_args

        assert call_args.kwargs["host"] == mock_settings.bind_host
        assert call_args.kwargs["port"] == mock_settings.port
        assert call_args.kwargs["log_level"] == mock_settings.log_level.lower()


async def test_run_mcp_server_global_status_updates(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server updates global status correctly."""
    from mcp_proxy.mcp_server import _global_status  # noqa: PLC0415

    # Clear global status before test
    _global_status["server_instances"].clear()

    named_servers = {"test_server": mock_stdio_params}

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        async def serve_with_lifespan() -> None:
            uvicorn_config = mock_uvicorn_server.call_args.args[0]
            app = uvicorn_config.app
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)

        mock_server_instance.serve.side_effect = serve_with_lifespan

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, named_servers)

        # Verify global status was updated
        assert "default" in _global_status["server_instances"]
        assert "test_server" in _global_status["server_instances"]
        assert _global_status["server_instances"]["default"] == "configured"
        assert _global_status["server_instances"]["test_server"] == "stopped"


async def test_run_mcp_server_http_url_logging(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server logs named Streamable HTTP URLs."""
    named_servers = {"test_server": mock_stdio_params}

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        async def serve_with_lifespan() -> None:
            uvicorn_config = mock_uvicorn_server.call_args.args[0]
            app = uvicorn_config.app
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)

        mock_server_instance.serve.side_effect = serve_with_lifespan

        # Run the function
        await run_mcp_server(mock_settings, mock_stdio_params, named_servers)

        # Verify named Streamable HTTP URLs were logged
        expected_named_url = (
            f"http://{mock_settings.bind_host}:{mock_settings.port}/servers/test_server/mcp"
        )

        mock_logger.info.assert_any_call("Serving named MCP Servers via Streamable HTTP:")
        mock_logger.info.assert_any_call("  - %s", expected_named_url)
        mock_logger.info.assert_any_call(
            "MCP proxy started successfully and is listening at %s.",
            f"http://{mock_settings.bind_host}:{mock_settings.port}",
        )
        assert not any(
            "Serving MCP Servers via SSE" in str(call)
            for call in mock_logger.info.call_args_list
        )
        mock_logger.info.assert_any_call(
            "MCP status: %s/status",
            f"http://{mock_settings.bind_host}:{mock_settings.port}",
        )


async def test_run_mcp_server_exception_handling(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server handles exceptions properly."""
    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession"),
    ):
        # Setup mocks to raise an exception
        mock_stdio_client.side_effect = Exception("Connection failed")

        # Should not raise, function should handle exceptions gracefully
        try:
            await run_mcp_server(mock_settings, mock_stdio_params, {})
        except Exception as e:  # noqa: BLE001
            # If an exception is raised, it should be the expected one
            assert "Connection failed" in str(e)  # noqa: PT017


async def test_run_mcp_server_skips_failed_named_server(
    mock_settings: MCPServerSettings,
) -> None:
    """Test a failed named server does not prevent another named server from starting."""
    from mcp_proxy.mcp_server import _global_status  # noqa: PLC0415

    _global_status["server_instances"].clear()
    named_servers = {
        "broken": StdioServerParameters(command="missing-command", args=[], env={}),
        "healthy": StdioServerParameters(command="echo", args=["hello"], env={}),
    }

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession"),
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        mock_stdio_context, _, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.side_effect = [
            Exception("child process exited"),
            mock_stdio_context,
        ]

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        async def serve_with_lifespan() -> None:
            uvicorn_config = mock_uvicorn_server.call_args.args[0]
            app = uvicorn_config.app
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)

        mock_server_instance.serve.side_effect = serve_with_lifespan

        await run_mcp_server(mock_settings, None, named_servers)

        assert mock_stdio_client.call_count == 2
        mock_create_proxy.assert_called_once()
        mock_create_routes.assert_called_once()
        mock_server_instance.serve.assert_called_once()
        assert _global_status["server_instances"] == {
            "broken": "failed",
            "healthy": "stopped",
        }
        failure_call = next(
            call
            for call in mock_logger.error.call_args_list
            if "Failed to initialize named server" in call.args[0]
        )
        failure_message = failure_call.args[2]
        assert "Cause: Exception" in failure_message
        assert "child process exited" in failure_message
        assert "Hint:" in failure_message
        mock_logger.info.assert_any_call(
            "  - %s",
            f"http://{mock_settings.bind_host}:{mock_settings.port}/servers/healthy/mcp",
        )


async def test_run_mcp_server_releases_failed_named_server_resources(
    mock_settings: MCPServerSettings,
) -> None:
    """Test resources opened before a named server handshake failure are released."""
    released: list[str] = []

    @contextlib.asynccontextmanager
    async def tracked_stdio(
        _params: StdioServerParameters,
        *,
        errlog: t.TextIO,
    ) -> t.AsyncIterator[tuple]:
        assert not errlog.closed
        released.append("stdio-enter")
        try:
            yield (AsyncMock(), AsyncMock())
        finally:
            released.append("stdio-exit")

    with (
        patch("mcp_proxy.mcp_server.stdio_client", side_effect=tracked_stdio),
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("uvicorn.Server") as mock_uvicorn_server,
    ):

        @contextlib.asynccontextmanager
        async def tracked_session(*_streams: AsyncMock) -> t.AsyncIterator[AsyncMock]:
            released.append("session-enter")
            try:
                yield AsyncMock()
            finally:
                released.append("session-exit")

        mock_client_session.side_effect = tracked_session
        mock_create_proxy.side_effect = McpError(
            types.ErrorData(code=0, message="Connection closed"),
        )
        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        await run_mcp_server(
            mock_settings,
            None,
            {"broken": StdioServerParameters(command="echo", args=[], env={})},
        )

    assert released == ["stdio-enter", "session-enter", "session-exit", "stdio-exit"]
    mock_server_instance.serve.assert_called_once()


async def test_named_server_failure_logs_leaf_cause_and_redacted_stderr(
    mock_settings: MCPServerSettings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Test nested failures and child stderr are reported without leaked secrets."""
    command = "secret-command"
    argument = "secret-argument"
    environment_credential = "environment-credential-value"
    database_credential = "database-credential-value"

    class NestedFailureError(Exception):
        exceptions = (McpError(ErrorData(code=0, message="Connection closed")),)

    @contextlib.asynccontextmanager
    async def failing_stdio(
        _params: StdioServerParameters,
        *,
        errlog: t.TextIO,
    ) -> t.AsyncIterator[tuple[AsyncMock, AsyncMock]]:
        errlog.write(
            f"Traceback: {command} {argument}\n"
            f"password={database_credential}\n"
            f"API_KEY={environment_credential}\n"
            "ORACLE_CONNECTION_STRING=ECOLOGY/"
            f"{database_credential}@10.10.11.134:1521/oauat\n",
        )
        errlog.flush()
        raise NestedFailureError("unhandled errors in a TaskGroup")
        yield (AsyncMock(), AsyncMock())

    params = StdioServerParameters(
        command=command,
        args=[argument],
        env={"ORACLE_CONNECTION_STRING": environment_credential},
    )

    with (
        patch("mcp_proxy.mcp_server.stdio_client", side_effect=failing_stdio),
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        await run_mcp_server(mock_settings, None, {"broken": params})

    failure_call = next(
        call
        for call in mock_logger.error.call_args_list
        if "Failed to initialize named server" in call.args[0]
    )
    failure_message = failure_call.args[2]
    assert "Cause: NestedFailureError -> McpError" in failure_message
    assert "Connection closed" in failure_message
    assert "unhandled errors in a TaskGroup" in failure_message
    assert "Child stderr (last lines)" in failure_message
    assert "[redacted]" in failure_message
    assert command not in failure_message
    assert argument not in failure_message
    assert environment_credential not in failure_message
    assert database_credential not in failure_message
    assert "Run the configured child command directly" in failure_message
    assert command not in capsys.readouterr().err


async def test_run_mcp_server_both_default_and_named_servers(
    mock_settings: MCPServerSettings,
    mock_stdio_params: StdioServerParameters,
) -> None:
    """Test run_mcp_server with both default and named servers."""
    named_servers = {"named_server": mock_stdio_params}

    with (
        patch("mcp_proxy.mcp_server.stdio_client") as mock_stdio_client,
        patch("mcp_proxy.mcp_server.ClientSession") as mock_client_session,
        patch("mcp_proxy.mcp_server.create_proxy_server") as mock_create_proxy,
        patch("mcp_proxy.mcp_server.create_single_instance_routes") as mock_create_routes,
        patch("uvicorn.Server") as mock_uvicorn_server,
        patch("mcp_proxy.mcp_server.logger") as mock_logger,
    ):
        # Setup mocks
        mock_stdio_context, mock_session_context, _, mock_http_manager, mock_routes = (
            setup_async_context_mocks()
        )
        mock_stdio_client.return_value = mock_stdio_context
        mock_client_session.return_value = mock_session_context

        mock_proxy = AsyncMock()
        mock_create_proxy.return_value = mock_proxy
        mock_create_routes.return_value = (mock_routes, mock_http_manager)

        mock_server_instance = AsyncMock()
        mock_uvicorn_server.return_value = mock_server_instance

        # Run the function with both default and named servers
        await run_mcp_server(mock_settings, mock_stdio_params, named_servers)

        # Verify both servers were set up
        assert mock_stdio_client.call_count == 2  # One for default, one for named
        assert mock_create_proxy.call_count == 2
        assert mock_create_routes.call_count == 2

        # Verify logging for both servers
        mock_logger.info.assert_any_call("Setting up default server.")
        mock_logger.info.assert_any_call(
            "Setting up named server '%s'.",
            "named_server",
        )

        mock_server_instance.serve.assert_called_once()
