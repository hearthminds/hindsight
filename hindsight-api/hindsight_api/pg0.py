import asyncio
import logging
import os
import signal
from pathlib import Path

from pg0 import Pg0

logger = logging.getLogger(__name__)

DEFAULT_USERNAME = "hindsight"
DEFAULT_PASSWORD = "hindsight"
DEFAULT_DATABASE = "hindsight"


class EmbeddedPostgres:
    """Manages an embedded PostgreSQL server instance using pg0-embedded."""

    def __init__(
        self,
        port: int | None = None,
        username: str = DEFAULT_USERNAME,
        password: str = DEFAULT_PASSWORD,
        database: str = DEFAULT_DATABASE,
        name: str = "hindsight",
        pg_config: dict[str, str] | None = None,
        **kwargs,
    ):
        self.port = port  # None means pg0 will auto-assign
        self.username = username
        self.password = password
        self.database = database
        self.name = name
        self.pg_config = pg_config
        self._pg0: Pg0 | None = None

    def _get_pg0(self) -> Pg0:
        if self._pg0 is None:
            kwargs = {
                "name": self.name,
                "username": self.username,
                "password": self.password,
                "database": self.database,
            }
            # Only set port if explicitly specified
            if self.port is not None:
                kwargs["port"] = self.port
            # Forward pg_config as Pg0's config parameter (e.g. listen_addresses)
            if self.pg_config is not None:
                kwargs["config"] = self.pg_config
            self._pg0 = Pg0(**kwargs)  # type: ignore[invalid-argument-type] - dict kwargs
        return self._pg0

    async def start(self, max_retries: int = 5, retry_delay: float = 4.0) -> str:
        """Start the PostgreSQL server with retry logic."""
        port_info = f"port={self.port}" if self.port else "port=auto"
        logger.info(f"Starting embedded PostgreSQL (name={self.name}, {port_info})...")

        pg0 = self._get_pg0()
        last_error = None

        for attempt in range(1, max_retries + 1):
            try:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(None, pg0.start)
                # Patch pg_hba.conf for network access if listening beyond localhost
                if self._needs_pg_hba_patch():
                    await loop.run_in_executor(None, self._patch_pg_hba)
                # Get URI from pg0 (includes auto-assigned port)
                uri = info.uri
                logger.info(f"PostgreSQL started: {uri}")
                return uri
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries:
                    delay = retry_delay * (2 ** (attempt - 1))
                    logger.debug(f"pg0 start attempt {attempt}/{max_retries} failed: {last_error}")
                    logger.debug(f"Retrying in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.debug(f"pg0 start attempt {attempt}/{max_retries} failed: {last_error}")

        raise RuntimeError(
            f"Failed to start embedded PostgreSQL after {max_retries} attempts. Last error: {last_error}"
        )

    def _needs_pg_hba_patch(self) -> bool:
        """Check if pg_hba.conf needs patching for non-localhost access."""
        if self.pg_config is None:
            return False
        listen = self.pg_config.get("listen_addresses", "localhost")
        return listen not in ("localhost", "127.0.0.1", "::1")

    def _build_pg_hba_entry(self, existing_content: str) -> str:
        """Add network access entry to pg_hba.conf content if not present.

        Adds 'host all all 0.0.0.0/0 password' to allow connections from
        bridge network interfaces (required for Podman bridge networking).
        """
        network_entry = "host    all             all             0.0.0.0/0               scram-sha-256"
        if "0.0.0.0/0" in existing_content:
            return existing_content
        return existing_content.rstrip("\n") + "\n" + network_entry + "\n"

    def _patch_pg_hba(self) -> None:
        """Patch pg_hba.conf to allow connections from bridge networks.

        Finds the pg_hba.conf in the pg0 data directory and adds a network
        access entry. Then signals PostgreSQL to reload configuration.
        """
        data_dir = Path.home() / ".pg0" / "instances" / self.name / "data"
        hba_path = data_dir / "pg_hba.conf"
        if not hba_path.exists():
            logger.warning(f"pg_hba.conf not found at {hba_path}, skipping patch")
            return

        content = hba_path.read_text()
        new_content = self._build_pg_hba_entry(content)
        if new_content != content:
            hba_path.write_text(new_content)
            logger.info(f"Patched {hba_path} for network access")
            # Reload PostgreSQL to pick up pg_hba.conf changes via SIGHUP
            pg0 = self._get_pg0()
            try:
                info = pg0.info()
                if info.pid:
                    os.kill(info.pid, signal.SIGHUP)
                    logger.info(f"Sent SIGHUP to PostgreSQL (pid={info.pid}) for config reload")
                else:
                    logger.warning("PostgreSQL pid not found, pg_hba.conf changes require restart")
            except Exception as e:
                logger.warning(f"Failed to reload PostgreSQL config: {e}")

    async def stop(self) -> None:
        """Stop the PostgreSQL server."""
        pg0 = self._get_pg0()
        logger.info(f"Stopping embedded PostgreSQL (name: {self.name})...")

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, pg0.stop)
            logger.info("Embedded PostgreSQL stopped")
        except Exception as e:
            if "not running" in str(e).lower():
                return
            raise RuntimeError(f"Failed to stop PostgreSQL: {e}")

    async def get_uri(self) -> str:
        """Get the connection URI for the PostgreSQL server."""
        pg0 = self._get_pg0()
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, pg0.info)
        return info.uri

    async def is_running(self) -> bool:
        """Check if the PostgreSQL server is currently running."""
        try:
            pg0 = self._get_pg0()
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, pg0.info)
            return info is not None and info.running
        except Exception:
            return False

    async def ensure_running(self) -> str:
        """Ensure the PostgreSQL server is running, starting it if needed."""
        if await self.is_running():
            return await self.get_uri()
        return await self.start()


_default_instance: EmbeddedPostgres | None = None


def get_embedded_postgres() -> EmbeddedPostgres:
    """Get or create the default EmbeddedPostgres instance."""
    global _default_instance
    if _default_instance is None:
        _default_instance = EmbeddedPostgres()
    return _default_instance


async def start_embedded_postgres() -> str:
    """Quick start function for embedded PostgreSQL."""
    return await get_embedded_postgres().ensure_running()


async def stop_embedded_postgres() -> None:
    """Stop the default embedded PostgreSQL instance."""
    global _default_instance
    if _default_instance:
        await _default_instance.stop()


def parse_pg0_url(db_url: str) -> tuple[bool, str | None, int | None]:
    """
    Parse a database URL and check if it's a pg0:// embedded database URL.

    Supports:
    - "pg0" -> default instance "hindsight"
    - "pg0://instance-name" -> named instance
    - "pg0://instance-name:port" -> named instance with explicit port
    - Any other URL (e.g., postgresql://) -> not a pg0 URL

    Args:
        db_url: The database URL to parse

    Returns:
        Tuple of (is_pg0, instance_name, port)
        - is_pg0: True if this is a pg0 URL
        - instance_name: The instance name (or None if not pg0)
        - port: The explicit port (or None for auto-assign)
    """
    if db_url == "pg0":
        return True, "hindsight", None

    if db_url.startswith("pg0://"):
        url_part = db_url[6:]  # Remove "pg0://"
        if ":" in url_part:
            instance_name, port_str = url_part.rsplit(":", 1)
            return True, instance_name or "hindsight", int(port_str)
        else:
            return True, url_part or "hindsight", None

    return False, None, None


async def resolve_database_url(db_url: str) -> str:
    """
    Resolve a database URL, handling pg0:// embedded database URLs.

    If the URL is a pg0:// URL, starts the embedded PostgreSQL and returns
    the actual postgresql:// connection URL. Otherwise, returns the URL unchanged.

    Args:
        db_url: Database URL (pg0://, pg0, or postgresql://)

    Returns:
        The resolved postgresql:// connection URL
    """
    is_pg0, instance_name, port = parse_pg0_url(db_url)
    if is_pg0:
        pg0 = EmbeddedPostgres(name=instance_name, port=port)
        return await pg0.ensure_running()
    return db_url
