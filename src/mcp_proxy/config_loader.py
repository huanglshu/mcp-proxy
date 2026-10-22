"""Configuration loader for MCP proxy.

This module loads named server configurations from JSON files or a MySQL table.
"""

import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pymysql
from mcp.client.stdio import StdioServerParameters
from pymysql.cursors import DictCursor

logger = logging.getLogger(__name__)

DEFAULT_MYSQL_CONFIG_POLL_INTERVAL = 30.0


@dataclass(frozen=True)
class MySQLNamedServerConfig:
    """One named server row from the MySQL configuration source."""

    enabled: bool
    version: str | None
    params: StdioServerParameters | None


@dataclass(frozen=True)
class MySQLNamedServerSnapshot:
    """A complete MySQL configuration snapshot and rows that must not be reconciled."""

    servers: dict[str, MySQLNamedServerConfig]
    invalid_names: frozenset[str] = frozenset()
    poll_interval: float = DEFAULT_MYSQL_CONFIG_POLL_INTERVAL

    @property
    def enabled_servers(self) -> dict[str, StdioServerParameters]:
        """Return enabled, valid server parameters."""
        return {
            name: config.params
            for name, config in self.servers.items()
            if config.enabled and config.params is not None
        }


@dataclass
class _MySQLLoadCounts:
    disabled: int = 0
    duplicate: int = 0
    invalid: int = 0


@dataclass
class _MySQLRowParsingState:
    base_env: dict[str, str]
    seen_names: set[str]
    invalid_names: set[str]
    log_details: bool


def _log_if(
    enabled: bool,
    log_method: Callable[..., None],
    message: str,
    *args: object,
) -> None:
    """Emit a loader detail only when detailed logging is enabled."""
    if enabled:
        log_method(message, *args)


def _mysql_log_target(config_data: dict[str, Any]) -> str:
    """Return a safe MySQL connection description for logs."""
    return (
        f"{config_data.get('username', '<unknown>')}@"
        f"{config_data.get('host', '<unknown>')}:"
        f"{config_data.get('port', 3306)}/"
        f"{config_data.get('database', '<unknown>')}"
    )


def _mysql_config_poll_interval(config_data: dict[str, Any]) -> float:
    """Read and validate the polling interval in seconds."""
    interval = config_data.get("poll_interval", DEFAULT_MYSQL_CONFIG_POLL_INTERVAL)
    if isinstance(interval, bool) or not isinstance(interval, int | float):
        msg = "MySQL configuration 'poll_interval' must be a positive number of seconds"
        raise ValueError(msg)  # noqa: TRY004

    interval_seconds = float(interval)
    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        msg = "MySQL configuration 'poll_interval' must be a positive number of seconds"
        raise ValueError(msg)
    return interval_seconds


def _is_mysql_server_enabled(status: object) -> bool:
    """Accept only an integer 1 or the exact string "1" as enabled."""
    return (type(status) is int and status == 1) or (type(status) is str and status == "1")


def _create_named_stdio_params(
    name: str,
    server_config: object,
    base_env: dict[str, str],
    source: str,
    *,
    log_details: bool = True,
) -> StdioServerParameters | None:
    """Convert one MCP server configuration into stdio server parameters."""
    if not isinstance(server_config, dict):
        _log_if(
            log_details,
            logger.warning,
            "Skipping invalid server config for '%s' from %s. Entry is not a dictionary.",
            name,
            source,
        )
        return None

    command = server_config.get("command")
    command_args = server_config.get("args", [])
    env = server_config.get("env", {})

    if not isinstance(command, str) or not command:
        if source == "config":
            _log_if(
                log_details,
                logger.warning,
                "Named server '%s' from config is missing 'command'. Skipping.",
                name,
            )
        else:
            _log_if(
                log_details,
                logger.warning,
                "Named server '%s' from %s is missing 'command'. Skipping.",
                name,
                source,
            )
        return None
    if not isinstance(command_args, list):
        if source == "config":
            _log_if(
                log_details,
                logger.warning,
                "Named server '%s' from config has invalid 'args' (must be a list). "
                "Skipping.",
                name,
            )
        else:
            _log_if(
                log_details,
                logger.warning,
                "Named server '%s' from %s has invalid 'args' (must be a list). Skipping.",
                name,
                source,
            )
        return None
    if not isinstance(env, dict):
        _log_if(
            log_details,
            logger.warning,
            "Named server '%s' from %s has invalid 'env' (must be a dictionary). Skipping.",
            name,
            source,
        )
        return None

    new_env = base_env.copy()
    new_env.update({str(key): str(value) for key, value in env.items()})

    _log_if(
        log_details,
        logger.info,
        "Configured named server '%s' from %s: args_count=%d",
        name,
        source,
        len(command_args),
    )
    return StdioServerParameters(
        command=command,
        args=[str(arg) for arg in command_args],
        env=new_env,
        cwd=None,
    )


def _parse_mysql_server_config(name: str, raw_config: object) -> dict[str, Any]:
    """Parse one database config value, accepting a server object or mcpServers wrapper."""
    if isinstance(raw_config, bytes):
        raw_config = raw_config.decode("utf-8")
    if isinstance(raw_config, str):
        raw_config = json.loads(raw_config)

    if not isinstance(raw_config, dict):
        msg = "config must be a JSON object"
        raise TypeError(msg)

    servers = raw_config.get("mcpServers")
    if servers is None:
        return raw_config
    if not isinstance(servers, dict):
        msg = "config.mcpServers must be a JSON object"
        raise TypeError(msg)
    if name in servers:
        server_config = servers[name]
    elif len(servers) == 1:
        server_config = next(iter(servers.values()))
    else:
        msg = f"config.mcpServers does not contain an entry named '{name}'"
        raise ValueError(msg)
    if not isinstance(server_config, dict):
        msg = f"config.mcpServers['{name}'] must be a JSON object"
        raise TypeError(msg)
    return server_config


def _load_mysql_connection_config(
    config_file_path: str | Path,
    *,
    log_details: bool = True,
) -> dict[str, Any]:
    """Load and validate the MySQL connection configuration file."""
    _log_if(
        log_details,
        logger.info,
        "Reading MySQL named server configuration file: %s",
        config_file_path,
    )
    try:
        with Path(config_file_path).open(encoding="utf-8") as config_file:
            config_data = json.load(config_file)
    except FileNotFoundError:
        _log_if(
            log_details,
            logger.exception,
            "MySQL configuration file not found: %s",
            config_file_path,
        )
        raise
    except json.JSONDecodeError:
        _log_if(
            log_details,
            logger.exception,
            "Error decoding JSON from MySQL configuration file: %s",
            config_file_path,
        )
        raise
    except Exception as e:
        _log_if(
            log_details,
            logger.exception,
            "Unexpected error opening or reading MySQL configuration file %s",
            config_file_path,
        )
        error_message = f"Could not read MySQL configuration file: {e}"
        raise ValueError(error_message) from e

    if not isinstance(config_data, dict):
        msg = f"MySQL configuration in {config_file_path} must be a JSON object"
        _log_if(log_details, logger.error, msg)
        raise TypeError(msg)

    required_fields = ("host", "username", "database", "sql")
    missing_fields = [field for field in required_fields if not config_data.get(field)]
    if missing_fields:
        joined_fields = ", ".join(missing_fields)
        msg = f"MySQL configuration is missing required fields: {joined_fields}"
        _log_if(log_details, logger.error, "%s: %s", msg, config_file_path)
        raise ValueError(msg)

    poll_interval = _mysql_config_poll_interval(config_data)
    _log_if(
        log_details,
        logger.info,
        "MySQL connection settings loaded: target=%s password_configured=%s",
        _mysql_log_target(config_data),
        bool(config_data.get("password")),
    )
    config_data["poll_interval"] = poll_interval
    return config_data


def _fetch_mysql_rows(
    config_data: dict[str, Any],
    *,
    log_details: bool = True,
) -> list[Any]:
    """Execute the configured query and return the rows."""
    target = _mysql_log_target(config_data)
    _log_if(
        log_details,
        logger.info,
        "Connecting to MySQL named server configuration source: %s",
        target,
    )
    connect_started_at = time.perf_counter()
    try:
        connection = pymysql.connect(  # type: ignore[no-untyped-call]
            host=str(config_data["host"]),
            port=int(config_data.get("port", 3306)),
            user=str(config_data["username"]),
            password=str(config_data.get("password", "")),
            database=str(config_data["database"]),
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=int(config_data.get("connect_timeout", 10)),
        )
    except Exception as error:
        _log_if(
            log_details,
            logger.error,
            "Failed to connect to MySQL named server configuration source: %s (%s)",
            target,
            type(error).__name__,
        )
        raise
    _log_if(
        log_details,
        logger.info,
        "Connected to MySQL named server configuration source: %s (%.3fs)",
        target,
        time.perf_counter() - connect_started_at,
    )

    sql = str(config_data["sql"])
    query_started_at = time.perf_counter()
    try:
        with connection.cursor() as cursor:  # type: ignore[no-untyped-call]
            _log_if(
                log_details,
                logger.debug,
                "Executing MySQL named server configuration query: %s",
                sql,
            )
            cursor.execute(sql)
            rows = cursor.fetchall()
    except Exception as error:
        _log_if(
            log_details,
            logger.error,
            "Failed to execute MySQL named server configuration query: %s (%s)",
            target,
            type(error).__name__,
        )
        raise
    finally:
        connection.close()  # type: ignore[no-untyped-call]
        _log_if(
            log_details,
            logger.debug,
            "Closed MySQL named server configuration connection: %s",
            target,
        )

    if not isinstance(rows, tuple | list):
        msg = "MySQL query result must be a list of rows"
        _log_if(log_details, logger.error, "%s: %s", msg, target)
        raise TypeError(msg)
    _log_if(
        log_details,
        logger.info,
        "MySQL named server configuration query returned %d row(s) in %.3fs: %s",
        len(rows),
        time.perf_counter() - query_started_at,
        target,
    )
    return list(rows)


def load_named_server_configs_from_mysql(
    config_file_path: str | Path,
    base_env: dict[str, str],
) -> dict[str, StdioServerParameters]:
    """Load enabled named server configurations from a MySQL query.

    The query must return ``name``, ``config``, ``status``, and ``version``
    columns. Only rows whose status is exactly 1 (or the string ``"1"``) are loaded.

    Args:
        config_file_path: Path to the MySQL connection JSON file.
        base_env: The base environment dictionary to be inherited by servers.

    Returns:
        A dictionary of named server parameters.

    Raises:
        FileNotFoundError: If the connection config file is not found.
        json.JSONDecodeError: If the connection config contains invalid JSON.
        ValueError: If the connection config or result shape is invalid.
        pymysql.MySQLError: If connecting to or querying MySQL fails.
    """
    return load_named_server_config_snapshot_from_mysql(
        config_file_path,
        base_env,
    ).enabled_servers


def load_named_server_config_snapshot_from_mysql(
    config_file_path: str | Path,
    base_env: dict[str, str],
    *,
    log_details: bool = True,
) -> MySQLNamedServerSnapshot:
    """Load named server rows and optionally suppress routine loading details."""
    config_data = _load_mysql_connection_config(
        config_file_path,
        log_details=log_details,
    )
    target = _mysql_log_target(config_data)
    if log_details:
        logger.info(
            "Loading named server configurations from MySQL: %s",
            target,
        )

    rows = _fetch_mysql_rows(config_data, log_details=log_details)
    if log_details:
        logger.info("Processing %d MySQL configuration row(s): %s", len(rows), target)

    named_servers: dict[str, MySQLNamedServerConfig] = {}
    parsing_state = _MySQLRowParsingState(
        base_env=base_env,
        seen_names=set(),
        invalid_names=set(),
        log_details=log_details,
    )
    counts = _MySQLLoadCounts()
    for row_index, row in enumerate(rows, start=1):
        name, server_config, outcome = _parse_mysql_server_row(
            row_index,
            row,
            parsing_state,
        )
        if name is not None and server_config is not None:
            named_servers[name] = server_config
        if outcome == "disabled":
            counts.disabled += 1
        elif outcome == "duplicate":
            counts.duplicate += 1
        elif outcome == "invalid":
            counts.invalid += 1

    if log_details:
        logger.info(
            "MySQL named server configuration summary: target=%s rows=%d loaded=%d "
            "disabled=%d duplicates=%d invalid=%d",
            target,
            len(rows),
            len(MySQLNamedServerSnapshot(named_servers).enabled_servers),
            counts.disabled,
            counts.duplicate,
            counts.invalid,
        )
    return MySQLNamedServerSnapshot(
        servers=named_servers,
        invalid_names=frozenset(parsing_state.invalid_names),
        poll_interval=float(config_data["poll_interval"]),
    )


def _parse_mysql_server_row(
    row_index: int,
    row: object,
    state: _MySQLRowParsingState,
) -> tuple[
    str | None,
    MySQLNamedServerConfig | None,
    Literal["loaded", "disabled", "duplicate", "invalid"],
]:
    """Validate and parse a single MySQL result row."""
    base_env = state.base_env
    seen_names = state.seen_names
    invalid_names = state.invalid_names
    if not isinstance(row, dict):
        msg = f"MySQL query row {row_index} must be a mapping"
        raise ValueError(msg)  # noqa: TRY004

    missing_columns = {"name", "config", "status", "version"} - row.keys()
    if missing_columns:
        joined_columns = ", ".join(sorted(missing_columns))
        msg = f"MySQL query row {row_index} is missing required columns: {joined_columns}"
        raise ValueError(msg)

    name = row.get("name")
    status = row.get("status")
    version = row.get("version")
    _log_if(
        state.log_details,
        logger.debug,
        "Processing MySQL configuration row %s: name=%s status=%s version=%s",
        row_index,
        name,
        status,
        version,
    )
    if not isinstance(name, str) or not name.strip():
        _log_if(
            state.log_details,
            logger.warning,
            "Skipping MySQL row %s because 'name' is invalid.",
            row_index,
        )
        return None, None, "invalid"

    name = name.strip()
    if name in seen_names:
        _log_if(
            state.log_details,
            logger.warning,
            "Skipping duplicate named server '%s' from MySQL row %s.",
            name,
            row_index,
        )
        invalid_names.add(name)
        return name, None, "duplicate"
    seen_names.add(name)
    normalized_version = None if version is None else str(version)

    if not _is_mysql_server_enabled(status):
        _log_if(
            state.log_details,
            logger.info,
            "Named server '%s' is disabled in MySQL. Skipping.",
            name,
        )
        return (
            name,
            MySQLNamedServerConfig(
                enabled=False,
                version=normalized_version,
                params=None,
            ),
            "disabled",
        )

    try:
        server_config = _parse_mysql_server_config(name, row.get("config"))
        params = _create_named_stdio_params(
            name,
            server_config,
            base_env,
            "MySQL",
            log_details=state.log_details,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        _log_if(
            state.log_details,
            logger.warning,
            "Skipping named server '%s' from MySQL row %s: %s",
            name,
            row_index,
            error,
        )
        invalid_names.add(name)
        return name, None, "invalid"

    if params is None:
        invalid_names.add(name)
        return name, None, "invalid"

    _log_if(
        state.log_details,
        logger.info,
        "Loaded named server '%s' from MySQL with version %s (row=%s).",
        name,
        version,
        row_index,
    )
    return (
        name,
        MySQLNamedServerConfig(
            enabled=True,
            version=normalized_version,
            params=params,
        ),
        "loaded",
    )


def load_named_server_configs_from_file(
    config_file_path: str | Path,
    base_env: dict[str, str],
) -> dict[str, StdioServerParameters]:
    """Load named server configurations from a JSON file.

    Args:
        config_file_path: Path to the JSON configuration file.
        base_env: The base environment dictionary to be inherited by servers.

    Returns:
        A dictionary of named server parameters.

    Raises:
        FileNotFoundError: If the config file is not found.
        json.JSONDecodeError: If the config file contains invalid JSON.
        ValueError: If the config file format is invalid.
    """
    logger.info("Loading named server configurations from: %s", config_file_path)

    try:
        with Path(config_file_path).open(encoding="utf-8") as f:
            config_data = json.load(f)
    except FileNotFoundError:
        logger.exception("Configuration file not found: %s", config_file_path)
        raise
    except json.JSONDecodeError:
        logger.exception("Error decoding JSON from configuration file: %s", config_file_path)
        raise
    except Exception as e:
        logger.exception(
            "Unexpected error opening or reading configuration file %s",
            config_file_path,
        )
        error_message = f"Could not read configuration file: {e}"
        raise ValueError(error_message) from e

    if not isinstance(config_data, dict) or "mcpServers" not in config_data:
        msg = f"Invalid config file format in {config_file_path}. Missing 'mcpServers' key."
        logger.error(msg)
        raise ValueError(msg)

    named_stdio_params: dict[str, StdioServerParameters] = {}
    for name, server_config in config_data.get("mcpServers", {}).items():
        if not isinstance(server_config, dict):
            logger.warning(
                "Skipping invalid server config for '%s' in %s. Entry is not a dictionary.",
                name,
                config_file_path,
            )
            continue
        if not server_config.get("enabled", True):  # Default to True if 'enabled' is not present
            logger.info("Named server '%s' from config is not enabled. Skipping.", name)
            continue

        params = _create_named_stdio_params(
            name,
            server_config,
            base_env,
            "config",
        )
        if params is not None:
            named_stdio_params[name] = params

    logger.info(
        "JSON named server configuration summary: source=%s loaded=%d",
        config_file_path,
        len(named_stdio_params),
    )
    return named_stdio_params
