"""Create a local SSE server that proxies requests to a stdio MCP server."""

import asyncio
import contextlib
import logging
import os
import re
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Literal, TextIO

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server import Server as MCPServerSDK  # Renamed to avoid conflict
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import Receive, Scope, Send

from .config_loader import MySQLNamedServerConfig, MySQLNamedServerSnapshot
from .mysql_config_watcher import MySQLNamedServerConfigWatcher
from .proxy_server import create_proxy_server

logger = logging.getLogger(__name__)

DEFAULT_EXPOSE_HEADERS: Final[tuple[str, ...]] = ("mcp-session-id",)
_REDACTION_MIN_LENGTH: Final[int] = 4


def _default_expose_headers() -> list[str]:
    return list(DEFAULT_EXPOSE_HEADERS)


@dataclass
class MCPServerSettings:
    """Settings for the MCP server."""

    bind_host: str
    port: int
    stateless: bool = False
    allow_origins: list[str] | None = None
    expose_headers: list[str] = field(default_factory=_default_expose_headers)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


@dataclass(frozen=True)
class MySQLNamedServerRuntime:
    """MySQL source details used to initialize and refresh named servers."""

    config_file_path: str
    base_env: dict[str, str]
    snapshot: MySQLNamedServerSnapshot


# To store last activity for multiple servers if needed, though status endpoint is global for now.
_global_status: dict[str, Any] = {
    "api_last_activity": datetime.now(timezone.utc).isoformat(),
    "server_instances": {},  # Could be used to store per-instance status later
}


def _update_global_activity() -> None:
    _global_status["api_last_activity"] = datetime.now(timezone.utc).isoformat()


class _ASGIEndpointAdapter:
    """Wrap a coroutine function into an ASGI application."""

    def __init__(self, endpoint: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self._endpoint = endpoint

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._endpoint(scope, receive, send)


HTTP_METHODS = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"]


async def _handle_status(_: Request) -> Response:
    """Global health check and service usage monitoring endpoint."""
    return JSONResponse(_global_status)


def create_single_instance_routes(
    mcp_server_instance: MCPServerSDK[object],
    *,
    stateless_instance: bool,
) -> tuple[list[BaseRoute], StreamableHTTPSessionManager]:  # Return the manager itself
    """Create Starlette routes and the HTTP session manager for a single MCP server instance."""
    logger.debug(
        "Creating routes for a single MCP server instance (stateless: %s)",
        stateless_instance,
    )

    sse_transport = SseServerTransport("/messages/")
    http_session_manager = StreamableHTTPSessionManager(
        app=mcp_server_instance,
        event_store=None,
        json_response=True,
        stateless=stateless_instance,
    )

    async def handle_sse_instance(request: Request) -> Response:
        async with sse_transport.connect_sse(
            request.scope,
            request.receive,
            request._send,  # noqa: SLF001
        ) as (read_stream, write_stream):
            _update_global_activity()
            await mcp_server_instance.run(
                read_stream,
                write_stream,
                mcp_server_instance.create_initialization_options(),
                stateless=stateless_instance,
            )
        return Response()

    async def handle_streamable_http_instance(scope: Scope, receive: Receive, send: Send) -> None:
        _update_global_activity()
        updated_scope = scope
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path and path.rstrip("/") == "/mcp" and not path.endswith("/"):
                updated_scope = dict(scope)
                normalized_path = path + "/"
                logger.debug(
                    "Normalized request path from '%s' to '%s' without redirect",
                    path,
                    normalized_path,
                )
                updated_scope["path"] = normalized_path

                raw_path = scope.get("raw_path")
                if raw_path:
                    if b"?" in raw_path:
                        path_part, query_part = raw_path.split(b"?", 1)
                        updated_scope["raw_path"] = path_part.rstrip(b"/") + b"/?" + query_part
                    else:
                        updated_scope["raw_path"] = raw_path.rstrip(b"/") + b"/"

        await http_session_manager.handle_request(updated_scope, receive, send)

    routes = [
        Route(
            "/mcp",
            endpoint=_ASGIEndpointAdapter(handle_streamable_http_instance),
            methods=HTTP_METHODS,
            include_in_schema=False,
        ),
        Mount("/mcp", app=handle_streamable_http_instance),
        Route("/sse", endpoint=handle_sse_instance),
        Mount("/messages/", app=sse_transport.handle_post_message),
    ]
    return routes, http_session_manager


def _should_propagate_server_error(error: BaseException) -> bool:
    """Return whether a server failure is fatal to the proxy process."""
    fatal_errors = (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt, SystemExit)
    if isinstance(error, fatal_errors):
        return True
    nested_errors = getattr(error, "exceptions", None)
    if not isinstance(nested_errors, tuple):
        return False
    return any(_should_propagate_server_error(nested) for nested in nested_errors)


class _CapturedStderr:
    """Capture a bounded child stderr tail for redacted initialization diagnostics."""

    def __init__(self, *, max_chars: int = 64 * 1024) -> None:
        self._max_chars = max_chars
        self._chunks: list[str] = []
        self._captured_chars = 0
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        read_fd, write_fd = os.pipe()
        self._writer = os.fdopen(
            write_fd,
            "w",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
        self._reader = os.fdopen(
            read_fd,
            "r",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    @property
    def errlog(self) -> TextIO:
        """Return the writable end passed to the MCP stdio client."""
        return self._writer

    def _drain(self) -> None:
        try:
            for chunk in iter(self._reader.readline, ""):
                if not chunk:
                    break
                with self._lock:
                    self._chunks.append(chunk)
                    self._captured_chars += len(chunk)
                    self._trim()
        except (OSError, ValueError):
            pass

    def _trim(self) -> None:
        while self._captured_chars > self._max_chars and self._chunks:
            overflow = self._captured_chars - self._max_chars
            first = self._chunks[0]
            if len(first) <= overflow:
                self._chunks.pop(0)
                self._captured_chars -= len(first)
            else:
                self._chunks[0] = first[overflow:]
                self._captured_chars -= overflow

    def close(self) -> None:
        """Close the pipe and wait briefly for stderr draining to finish."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        with contextlib.suppress(OSError, ValueError):
            self._writer.close()
        self._thread.join(timeout=2)
        with contextlib.suppress(OSError, ValueError):
            self._reader.close()

    def getvalue(self) -> str:
        """Return the captured stderr."""
        with self._lock:
            return "".join(self._chunks)


def _nested_exceptions(error: BaseException) -> tuple[BaseException, ...]:
    """Return nested exceptions without relying on Python 3.11 types."""
    nested = getattr(error, "exceptions", None)
    return tuple(nested) if isinstance(nested, tuple) else ()


def _exception_paths(error: BaseException) -> list[list[BaseException]]:
    """Return root-to-leaf exception paths, including explicit causes."""
    paths: list[list[BaseException]] = []
    visited: set[int] = set()

    def visit(current: BaseException, path: list[BaseException]) -> None:
        if id(current) in visited:
            return
        visited.add(id(current))
        current_path = [*path, current]
        nested = _nested_exceptions(current)
        if nested:
            for child in nested:
                visit(child, current_path)
            return

        cause = current.__cause__ or current.__context__
        if cause is not None and id(cause) not in visited:
            visit(cause, current_path)
            return
        paths.append(current_path)

    visit(error, [])
    return paths


def _redact_init_details(text: str, params: StdioServerParameters) -> str:
    """Remove configured command, arguments, environment values, and common secrets."""
    secret_values = {
        params.command,
        *params.args,
        *(params.env or {}).values(),
    }
    for value in sorted(secret_values, key=len, reverse=True):
        if not value:
            continue
        if len(value) >= _REDACTION_MIN_LENGTH:
            text = text.replace(value, "[redacted]")
        else:
            text = re.sub(
                rf"(?<!\w){re.escape(value)}(?!\w)",
                "[redacted]",
                text,
            )

    text = re.sub(
        r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@",
        r"\1[redacted]@",
        text,
    )
    return re.sub(
        r"\b([A-Za-z][\w.-]*)/[^\s@/:]+@",
        r"\1/[redacted]@",
        text,
    )


def _tail_lines(text: str, *, max_lines: int = 20, max_chars: int = 8000) -> list[str]:
    """Return a bounded tail without splitting lines mid-entry."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    tail = lines[-max_lines:]
    while tail and sum(len(line) + 1 for line in tail) > max_chars:
        tail.pop(0)
    return tail


def _format_exception_path(path: list[BaseException]) -> str:
    """Format an exception path, collapsing adjacent duplicate types."""
    names: list[str] = []
    for error in path:
        name = type(error).__name__
        if not names or names[-1] != name:
            names.append(name)
    return " -> ".join(names)


def _initialization_hint(error: BaseException, child_stderr: str) -> str:
    """Return an actionable hint based on the observed failure."""
    types = {type(item).__name__ for path in _exception_paths(error) for item in path}
    details = f"{error} {child_stderr}".lower()
    if "oserror" in {name.lower() for name in types} or "no such file" in details:
        return (
            "The child command could not be launched. Verify its executable and arguments "
            "for this operating system; container-only paths are not available on Windows."
        )
    if "connection closed" in details or "mcp error" in details:
        return (
            "The child process exited or closed stdio before MCP initialization completed. "
            "Run its command directly to reproduce the failure and inspect the stderr above."
        )
    return "Run the configured child command directly to reproduce the initialization failure."


@dataclass
class _NamedServerInitFailure:
    """Mutable state carrying captured stderr out of an initialization context."""

    stderr: str = ""


@contextlib.asynccontextmanager
async def _initialize_named_server(
    params: StdioServerParameters,
    *,
    stateless_instance: bool,
    failure_state: _NamedServerInitFailure,
) -> AsyncIterator[list[BaseRoute]]:
    """Initialize one named server within an isolated resource lifetime."""
    stderr_capture = _CapturedStderr()
    try:
        async with contextlib.AsyncExitStack() as server_stack:
            stdio_streams = await server_stack.enter_async_context(
                stdio_client(params, errlog=stderr_capture.errlog),
            )
            session = await server_stack.enter_async_context(ClientSession(*stdio_streams))
            proxy = await create_proxy_server(session)
            routes, http_manager = create_single_instance_routes(
                proxy,
                stateless_instance=stateless_instance,
            )
            await server_stack.enter_async_context(http_manager.run())
            yield routes
    except BaseException:
        stderr_capture.close()
        failure_state.stderr = stderr_capture.getvalue()
        raise
    finally:
        stderr_capture.close()


def _record_named_server_failure(
    name: str,
    error: BaseException,
    params: StdioServerParameters,
    child_stderr: str,
    *,
    status: str = "failed",
) -> None:
    """Record a named server initialization failure without logging secrets."""
    _global_status["server_instances"][name] = status
    safe_stderr = _redact_init_details(child_stderr, params)
    detail_lines = ["Initialization failure details:"]
    for path in _exception_paths(error):
        detail_lines.append(f"  Cause: {_format_exception_path(path)}")
        for cause in path:
            message = _redact_init_details(str(cause).strip(), params)
            if message:
                detail_lines.append(f"    {type(cause).__name__}: {message}")

    tail = _tail_lines(safe_stderr)
    if tail:
        detail_lines.append("  Child stderr (last lines):")
        detail_lines.extend(f"    {line}" for line in tail)
    else:
        detail_lines.append("  Child stderr: no output captured.")
    detail_lines.append(f"  Hint: {_initialization_hint(error, safe_stderr)}")
    logger.error(
        "Failed to initialize named server '%s'; skipping it.\n%s",
        name,
        "\n".join(detail_lines),
    )


@dataclass
class _ManagedNamedServer:
    """Resources and metadata for one initialized named server."""

    params: StdioServerParameters
    version: str | None
    route: Mount
    stack: contextlib.AsyncExitStack = field(default_factory=contextlib.AsyncExitStack)
    _stop_event: asyncio.Event | None = field(default=None, repr=False)
    _lifecycle_task: asyncio.Task[None] | None = field(default=None, repr=False)

    async def aclose(self) -> None:
        """Close server resources within the task that entered their contexts."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._lifecycle_task is not None:
            try:
                await self._lifecycle_task
            except asyncio.CancelledError:
                self._lifecycle_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._lifecycle_task
                raise
        await self.stack.aclose()


async def _prepare_named_server(
    name: str,
    params: StdioServerParameters,
    *,
    stateless_instance: bool,
    version: str | None,
    failure_status: str = "failed",
) -> _ManagedNamedServer | None:
    """Start and initialize a named server without publishing its route."""
    failure_state = _NamedServerInitFailure()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    ready_future: asyncio.Future[list[BaseRoute]] = loop.create_future()

    async def _lifecycle_runner() -> None:
        server_stack = contextlib.AsyncExitStack()
        try:
            routes = await server_stack.enter_async_context(
                _initialize_named_server(
                    params,
                    stateless_instance=stateless_instance,
                    failure_state=failure_state,
                ),
            )
        except BaseException as error:  # noqa: BLE001
            await server_stack.aclose()
            if not ready_future.done():
                ready_future.set_exception(error)
            return

        if not ready_future.done():
            ready_future.set_result(routes)
        try:
            await stop_event.wait()
        finally:
            await server_stack.aclose()

    lifecycle_task = asyncio.create_task(
        _lifecycle_runner(),
        name=f"mcp-named-server-{name}",
    )
    try:
        routes = await ready_future
    except BaseException as error:
        stop_event.set()
        if not lifecycle_task.done():
            lifecycle_task.cancel()
        with contextlib.suppress(BaseException):
            await lifecycle_task
        if _should_propagate_server_error(error):
            raise
        _record_named_server_failure(
            name,
            error,
            params,
            failure_state.stderr,
            status=failure_status,
        )
        return None

    return _ManagedNamedServer(
        params=params,
        version=version,
        route=Mount(f"/servers/{name}", routes=routes),
        _stop_event=stop_event,
        _lifecycle_task=lifecycle_task,
    )


class _NamedServerRegistry:
    """Own named server resources and update their routes as MySQL changes."""

    def __init__(
        self,
        routes: list[BaseRoute],
        *,
        stateless_instance: bool,
        base_url: str,
        known_names: set[str] | None = None,
    ) -> None:
        self._routes = routes
        self._stateless_instance = stateless_instance
        self._base_url = base_url
        self._servers: dict[str, _ManagedNamedServer] = {}
        self._observed_versions: dict[str, str | None] = {}
        self._known_names = set(known_names or ())
        self._lock = asyncio.Lock()

    def attach(self, app: Starlette) -> None:
        """Switch route mutations to Starlette's live route list."""
        self._routes = app.router.routes

    async def add(
        self,
        name: str,
        params: StdioServerParameters,
        *,
        version: str | None,
    ) -> bool:
        """Initialize one named server and publish its route on success."""
        candidate = await _prepare_named_server(
            name,
            params,
            stateless_instance=self._stateless_instance,
            version=version,
        )
        self._observed_versions[name] = version
        if candidate is None:
            return False

        self._routes.append(candidate.route)
        self._servers[name] = candidate
        _global_status["server_instances"][name] = "configured"
        logger.info("Added named server '%s'.", name)
        return True

    async def remove(
        self,
        name: str,
        *,
        status: Literal["removed", "disabled", "stopped"],
    ) -> None:
        """Unpublish and close one named server."""
        server = self._servers.pop(name, None)
        self._observed_versions.pop(name, None)
        if status in {"removed", "disabled"}:
            if status == "removed":
                self._known_names.discard(name)
            _global_status["server_instances"].pop(name, None)
        else:
            _global_status["server_instances"][name] = status
        if server is not None:
            with contextlib.suppress(ValueError):
                self._routes.remove(server.route)
            logger.info("Removed named server '%s'.", name)
            await server.aclose()

    async def _restart(
        self,
        name: str,
        current: _ManagedNamedServer,
        desired: MySQLNamedServerConfig,
    ) -> None:
        """Start a replacement first, then atomically swap routes."""
        if desired.params is None:
            return
        candidate = await _prepare_named_server(
            name,
            desired.params,
            stateless_instance=self._stateless_instance,
            version=desired.version,
            failure_status="configured",
        )
        self._observed_versions[name] = desired.version
        if candidate is None:
            logger.warning(
                "Could not update named server '%s'; keeping the current instance.",
                name,
            )
            return

        try:
            route_index = self._routes.index(current.route)
        except ValueError:
            self._routes.append(candidate.route)
        else:
            self._routes[route_index] = candidate.route
        self._servers[name] = candidate
        _global_status["server_instances"][name] = "configured"
        await current.aclose()
        logger.info("Updated named server '%s'.", name)

    def _skip_unchanged_version(self, name: str, version: str | None) -> bool:
        """Skip an initialization target whose version has not changed."""
        if name not in self._observed_versions:
            return False
        return self._observed_versions[name] == version

    async def _reconcile_running(self, snapshot: MySQLNamedServerSnapshot) -> None:
        """Update or remove currently running servers."""
        for name, current in list(self._servers.items()):
            if name in snapshot.invalid_names:
                continue

            desired = snapshot.servers.get(name)
            if desired is None:
                await self.remove(name, status="removed")
            elif not desired.enabled:
                await self.remove(name, status="disabled")
            elif current.version != desired.version:
                if self._skip_unchanged_version(name, desired.version):
                    continue
                await self._restart(name, current, desired)
            else:
                self._observed_versions[name] = desired.version

    async def _add_or_disable(
        self,
        name: str,
        desired: MySQLNamedServerConfig,
    ) -> None:
        """Apply one enabled or disabled row that has no running server."""
        if not desired.enabled:
            _global_status["server_instances"].pop(name, None)
            self._observed_versions.pop(name, None)
            return
        if name in self._servers or desired.params is None:
            return
        if self._skip_unchanged_version(name, desired.version):
            return
        await self.add(name, desired.params, version=desired.version)

    def _forget_removed_attempts(self, snapshot: MySQLNamedServerSnapshot) -> None:
        """Forget database rows that no longer exist and clear their status."""
        removed_names: set[str] = set()
        for name in list(self._known_names):
            if name not in snapshot.servers and name not in snapshot.invalid_names:
                self._known_names.discard(name)
                self._observed_versions.pop(name, None)
                if name not in self._servers:
                    _global_status["server_instances"].pop(name, None)
                    removed_names.add(name)

        self._known_names.update(snapshot.servers)
        self._known_names.update(snapshot.invalid_names)

        for name in sorted(removed_names):
            logger.info("Removed named server '%s'.", name)

    async def _add_missing(self, snapshot: MySQLNamedServerSnapshot) -> None:
        """Start enabled servers that are not present in the registry."""
        for name, desired in snapshot.servers.items():
            if name not in snapshot.invalid_names:
                await self._add_or_disable(name, desired)

        self._forget_removed_attempts(snapshot)
        for name in snapshot.invalid_names:
            if name not in self._servers:
                _global_status["server_instances"][name] = "invalid"

    async def reconcile(self, snapshot: MySQLNamedServerSnapshot) -> None:
        """Apply a MySQL snapshot, checking running services before additions."""
        async with self._lock:
            await self._reconcile_running(snapshot)
            await self._add_missing(snapshot)

    async def close(self) -> None:
        """Close every running named server during proxy shutdown."""
        async with self._lock:
            for name in list(self._servers):
                await self.remove(name, status="stopped")


async def _setup_named_servers(
    registry: _NamedServerRegistry,
    named_server_params: dict[str, StdioServerParameters],
    snapshot: MySQLNamedServerSnapshot | None = None,
) -> list[str]:
    """Initialize named servers and retain database version metadata."""
    initialized_names: list[str] = []
    for name, params in named_server_params.items():
        logger.info("Setting up named server '%s'.", name)
        config = snapshot.servers.get(name) if snapshot is not None else None
        version = config.version if config is not None else None
        if await registry.add(name, params, version=version):
            initialized_names.append(name)
    return initialized_names


async def _setup_default_server(
    stack: contextlib.AsyncExitStack,
    params: StdioServerParameters | None,
    *,
    stateless_instance: bool,
) -> list[BaseRoute]:
    """Initialize the optional default stdio server and return its routes."""
    if params is None:
        return []

    logger.info("Setting up default server.")
    stdio_streams = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(*stdio_streams))
    proxy = await create_proxy_server(session)
    routes, http_manager = create_single_instance_routes(
        proxy,
        stateless_instance=stateless_instance,
    )
    await stack.enter_async_context(http_manager.run())
    _global_status["server_instances"]["default"] = "configured"
    return routes


def _create_combined_lifespan(
    named_registry: _NamedServerRegistry,
    config_watcher: MySQLNamedServerConfigWatcher | None,
    *,
    base_url: str,
    initialized_named_servers: list[str],
) -> Callable[[Starlette], contextlib.AbstractAsyncContextManager[None]]:
    """Create the application lifespan that owns the config watcher task."""

    @contextlib.asynccontextmanager
    async def combined_lifespan(app: Starlette) -> AsyncIterator[None]:
        logger.info("Main application lifespan starting...")
        named_registry.attach(app)
        logger.info("MCP proxy started successfully and is listening at %s.", base_url)
        _log_named_server_urls(initialized_named_servers, base_url)
        watcher_task = (
            asyncio.create_task(config_watcher.run())
            if config_watcher is not None
            else None
        )
        try:
            yield
        finally:
            if watcher_task is not None:
                watcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher_task
            logger.info("Main application lifespan shutting down...")

    return combined_lifespan


def _record_initial_mysql_status(snapshot: MySQLNamedServerSnapshot) -> None:
    """Expose invalid rows and omit disabled rows from the status endpoint."""
    for name, server_config in snapshot.servers.items():
        if not server_config.enabled:
            _global_status["server_instances"].pop(name, None)
    for name in snapshot.invalid_names:
        if name not in _global_status["server_instances"]:
            _global_status["server_instances"][name] = "invalid"


def _log_named_server_urls(names: list[str], base_url: str) -> None:
    """Log the Streamable HTTP URLs for successfully initialized named servers."""
    urls = [f"{base_url}/servers/{name}/mcp" for name in names]
    if urls:
        logger.info("Serving named MCP Servers via Streamable HTTP:")
        for url in urls:
            logger.info("  - %s", url)
    logger.info("MCP status: %s/status", base_url)


async def run_mcp_server(
    mcp_settings: MCPServerSettings,
    default_server_params: StdioServerParameters | None = None,
    named_server_params: dict[str, StdioServerParameters] | None = None,
    *,
    mysql_config: MySQLNamedServerRuntime | None = None,
) -> None:
    """Run stdio client(s) and expose an MCP server with multiple possible backends."""
    if named_server_params is None:
        named_server_params = {}

    all_routes: list[BaseRoute] = [
        Route("/status", endpoint=_handle_status),  # Global status endpoint
    ]
    base_url = f"http://{mcp_settings.bind_host}:{mcp_settings.port}"
    named_registry = _NamedServerRegistry(
        all_routes,
        stateless_instance=mcp_settings.stateless,
        base_url=base_url,
        known_names=(
            set(mysql_config.snapshot.servers)
            | set(mysql_config.snapshot.invalid_names)
            if mysql_config is not None
            else None
        ),
    )
    config_watcher = (
        MySQLNamedServerConfigWatcher(
            mysql_config.config_file_path,
            mysql_config.base_env,
            named_registry.reconcile,
            interval=mysql_config.snapshot.poll_interval,
        )
        if mysql_config is not None
        else None
    )
    # Use AsyncExitStack to manage lifecycles of multiple components
    async with contextlib.AsyncExitStack() as stack:
        stack.push_async_callback(named_registry.close)

        all_routes.extend(
            await _setup_default_server(
                stack,
                default_server_params,
                stateless_instance=mcp_settings.stateless,
            ),
        )

        # Setup named servers
        initialized_named_servers = await _setup_named_servers(
            named_registry,
            named_server_params,
            mysql_config.snapshot if mysql_config is not None else None,
        )
        if mysql_config is not None:
            _record_initial_mysql_status(mysql_config.snapshot)

        if (
            not default_server_params
            and not named_server_params
            and mysql_config is None
        ):
            logger.error("No servers configured to run.")
            return

        middleware: list[Middleware] = []
        if mcp_settings.allow_origins:
            middleware.append(
                Middleware(
                    CORSMiddleware,
                    allow_origins=mcp_settings.allow_origins,
                    allow_methods=["*"],
                    allow_headers=["*"],
                    expose_headers=mcp_settings.expose_headers,
                ),
            )

        starlette_app = Starlette(
            debug=(mcp_settings.log_level == "DEBUG"),
            routes=all_routes,
            middleware=middleware,
            lifespan=_create_combined_lifespan(
                named_registry,
                config_watcher,
                base_url=base_url,
                initialized_named_servers=initialized_named_servers,
            ),
        )
        starlette_app.router.redirect_slashes = False
        uvicorn_config = uvicorn.Config(
            starlette_app,
            host=mcp_settings.bind_host,
            port=mcp_settings.port,
            log_level=mcp_settings.log_level.lower(),
        )
        http_server = uvicorn.Server(uvicorn_config)
        logger.debug(
            "Serving incoming MCP requests on %s:%s",
            mcp_settings.bind_host,
            mcp_settings.port,
        )
        await http_server.serve()
