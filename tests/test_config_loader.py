"""Tests for the configuration loader module."""

import json
import shutil
import tempfile
import typing as t
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from mcp.client.stdio import StdioServerParameters

from mcp_proxy.config_loader import (
    load_named_server_configs_from_file,
    load_named_server_configs_from_mysql,
)


@pytest.fixture
def create_temp_config_file() -> Generator[Callable[[dict[str, t.Any]], str], None, None]:
    """Creates a temporary JSON config file and returns its path."""
    temp_files: list[str] = []

    def _create_temp_config_file(config_content: dict[str, t.Any]) -> str:
        with tempfile.NamedTemporaryFile(
            mode="w",
            delete=False,
            suffix=".json",
        ) as tmp_config:
            json.dump(config_content, tmp_config)
            temp_files.append(tmp_config.name)
            return tmp_config.name

    yield _create_temp_config_file

    for f_path in temp_files:
        path = Path(f_path)
        if path.exists():
            path.unlink()


def test_load_valid_config(create_temp_config_file: Callable[[dict[str, t.Any]], str]) -> None:
    """Test loading a valid configuration file."""
    config_content = {
        "mcpServers": {
            "server1": {
                "command": "echo",
                "args": ["hello"],
                "env": {"FOO": "bar"},
                "enabled": True,
            },
            "server2": {
                "command": "cat",
                "args": ["file.txt"],
            },
        },
    }
    tmp_config_path = create_temp_config_file(config_content)
    base_env = {"PASSED": "env_value"}
    base_env_with_added_env = {"PASSED": "env_value", "FOO": "bar"}

    loaded_params = load_named_server_configs_from_file(tmp_config_path, base_env)

    assert "server1" in loaded_params
    assert loaded_params["server1"].command == "echo"
    assert loaded_params["server1"].args == ["hello"]
    assert (
        loaded_params["server1"].env == base_env_with_added_env
    )  # Env is a copy, check if it contains base_env items

    assert "server2" in loaded_params
    assert loaded_params["server2"].command == "cat"
    assert loaded_params["server2"].args == ["file.txt"]
    assert loaded_params["server2"].env == base_env


def test_load_config_with_not_enabled_server(
    create_temp_config_file: Callable[[dict[str, t.Any]], str],
) -> None:
    """Test loading a configuration with disabled servers."""
    config_content = {
        "mcpServers": {
            "explicitly_enabled_server": {"command": "true_command", "enabled": True},
            # No 'enabled' flag, defaults to True
            "implicitly_enabled_server": {"command": "another_true_command"},
            "not_enabled_server": {"command": "false_command", "enabled": False},
        },
    }
    tmp_config_path = create_temp_config_file(config_content)
    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})

    assert "explicitly_enabled_server" in loaded_params
    assert loaded_params["explicitly_enabled_server"].command == "true_command"
    assert "implicitly_enabled_server" in loaded_params
    assert loaded_params["implicitly_enabled_server"].command == "another_true_command"
    assert "not_enabled_server" not in loaded_params


def test_file_not_found() -> None:
    """Test handling of non-existent configuration files."""
    with pytest.raises(FileNotFoundError):
        load_named_server_configs_from_file("non_existent_file.json", {})


def test_json_decode_error() -> None:
    """Test handling of invalid JSON in configuration files."""
    # Create a file with invalid JSON content
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        suffix=".json",
    ) as tmp_config:
        tmp_config.write("this is not json {")
        tmp_config_path = tmp_config.name

    # Use try/finally to ensure cleanup
    try:
        with pytest.raises(json.JSONDecodeError):
            load_named_server_configs_from_file(tmp_config_path, {})
    finally:
        path = Path(tmp_config_path)
        if path.exists():
            path.unlink()


def test_load_example_fetch_config_if_uvx_exists() -> None:
    """Test loading the example fetch configuration if uvx is available."""
    if not shutil.which("uvx"):
        pytest.skip("uvx command not found in PATH, skipping test for example config.")

    # Assuming the test is run from the root of the repository
    example_config_path = Path(__file__).parent.parent / "config_example.json"

    if not example_config_path.exists():
        pytest.fail(
            f"Example config file not found at expected path: {example_config_path}",
        )

    base_env = {"EXAMPLE_ENV": "true"}
    loaded_params = load_named_server_configs_from_file(example_config_path, base_env)

    assert "context7" in loaded_params
    context7_param = loaded_params["context7"]
    assert isinstance(context7_param, StdioServerParameters)
    assert context7_param.command == "npx"
    assert context7_param.args == ["@upstash/context7-mcp@1.0.31"]
    assert context7_param.env["EXAMPLE_ENV"] == "true"
    # The 'timeout' and 'transportType' fields from the config are currently ignored by the loader,
    # so no need to assert them on StdioServerParameters.


def test_invalid_config_format_missing_mcpservers(
    create_temp_config_file: Callable[[dict[str, t.Any]], str],
) -> None:
    """Test handling of configuration files missing the mcpServers key."""
    config_content = {"some_other_key": "value"}
    tmp_config_path = create_temp_config_file(config_content)

    with pytest.raises(ValueError, match="Missing 'mcpServers' key"):
        load_named_server_configs_from_file(tmp_config_path, {})


@patch("mcp_proxy.config_loader.logger")
def test_invalid_server_entry_not_dict(
    mock_logger: Mock,
    create_temp_config_file: Callable[[dict[str, t.Any]], str],
) -> None:
    """Test handling of server entries that are not dictionaries."""
    config_content = {"mcpServers": {"server1": "not_a_dict"}}
    tmp_config_path = create_temp_config_file(config_content)

    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})
    assert len(loaded_params) == 0  # No servers should be loaded
    mock_logger.warning.assert_called_with(
        "Skipping invalid server config for '%s' in %s. Entry is not a dictionary.",
        "server1",
        tmp_config_path,
    )


@patch("mcp_proxy.config_loader.logger")
def test_server_entry_missing_command(
    mock_logger: Mock,
    create_temp_config_file: Callable[[dict[str, t.Any]], str],
) -> None:
    """Test handling of server entries missing the command field."""
    config_content = {"mcpServers": {"server_no_command": {"args": ["arg1"]}}}
    tmp_config_path = create_temp_config_file(config_content)
    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})
    assert "server_no_command" not in loaded_params
    mock_logger.warning.assert_called_with(
        "Named server '%s' from config is missing 'command'. Skipping.",
        "server_no_command",
    )


@patch("mcp_proxy.config_loader.logger")
def test_server_entry_invalid_args_type(
    mock_logger: Mock,
    create_temp_config_file: Callable[[dict[str, t.Any]], str],
) -> None:
    """Test handling of server entries with invalid args type."""
    config_content = {
        "mcpServers": {
            "server_invalid_args": {"command": "mycmd", "args": "not_a_list"},
        },
    }
    tmp_config_path = create_temp_config_file(config_content)
    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})
    assert "server_invalid_args" not in loaded_params
    mock_logger.warning.assert_called_with(
        "Named server '%s' from config has invalid 'args' (must be a list). Skipping.",
        "server_invalid_args",
    )


def test_empty_mcpservers_dict(create_temp_config_file: Callable[[dict[str, t.Any]], str]) -> None:
    """Test handling of configuration files with empty mcpServers dictionary."""
    config_content: dict[str, t.Any] = {"mcpServers": {}}
    tmp_config_path = create_temp_config_file(config_content)
    loaded_params = load_named_server_configs_from_file(tmp_config_path, {})
    assert len(loaded_params) == 0


def test_config_file_is_empty_json_object(
    create_temp_config_file: Callable[[dict[str, t.Any]], str],
) -> None:
    """Test handling of configuration files with empty JSON objects."""
    config_content: dict[str, t.Any] = {}  # Empty JSON object
    tmp_config_path = create_temp_config_file(config_content)
    with pytest.raises(ValueError, match="Missing 'mcpServers' key"):
        load_named_server_configs_from_file(tmp_config_path, {})


def test_config_file_is_empty_string() -> None:
    """Test handling of configuration files with empty content."""
    # Create a file with an empty string
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        suffix=".json",
    ) as tmp_config:
        tmp_config.write("")  # Empty content
        tmp_config_path = tmp_config.name
    try:
        with pytest.raises(json.JSONDecodeError):
            load_named_server_configs_from_file(tmp_config_path, {})
    finally:
        path = Path(tmp_config_path)
        if path.exists():
            path.unlink()


@pytest.fixture
def mysql_config_file() -> Generator[str, None, None]:
    """Creates a temporary MySQL connection config file."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        delete=False,
        suffix=".json",
        encoding="utf-8",
    ) as f:
        json.dump(
            {
                "host": "mysql.example.com",
                "port": 3306,
                "username": "reader",
                "password": "secret",
                "database": "mcp",
                "sql": (
                    "SELECT mcp_code AS name, config_json AS config, status, version "
                    "FROM t_mcp_config"
                ),
            },
            f,
        )
        path = f.name

    yield path

    config_path = Path(path)
    if config_path.exists():
        config_path.unlink()


@patch("mcp_proxy.config_loader.pymysql.connect")
def test_load_named_servers_from_mysql(
    mock_connect: Mock,
    mysql_config_file: str,
) -> None:
    """Load enabled server rows from MySQL and ignore disabled rows."""
    mock_cursor = Mock()
    mock_cursor.fetchall.return_value = [
        {
            "name": "enabled_server",
            "config": json.dumps(
                {
                    "command": "uvx",
                    "args": ["mcp-server-fetch"],
                    "env": {"FOO": "bar"},
                },
            ),
            "status": 1,
            "version": "2026.10.22",
        },
        {
            "name": "disabled_server",
            "config": {"command": "disabled-command"},
            "status": 0,
            "version": "1",
        },
    ]
    mock_connection = mock_connect.return_value
    mock_connection.cursor.return_value.__enter__.return_value = mock_cursor

    loaded_params = load_named_server_configs_from_mysql(
        mysql_config_file,
        {"PASSED": "env_value"},
    )

    assert set(loaded_params) == {"enabled_server"}
    enabled_server = loaded_params["enabled_server"]
    assert enabled_server.command == "uvx"
    assert enabled_server.args == ["mcp-server-fetch"]
    assert enabled_server.env == {"PASSED": "env_value", "FOO": "bar"}
    mock_connection.close.assert_called_once_with()
    mock_cursor.execute.assert_called_once()


@patch("mcp_proxy.config_loader.pymysql.connect")
def test_load_named_servers_from_mysql_accepts_mcp_servers_wrapper(
    mock_connect: Mock,
    mysql_config_file: str,
) -> None:
    """Accept a full mcpServers wrapper in the config column."""
    mock_cursor = Mock()
    mock_cursor.fetchall.return_value = [
        {
            "name": "context7",
            "config": {
                "mcpServers": {
                    "context7": {
                        "command": "npx",
                        "args": ["-y", "@upstash/context7-mcp@1.0.31"],
                    },
                },
            },
            "status": "1",
            "version": 3,
        },
    ]
    mock_connect.return_value.cursor.return_value.__enter__.return_value = mock_cursor

    loaded_params = load_named_server_configs_from_mysql(mysql_config_file, {})

    assert list(loaded_params) == ["context7"]
    assert loaded_params["context7"].command == "npx"


@patch("mcp_proxy.config_loader.pymysql.connect")
def test_load_named_servers_from_mysql_logs_key_nodes_without_secrets(
    mock_connect: Mock,
    mysql_config_file: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log key MySQL loading stages without exposing the password or config value."""
    mock_cursor = Mock()
    mock_cursor.fetchall.return_value = [
        {
            "name": "logged_server",
            "config": {"command": "top-secret-command"},
            "status": 1,
            "version": 7,
        },
    ]
    mock_connect.return_value.cursor.return_value.__enter__.return_value = mock_cursor

    with caplog.at_level("DEBUG", logger="mcp_proxy.config_loader"):
        load_named_server_configs_from_mysql(mysql_config_file, {})

    log_messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Reading MySQL named server configuration file" in message
        for message in log_messages
    )
    assert any("MySQL connection settings loaded" in message for message in log_messages)
    assert any(
        "Connecting to MySQL named server configuration source" in message
        for message in log_messages
    )
    assert any(
        "Executing MySQL named server configuration query" in message
        for message in log_messages
    )
    assert any("returned 1 row(s)" in message for message in log_messages)
    assert any(
        "Loaded named server 'logged_server' from MySQL" in message
        for message in log_messages
    )
    assert any("MySQL named server configuration summary" in message for message in log_messages)
    assert "secret" not in caplog.text
    assert "top-secret-command" not in caplog.text


@patch("mcp_proxy.config_loader.logger")
@patch("mcp_proxy.config_loader.pymysql.connect")
def test_load_named_servers_from_mysql_skips_invalid_and_duplicate_rows(
    mock_connect: Mock,
    mock_logger: Mock,
    mysql_config_file: str,
) -> None:
    """Skip invalid config rows and duplicate names from MySQL."""
    mock_cursor = Mock()
    mock_cursor.fetchall.return_value = [
        {
            "name": "duplicate",
            "config": {"command": "first-command"},
            "status": 1,
            "version": 1,
        },
        {
            "name": "duplicate",
            "config": {"command": "second-command"},
            "status": 1,
            "version": 2,
        },
        {
            "name": "invalid_json",
            "config": "not json",
            "status": 1,
            "version": 1,
        },
        {
            "name": "",
            "config": {"command": "ignored-command"},
            "status": 1,
            "version": 1,
        },
    ]
    mock_connect.return_value.cursor.return_value.__enter__.return_value = mock_cursor

    loaded_params = load_named_server_configs_from_mysql(mysql_config_file, {})

    assert list(loaded_params) == ["duplicate"]
    assert loaded_params["duplicate"].command == "first-command"
    assert mock_logger.warning.call_count == 3  # noqa: PLR2004
    mock_logger.info.assert_any_call(
        "MySQL named server configuration summary: target=%s rows=%d loaded=%d "
        "disabled=%d duplicates=%d invalid=%d",
        "reader@mysql.example.com:3306/mcp",
        4,
        1,
        0,
        1,
        2,
    )


def test_load_named_servers_from_mysql_invalid_connection_config(
    create_temp_config_file: Callable[[dict[str, t.Any]], str],
) -> None:
    """Reject MySQL connection config files missing required fields."""
    tmp_config_path = create_temp_config_file(
        {
            "host": "mysql.example.com",
            "username": "reader",
            "sql": "SELECT 1",
        },
    )

    with pytest.raises(ValueError, match="missing required fields: database"):
        load_named_server_configs_from_mysql(tmp_config_path, {})
