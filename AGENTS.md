# mcp-proxy Agent Instructions

## Scope: mcp-proxy MySQL named server configuration

These notes document the MySQL-backed named server configuration feature so
development can continue consistently on another computer.

### Feature contract

- CLI entry point: `--named-server-mysql-config FILE_PATH`.
- The referenced JSON file contains MySQL connection settings, not MCP server
  definitions: `host`, `port`, `username`, `password`, `database`, `sql`, and
  optional `connect_timeout` and `poll_interval`. The default port is `3306`,
  the default connection timeout is `10` seconds, and the default polling
  interval is `30` seconds.
- The SQL result must return four columns: `name`, `config`, `status`,
  `version`.
- `name` is unique. It becomes the URL path segment `/servers/<name>/`.
- `status` enables a row only when its value is exactly `1` or the string
  `"1"`. Every other value disables the row.
- `version` is metadata for traceability and logging. It does not select between
  multiple versions because `name` is unique.
- `config` may be a JSON string or an already decoded JSON object. It contains
  the standard stdio fields `command`, `args`, and optional `env`. For
  compatibility, a full `mcpServers` wrapper is also accepted; prefer an entry
  keyed by `name`, or use a wrapper containing one server.
- `--named-server-config` and `--named-server-mysql-config` are mutually
  exclusive. Either source overrides `--named-server` CLI definitions.

### Key implementation files

- `src/mcp_proxy/config_loader.py`
  - `load_named_server_configs_from_mysql()`: public MySQL loading entry point.
  - `_load_mysql_connection_config()`: reads and validates the connection JSON.
  - `_fetch_mysql_rows()`: opens the connection, executes SQL, and closes it.
  - `_parse_mysql_server_config()`: parses a direct config or `mcpServers`
    wrapper.
  - `_create_named_stdio_params()`: validates `command`/`args`/`env` and creates
    `StdioServerParameters`.
- `src/mcp_proxy/__main__.py`
  - CLI argument definition.
  - `_load_named_servers_from_mysql_config()`.
  - `_configure_named_servers()` source selection.
- `tests/test_config_loader.py` and `tests/test_cli_arguments.py`.
- `README.md`: user-facing MySQL configuration documentation.
- `start_local_db_config.sh`: local MySQL-backed startup script.
- `.vscode/launch.json`: VS Code launch configuration.
- `pyproject.toml` and `uv.lock`: `pymysql` dependency.

### Security rules

- Never commit real database credentials. `db_config_example.json` is a local
  development file when it contains a real host, account, password, or database;
  keep it untracked and create a sanitized example before committing anything
  intended for other computers.
- Do not log passwords, complete `config` JSON, `env` values, command arguments,
  or raw child-process argv. Safe log fields are connection target
  (`user@host:port/database`), whether a password is configured, row counts,
  durations, `name`, `status`, and `version`.
- SQL may be logged only at `DEBUG`.
- Preserve the existing behavior that skips duplicate names, disabled rows, and
  invalid config rows instead of starting a partially broken server.

### Development and verification

From the repository root:

```bash
uv sync --frozen
uv run --frozen pytest tests -q
uv run --frozen mypy src
uvx --from ruff ruff check src tests
```

Local MySQL-backed startup:

```bash
./start_local_db_config.sh
```

The script resolves its own directory, so it remains portable across machines.
Supported overrides are `DB_CONFIG_FILE`, `HOST`, `PORT`, and `LOG_LEVEL`. The
default config is `db_config_example.json` beside the script. The VS Code launch
entry is `Start mcp-proxy (start_local_db_config.sh)`.

When changing this feature, keep tests covering:

- only `status=1` rows loaded;
- JSON string and object config values;
- optional `mcpServers` wrapper compatibility;
- duplicate, invalid, and disabled rows skipped;
- safe key-node logs with no credential or config leakage;
- missing/invalid connection settings and invalid SQL result shapes.

### Troubleshooting boundary

If startup logs show successful MySQL reading, connection, query, row processing,
and summary messages, then a later error such as
`mcp.shared.exceptions.McpError: Connection closed` is usually caused by the
child MCP server command, not by MySQL loading. On Windows, container paths such
as `/config/kube-config.yaml` are not valid; either use the machine-local
kubeconfig path in the database `config`, or disable that row with `status=0`.
Run the child command directly to see its stderr, because the MCP stdio client
does not surface all child-process stderr output in the outer traceback.

---

## 中文版：mcp-proxy MySQL named server 配置说明

本节仅在开发 `mcp-proxy` 仓库时适用，用于记录基于 MySQL 的 named server
配置功能，方便在不同电脑上继续开发。

### 功能约定

- CLI 参数：`--named-server-mysql-config FILE_PATH`。
- 配置文件保存的是 MySQL 连接信息，不是 MCP server 定义：`host`、`port`、
  `username`、`password`、`database`、`sql`，可选 `connect_timeout` 和
  `poll_interval`。默认端口为 `3306`，默认连接超时为 `10` 秒，默认轮询间隔为
  `30` 秒。
- SQL 查询结果必须包含四列：`name`、`config`、`status`、`version`。
- `name` 是唯一标识，同时作为访问路径 `/servers/<name>/`。
- `status` 只有值为 `1` 或字符串 `"1"` 时才启用，其他值均视为禁用。
- `version` 是配置版本号，仅用于日志和追踪；由于 `name` 唯一，不用于多版本选择。
- `config` 可以是 JSON 字符串，也可以是已经解析好的 JSON 对象，内容使用标准 stdio
  字段：`command`、`args`，可选 `env`。为兼容历史配置，也支持完整的
  `mcpServers` 包装；优先使用以 `name` 为 key 的结构，或只包含一个 server 的包装。
- `--named-server-config` 和 `--named-server-mysql-config` 互斥。两种配置源都会覆盖
  `--named-server` CLI 定义。

### 关键实现文件

- `src/mcp_proxy/config_loader.py`
  - `load_named_server_configs_from_mysql()`：MySQL 配置加载入口。
  - `_load_mysql_connection_config()`：读取并校验连接配置 JSON。
  - `_fetch_mysql_rows()`：建立连接、执行 SQL、关闭连接。
  - `_parse_mysql_server_config()`：解析直接配置或 `mcpServers` 包装格式。
  - `_create_named_stdio_params()`：校验 `command`/`args`/`env` 并创建
    `StdioServerParameters`。
- `src/mcp_proxy/__main__.py`
  - CLI 参数定义。
  - `_load_named_servers_from_mysql_config()`。
  - `_configure_named_servers()`：配置源选择。
- `tests/test_config_loader.py` 与 `tests/test_cli_arguments.py`。
- `README.md`：面向使用者的 MySQL 配置文档。
- `start_local_db_config.sh`：本地 MySQL 配置启动脚本。
- `.vscode/launch.json`：VS Code 启动配置。
- `pyproject.toml` 与 `uv.lock`：`pymysql` 依赖。

### 安全规则

- 禁止提交真实数据库凭据。如果 `db_config_example.json` 中包含真实主机、账号、
  密码或数据库名，应将其视为本地开发文件，保持未跟踪；需要共享时先创建脱敏示例。
- 禁止记录密码、完整 `config` JSON、`env` 值、命令参数或子进程原始 argv。安全的日志
  字段包括连接目标 `user@host:port/database`、是否配置密码、行数、耗时、`name`、
  `status`、`version`。
- SQL 只能在 `DEBUG` 级别输出。
- 保持现有行为：跳过重复 `name`、禁用行和无效配置，不要因为单条配置错误启动一个
  不完整的 server。

### 开发与验证

在仓库根目录执行：

```bash
uv sync --frozen
uv run --frozen pytest tests -q
uv run --frozen mypy src
uvx --from ruff ruff check src tests
```

本地使用 MySQL 配置启动：

```bash
./start_local_db_config.sh
```

脚本会根据自身位置解析路径，因此可在不同电脑上使用。支持覆盖 `DB_CONFIG_FILE`、
`HOST`、`PORT`、`LOG_LEVEL`。默认读取脚本同目录下的 `db_config_example.json`。
VS Code 启动项名称为 `Start mcp-proxy (start_local_db_config.sh)`。

修改该功能时，测试至少覆盖：

- 仅加载 `status=1` 的行；
- JSON 字符串和对象两种 `config`；
- 可选的 `mcpServers` 包装兼容；
- 重复、无效、禁用行会被跳过；
- 关键节点日志存在，且不泄露凭据或配置内容；
- 缺失/无效连接配置和非法 SQL 结果结构。

### 排查边界

如果启动日志已经显示 MySQL 读取、连接、查询、逐行处理和汇总成功，那么后续出现的
`mcp.shared.exceptions.McpError: Connection closed` 通常是子 MCP server 命令的问题，
不是 MySQL 加载失败。在 Windows 上，`/config/kube-config.yaml` 这类容器路径无效；
应在数据库 `config` 中使用本机 kubeconfig 路径，或将该行 `status` 设为 `0` 禁用。
请直接运行子进程命令查看其 stderr，因为 MCP stdio 客户端不会在外层堆栈中展示所有
子进程 stderr。
