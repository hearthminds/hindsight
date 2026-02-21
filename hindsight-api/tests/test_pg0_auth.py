"""Tests for pg0 authentication hardening (F-016 Phase 4).

These tests verify:
1. HINDSIGHT_API_PG0_PASSWORD env var is read by HindsightConfig
2. Default pg0 password remains "hindsight" for backward compatibility
3. MemoryEngine passes password through to EmbeddedPostgres constructor
4. pg_hba.conf uses scram-sha-256 instead of cleartext password auth

Red phase: All tests should fail until production code is updated.
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hindsight_api.pg0 import EmbeddedPostgres


# ---------------------------------------------------------------------------
# HindsightConfig: pg0_password field
# ---------------------------------------------------------------------------


class TestHindsightConfigPg0Password:
    """HindsightConfig must support HINDSIGHT_API_PG0_PASSWORD env var."""

    def test_pg0_password_from_env(self):
        """Config reads pg0_password from HINDSIGHT_API_PG0_PASSWORD."""
        from hindsight_api.config import HindsightConfig

        with patch.dict(
            "os.environ",
            {"HINDSIGHT_API_PG0_PASSWORD": "s3cret-pg-pass"},
            clear=False,
        ):
            config = HindsightConfig.from_env()
            assert config.pg0_password == "s3cret-pg-pass"

    def test_pg0_password_default_is_hindsight(self):
        """pg0_password defaults to 'hindsight' when env var is not set."""
        from hindsight_api.config import HindsightConfig

        env = dict(os.environ)
        env.pop("HINDSIGHT_API_PG0_PASSWORD", None)
        with patch.dict("os.environ", env, clear=True):
            config = HindsightConfig.from_env()
            assert config.pg0_password == "hindsight"


# ---------------------------------------------------------------------------
# EmbeddedPostgres: password propagation
# ---------------------------------------------------------------------------


class TestEmbeddedPostgresPassword:
    """EmbeddedPostgres must forward custom password to Pg0."""

    def test_custom_password_stored(self):
        """Custom password is stored on EmbeddedPostgres instance."""
        ep = EmbeddedPostgres(password="my-custom-pw")
        assert ep.password == "my-custom-pw"

    def test_default_password_is_hindsight(self):
        """Default password remains 'hindsight' for backward compat."""
        ep = EmbeddedPostgres()
        assert ep.password == "hindsight"

    def test_custom_password_forwarded_to_pg0(self):
        """Custom password is passed to Pg0() constructor."""
        ep = EmbeddedPostgres(name="test", password="unique-pass-123")

        with patch("hindsight_api.pg0.Pg0") as mock_pg0_class:
            ep._get_pg0()
            call_kwargs = mock_pg0_class.call_args.kwargs
            assert call_kwargs["password"] == "unique-pass-123"


# ---------------------------------------------------------------------------
# MemoryEngine: password passthrough to EmbeddedPostgres
# ---------------------------------------------------------------------------


class TestMemoryEnginePg0Password:
    """MemoryEngine must forward pg0_password from config to EmbeddedPostgres."""

    @pytest.mark.asyncio
    async def test_pg0_password_passed_to_embedded_postgres(self):
        """When config has pg0_password, it is passed to EmbeddedPostgres(password=...)."""
        from hindsight_api.config import HindsightConfig

        with (
            patch("hindsight_api.engine.memory_engine.EmbeddedPostgres") as MockEP,
            patch("hindsight_api.engine.memory_engine.get_config") as mock_get_config,
        ):
            mock_cfg = MagicMock(spec=HindsightConfig)
            mock_cfg.pg0_listen_addresses = "localhost"
            mock_cfg.pg0_password = "super-secret-pw"
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
                return_value="postgresql://hindsight:super-secret-pw@localhost:5434/hindsight"
            )
            MockEP.return_value = mock_ep_instance

            from hindsight_api.engine.memory_engine import MemoryEngine

            engine = MemoryEngine.__new__(MemoryEngine)
            engine._use_pg0 = True
            engine._pg0_instance_name = "aletheia"
            engine._pg0_port = 5433
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

            try:
                await engine.initialize()
            except (AttributeError, Exception):
                pass  # Expected — we only mock what start_pg0 needs

            # Verify EmbeddedPostgres was constructed with password
            MockEP.assert_called_once()
            call_kwargs = MockEP.call_args.kwargs
            assert "password" in call_kwargs
            assert call_kwargs["password"] == "super-secret-pw"

    @pytest.mark.asyncio
    async def test_pg0_default_password_not_passed_explicitly(self):
        """When config has default pg0_password ('hindsight'), password kwarg
        is still passed but with the default value — no silent omission."""
        from hindsight_api.config import HindsightConfig

        with (
            patch("hindsight_api.engine.memory_engine.EmbeddedPostgres") as MockEP,
            patch("hindsight_api.engine.memory_engine.get_config") as mock_get_config,
        ):
            mock_cfg = MagicMock(spec=HindsightConfig)
            mock_cfg.pg0_listen_addresses = "localhost"
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

            engine = MemoryEngine.__new__(MemoryEngine)
            engine._use_pg0 = True
            engine._pg0_instance_name = "logos"
            engine._pg0_port = 5432
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

            try:
                await engine.initialize()
            except (AttributeError, Exception):
                pass

            MockEP.assert_called_once()
            call_kwargs = MockEP.call_args.kwargs
            # Password should always be passed, even when it's the default
            assert "password" in call_kwargs
            assert call_kwargs["password"] == "hindsight"


# ---------------------------------------------------------------------------
# pg_hba auth method: scram-sha-256 instead of cleartext password
# ---------------------------------------------------------------------------


class TestPgHbaScramAuth:
    """pg_hba.conf must use scram-sha-256 instead of cleartext 'password'."""

    def test_build_pg_hba_entry_uses_scram(self):
        """_build_pg_hba_entry uses scram-sha-256, not cleartext password."""
        ep = EmbeddedPostgres(
            name="test", pg_config={"listen_addresses": "0.0.0.0"}
        )

        existing_hba = (
            "local   all   all   trust\n"
            "host    all   all   127.0.0.1/32   trust\n"
        )

        result = ep._build_pg_hba_entry(existing_hba)
        assert "scram-sha-256" in result
        # The network entry must NOT use cleartext 'password' auth
        for line in result.splitlines():
            if "0.0.0.0/0" in line:
                assert "scram-sha-256" in line
                # Ensure it's not using the old cleartext method
                # (can't just check 'password' not in line since
                # 'scram-sha-256' doesn't contain 'password')
                parts = line.split()
                assert parts[-1] == "scram-sha-256"

    def test_scram_entry_idempotent(self):
        """Adding scram entry is idempotent — doesn't duplicate."""
        ep = EmbeddedPostgres(
            name="test", pg_config={"listen_addresses": "0.0.0.0"}
        )

        # Already has scram entry
        existing_hba = (
            "local   all   all   trust\n"
            "host    all             all             0.0.0.0/0               scram-sha-256\n"
        )

        result = ep._build_pg_hba_entry(existing_hba)
        count = result.count("0.0.0.0/0")
        assert count == 1

    def test_patch_pg_hba_writes_scram_to_file(self):
        """_patch_pg_hba writes scram-sha-256 entries to the actual pg_hba.conf file."""
        ep = EmbeddedPostgres(
            name="test-scram", pg_config={"listen_addresses": "0.0.0.0"}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            pg0_base = Path(tmpdir) / ".pg0" / "instances" / "test-scram" / "data"
            pg0_base.mkdir(parents=True)
            hba_path = pg0_base / "pg_hba.conf"
            hba_path.write_text(
                "local   all   all   trust\n"
                "host    all   all   127.0.0.1/32   trust\n"
            )

            mock_pg0 = MagicMock()
            mock_info = MagicMock()
            mock_info.pid = 99
            mock_pg0.info.return_value = mock_info

            with (
                patch("hindsight_api.pg0.Path.home", return_value=Path(tmpdir)),
                patch("hindsight_api.pg0.os.kill"),
                patch.object(ep, "_get_pg0", return_value=mock_pg0),
            ):
                ep._patch_pg_hba()

            content = hba_path.read_text()
            assert "scram-sha-256" in content
            # Ensure no cleartext 'password' auth for network entries
            for line in content.splitlines():
                if "0.0.0.0/0" in line:
                    parts = line.split()
                    assert parts[-1] == "scram-sha-256"
