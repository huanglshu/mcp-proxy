"""Poll MySQL named server configuration and notify the running server."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config_loader import (
    DEFAULT_MYSQL_CONFIG_POLL_INTERVAL,
    MySQLNamedServerSnapshot,
    load_named_server_config_snapshot_from_mysql,
)

logger = logging.getLogger(__name__)

class MySQLNamedServerConfigWatcher:
    """Periodically reload MySQL rows and reconcile them with running servers."""

    def __init__(
        self,
        config_file_path: str | Path,
        base_env: dict[str, str],
        reconcile: Callable[[MySQLNamedServerSnapshot], Awaitable[None]],
        *,
        interval: float = DEFAULT_MYSQL_CONFIG_POLL_INTERVAL,
    ) -> None:
        """Store polling inputs and validate the interval."""
        if interval <= 0:
            msg = "MySQL config poll interval must be greater than zero"
            raise ValueError(msg)

        self._config_file_path = config_file_path
        self._base_env = base_env.copy()
        self._reconcile = reconcile
        self._interval = interval

    async def poll_once(self) -> bool:
        """Load one snapshot and reconcile it; retain current servers on failure."""
        poll_started = time.perf_counter()
        try:
            snapshot = await asyncio.to_thread(
                load_named_server_config_snapshot_from_mysql,
                self._config_file_path,
                self._base_env,
                log_details=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.error(  # noqa: TRY400
                "MySQL named server configuration poll failed (%s) after %.3fs; "
                "keeping the current server configuration.",
                type(error).__name__,
                time.perf_counter() - poll_started,
            )
            return False

        self._interval = snapshot.poll_interval
        try:
            await self._reconcile(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.error(  # noqa: TRY400
                "MySQL named server configuration reconciliation failed (%s) after %.3fs; "
                "the next poll will retry.",
                type(error).__name__,
                time.perf_counter() - poll_started,
            )
            return False
        return True

    async def run(self) -> None:
        """Poll immediately after startup, then continue at the configured interval."""
        while True:
            await self.poll_once()
            await asyncio.sleep(self._interval)
