"""Tests for pg0 network configuration support (F-016 bridge networking).

These tests verify that EmbeddedPostgres can be configured to listen on
non-localhost addresses and that pg_hba.conf is patched to allow connections
from bridge networks. This is required for Podman bridge networking where
published ports arrive on the container's bridge interface, not localhost.

Red phase: All tests should fail until production code is updated.
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from hindsight_api.pg0 import EmbeddedPostgres, parse_pg0_url


class TestEmbeddedPostgresPgConfig:
    """EmbeddedPostgres must forward pg_config to the underlying Pg0 instance."""

    def test_pg_config_stored_on_init(self):
        """pg_config dict is stored when provided to constructor."""
        pg_config = {"listen_addresses": "0.0.0.0", "shared_buffers": "256MB"}
        ep = EmbeddedPostgres(pg_config=pg_config)
        assert ep.pg_config == pg_config

    def test_pg_config_defaults_to_none(self):
        """pg_config defaults to None when not provided."""
        ep = EmbeddedPostgres()
        assert ep.pg_config is None

    def test_pg_config_forwarded_to_pg0(self):
        """pg_config dict is passed to Pg0(config=...) in _get_pg0()."""
        pg_config = {"listen_addresses": "0.0.0.0"}
        ep = EmbeddedPostgres(name="test", pg_config=pg_config)

        with patch("hindsight_api.pg0.Pg0") as mock_pg0_class:
            ep._get_pg0()
            mock_pg0_class.assert_called_once()
            call_kwargs = mock_pg0_class.call_args
            assert call_kwargs.kwargs.get("config") == pg_config or \
                   (len(call_kwargs.args) == 0 and "config" in dict(call_kwargs.kwargs) and
                    call_kwargs.kwargs["config"] == pg_config)

    def test_pg_config_none_not_forwarded(self):
        """When pg_config is None, config is not passed to Pg0()."""
        ep = EmbeddedPostgres(name="test")

        with patch("hindsight_api.pg0.Pg0") as mock_pg0_class:
            ep._get_pg0()
            call_kwargs = mock_pg0_class.call_args.kwargs
            assert "config" not in call_kwargs

    def test_pg_config_combined_with_port(self):
        """pg_config works alongside explicit port configuration."""
        pg_config = {"listen_addresses": "0.0.0.0"}
        ep = EmbeddedPostgres(name="test", port=5434, pg_config=pg_config)

        with patch("hindsight_api.pg0.Pg0") as mock_pg0_class:
            ep._get_pg0()
            call_kwargs = mock_pg0_class.call_args.kwargs
            assert call_kwargs["port"] == 5434
            assert call_kwargs["config"] == pg_config


class TestEmbeddedPostgresPgHba:
    """EmbeddedPostgres must patch pg_hba.conf after start when listening
    on non-localhost addresses, to allow connections from bridge networks."""

    @pytest.mark.asyncio
    async def test_patch_pg_hba_called_after_start(self):
        """_patch_pg_hba is called after successful pg0 start when
        listen_addresses is not localhost."""
        pg_config = {"listen_addresses": "0.0.0.0"}
        ep = EmbeddedPostgres(name="test", pg_config=pg_config)

        mock_pg0 = MagicMock()
        mock_info = MagicMock()
        mock_info.uri = "postgresql://hindsight:hindsight@localhost:5434/hindsight"
        mock_pg0.start.return_value = mock_info

        with patch("hindsight_api.pg0.Pg0", return_value=mock_pg0), \
             patch.object(ep, "_patch_pg_hba") as mock_patch:
            await ep.start()
            mock_patch.assert_called_once()

    @pytest.mark.asyncio
    async def test_patch_pg_hba_not_called_for_localhost(self):
        """_patch_pg_hba is NOT called when listen_addresses is localhost
        (default behavior, no bridge networking)."""
        ep = EmbeddedPostgres(name="test")

        mock_pg0 = MagicMock()
        mock_info = MagicMock()
        mock_info.uri = "postgresql://hindsight:hindsight@localhost:5434/hindsight"
        mock_pg0.start.return_value = mock_info

        with patch("hindsight_api.pg0.Pg0", return_value=mock_pg0), \
             patch.object(ep, "_patch_pg_hba") as mock_patch:
            await ep.start()
            mock_patch.assert_not_called()

    def test_patch_pg_hba_adds_network_entry(self):
        """_patch_pg_hba adds 'host all all 0.0.0.0/0 scram-sha-256' to pg_hba.conf
        if it doesn't already exist."""
        ep = EmbeddedPostgres(name="test", pg_config={"listen_addresses": "0.0.0.0"})

        # Simulate existing pg_hba.conf content (localhost-only)
        existing_hba = (
            "local   all   all   trust\n"
            "host    all   all   127.0.0.1/32   trust\n"
            "host    all   all   ::1/128        trust\n"
        )

        result = ep._build_pg_hba_entry(existing_hba)
        assert "host    all             all             0.0.0.0/0               scram-sha-256" in result

    def test_patch_pg_hba_idempotent(self):
        """_build_pg_hba_entry is idempotent — doesn't add duplicate entries."""
        ep = EmbeddedPostgres(name="test", pg_config={"listen_addresses": "0.0.0.0"})

        existing_hba = (
            "local   all   all   password\n"
            "host    all   all   127.0.0.1/32   password\n"
            "host    all   all   0.0.0.0/0      password\n"
        )

        result = ep._build_pg_hba_entry(existing_hba)
        # Count occurrences of 0.0.0.0/0
        count = result.count("0.0.0.0/0")
        assert count == 1

    def test_patch_pg_hba_sends_sighup(self):
        """_patch_pg_hba sends SIGHUP to PostgreSQL to reload config."""
        import signal
        import tempfile
        from pathlib import Path as RealPath

        ep = EmbeddedPostgres(name="test", pg_config={"listen_addresses": "0.0.0.0"})

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create the directory structure pg0.py expects
            pg0_base = RealPath(tmpdir) / ".pg0" / "instances" / "test" / "data"
            pg0_base.mkdir(parents=True)
            hba_path = pg0_base / "pg_hba.conf"
            hba_path.write_text(
                "local   all   all   password\n"
                "host    all   all   127.0.0.1/32   password\n"
            )

            mock_pg0 = MagicMock()
            mock_info = MagicMock()
            mock_info.pid = 42
            mock_pg0.info.return_value = mock_info

            with patch("hindsight_api.pg0.Path.home", return_value=RealPath(tmpdir)), \
                 patch("hindsight_api.pg0.os.kill") as mock_kill, \
                 patch.object(ep, "_get_pg0", return_value=mock_pg0):
                ep._patch_pg_hba()

            mock_kill.assert_called_once_with(42, signal.SIGHUP)
            assert "0.0.0.0/0" in hba_path.read_text()


class TestHindsightConfigPg0ListenAddresses:
    """HindsightConfig must support HINDSIGHT_API_PG0_LISTEN_ADDRESSES env var."""

    def test_pg0_listen_addresses_from_env(self):
        """Config reads pg0_listen_addresses from environment variable."""
        from hindsight_api.config import HindsightConfig
        with patch.dict("os.environ", {"HINDSIGHT_API_PG0_LISTEN_ADDRESSES": "0.0.0.0"}, clear=False):
            config = HindsightConfig.from_env()
            assert config.pg0_listen_addresses == "0.0.0.0"

    def test_pg0_listen_addresses_default_localhost(self):
        """pg0_listen_addresses defaults to 'localhost' when env var is not set."""
        from hindsight_api.config import HindsightConfig
        with patch.dict("os.environ", {}, clear=False):
            # Remove the env var if it exists
            env = dict(os.environ)
            env.pop("HINDSIGHT_API_PG0_LISTEN_ADDRESSES", None)
            with patch.dict("os.environ", env, clear=True):
                config = HindsightConfig.from_env()
                assert config.pg0_listen_addresses == "localhost"


class TestMemoryEnginePg0Config:
    """MemoryEngine must forward pg0_listen_addresses to EmbeddedPostgres."""

    @pytest.mark.asyncio
    async def test_pg0_config_passed_when_listen_addresses_set(self):
        """When config has pg0_listen_addresses != 'localhost', the production
        start_pg0() in initialize() passes pg_config to EmbeddedPostgres."""
        from hindsight_api.config import HindsightConfig

        with patch("hindsight_api.engine.memory_engine.EmbeddedPostgres") as MockEP, \
             patch("hindsight_api.engine.memory_engine.get_config") as mock_get_config:
            mock_cfg = MagicMock(spec=HindsightConfig)
            mock_cfg.pg0_listen_addresses = "0.0.0.0"
            mock_cfg.pg0_password = "hindsight"
            mock_cfg.skip_llm_verification = True
            mock_cfg.lazy_reranker = True
            mock_cfg.llm_provider = "mock"
            mock_cfg.llm_api_key = "dummy"
            mock_cfg.llm_model = "test"
            mock_cfg.get_llm_base_url.return_value = None
            mock_get_config.return_value = mock_cfg

            mock_ep_instance = MagicMock()
            mock_ep_instance.is_running = AsyncMock(return_value=False)
            mock_ep_instance.ensure_running = AsyncMock(
                return_value="postgresql://hindsight:hindsight@localhost:5434/hindsight"
            )
            MockEP.return_value = mock_ep_instance

            from hindsight_api.engine.memory_engine import MemoryEngine

            # Set up minimal engine state to test start_pg0() production path
            engine = MemoryEngine.__new__(MemoryEngine)
            engine._use_pg0 = True
            engine._pg0_instance_name = "shared"
            engine._pg0_port = 5434
            engine._pg0 = None
            engine._initialized = False
            engine.db_url = None
            engine._skip_llm_verification = True
            engine._lazy_reranker = True
            engine._run_migrations = False
            engine.embeddings = MagicMock()
            engine.embeddings.provider_name = "mock"
            engine.embeddings.initialize = AsyncMock()
            engine.query_analyzer = MagicMock()
            engine.query_analyzer.load = MagicMock()

            # Call initialize() — this runs the actual start_pg0() production code
            # initialize() will fail later (pool creation) since we only set up
            # enough state for start_pg0(). That's fine — we catch it.
            try:
                await engine.initialize()
            except (AttributeError, Exception):
                pass  # Expected — we only mock what start_pg0 needs

            # Verify EmbeddedPostgres was constructed with pg_config
            MockEP.assert_called_once()
            call_kwargs = MockEP.call_args.kwargs
            assert "pg_config" in call_kwargs
            assert call_kwargs["pg_config"]["listen_addresses"] == "0.0.0.0"


import os
