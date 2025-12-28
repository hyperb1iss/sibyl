"""Tests for CLI context management (config_store context functions)."""

import pytest

from sibyl.cli.config_store import (
    Context,
    create_context,
    delete_context,
    get_active_context,
    get_active_context_name,
    get_context,
    get_effective_project,
    get_effective_server_url,
    list_contexts,
    set_active_context,
    update_context,
)


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Isolate config to temp directory."""
    config_dir = tmp_path / ".sibyl"
    config_dir.mkdir()

    # Patch config_dir to return our temp directory
    monkeypatch.setattr(
        "sibyl.cli.config_store.config_dir",
        lambda: config_dir,
    )
    monkeypatch.setattr(
        "sibyl.cli.config_store.config_path",
        lambda: config_dir / "config.toml",
    )

    return config_dir


class TestContextModel:
    """Test Context dataclass."""

    def test_context_defaults(self):
        ctx = Context(name="test")
        assert ctx.name == "test"
        assert ctx.server_url == "http://localhost:3334"
        assert ctx.org_slug is None
        assert ctx.default_project is None

    def test_context_to_dict(self):
        ctx = Context(
            name="prod",
            server_url="https://prod.example.com",
            org_slug="acme",
            default_project="proj_123",
        )
        d = ctx.to_dict()
        assert d["server_url"] == "https://prod.example.com"
        assert d["org_slug"] == "acme"
        assert d["default_project"] == "proj_123"

    def test_context_from_dict(self):
        data = {
            "server_url": "https://staging.example.com",
            "org_slug": "beta",
            "default_project": "",
        }
        ctx = Context.from_dict("staging", data)
        assert ctx.name == "staging"
        assert ctx.server_url == "https://staging.example.com"
        assert ctx.org_slug == "beta"
        assert ctx.default_project is None  # Empty string becomes None

    def test_context_from_dict_missing_fields(self):
        ctx = Context.from_dict("minimal", {})
        assert ctx.name == "minimal"
        assert ctx.server_url == "http://localhost:3334"
        assert ctx.org_slug is None
        assert ctx.default_project is None


class TestContextCRUD:
    """Test context CRUD operations."""

    def test_create_context(self, isolated_config):
        ctx = create_context(
            name="local",
            server_url="http://localhost:3334",
            org_slug="dev",
        )
        assert ctx.name == "local"
        assert ctx.server_url == "http://localhost:3334"
        assert ctx.org_slug == "dev"

        # Verify persisted
        retrieved = get_context("local")
        assert retrieved is not None
        assert retrieved.name == "local"
        assert retrieved.org_slug == "dev"

    def test_create_context_duplicate_raises(self, isolated_config):
        create_context(name="dupe", server_url="http://localhost:3334")
        with pytest.raises(ValueError, match="already exists"):
            create_context(name="dupe", server_url="http://other.com")

    def test_create_context_set_active(self, isolated_config):
        create_context(
            name="auto-active",
            server_url="http://localhost:3334",
            set_active=True,
        )
        assert get_active_context_name() == "auto-active"

    def test_list_contexts_empty(self, isolated_config):
        contexts = list_contexts()
        assert contexts == []

    def test_list_contexts(self, isolated_config):
        create_context(name="a", server_url="http://a.com")
        create_context(name="b", server_url="http://b.com")

        contexts = list_contexts()
        names = {c.name for c in contexts}
        assert names == {"a", "b"}

    def test_get_context_not_found(self, isolated_config):
        assert get_context("nonexistent") is None

    def test_update_context(self, isolated_config):
        create_context(name="updatable", server_url="http://old.com")

        updated = update_context("updatable", server_url="http://new.com")
        assert updated.server_url == "http://new.com"

        # Verify persisted
        retrieved = get_context("updatable")
        assert retrieved.server_url == "http://new.com"

    def test_update_context_partial(self, isolated_config):
        create_context(
            name="partial",
            server_url="http://orig.com",
            org_slug="orig-org",
            default_project="orig-proj",
        )

        # Update only org
        updated = update_context("partial", org_slug="new-org")
        assert updated.server_url == "http://orig.com"  # Unchanged
        assert updated.org_slug == "new-org"  # Updated
        assert updated.default_project == "orig-proj"  # Unchanged

    def test_update_context_clear_optional_fields(self, isolated_config):
        create_context(
            name="clearable",
            server_url="http://x.com",
            org_slug="org",
            default_project="proj",
        )

        # Clear org and project by setting to None
        updated = update_context("clearable", org_slug=None, default_project=None)
        assert updated.org_slug is None
        assert updated.default_project is None

    def test_update_context_not_found(self, isolated_config):
        with pytest.raises(ValueError, match="not found"):
            update_context("ghost", server_url="http://x.com")

    def test_delete_context(self, isolated_config):
        create_context(name="deleteme", server_url="http://x.com")
        assert get_context("deleteme") is not None

        deleted = delete_context("deleteme")
        assert deleted is True
        assert get_context("deleteme") is None

    def test_delete_context_not_found(self, isolated_config):
        deleted = delete_context("nonexistent")
        assert deleted is False

    def test_delete_active_context_clears_active(self, isolated_config):
        create_context(name="active-delete", server_url="http://x.com", set_active=True)
        assert get_active_context_name() == "active-delete"

        delete_context("active-delete")
        assert get_active_context_name() is None


class TestActiveContext:
    """Test active context management."""

    def test_get_active_context_none(self, isolated_config):
        assert get_active_context_name() is None
        assert get_active_context() is None

    def test_set_active_context(self, isolated_config):
        create_context(name="ctx1", server_url="http://1.com")
        create_context(name="ctx2", server_url="http://2.com")

        set_active_context("ctx1")
        assert get_active_context_name() == "ctx1"
        assert get_active_context().name == "ctx1"

        set_active_context("ctx2")
        assert get_active_context_name() == "ctx2"
        assert get_active_context().server_url == "http://2.com"

    def test_clear_active_context(self, isolated_config):
        create_context(name="temp", server_url="http://x.com", set_active=True)
        assert get_active_context_name() == "temp"

        set_active_context(None)
        assert get_active_context_name() is None


class TestEffectiveSettings:
    """Test effective setting resolution."""

    def test_effective_server_url_no_context(self, isolated_config):
        # Falls back to legacy config (localhost default)
        url = get_effective_server_url()
        assert url == "http://localhost:3334"

    def test_effective_server_url_with_context(self, isolated_config):
        create_context(
            name="custom",
            server_url="https://custom.example.com",
            set_active=True,
        )
        url = get_effective_server_url()
        assert url == "https://custom.example.com"

    def test_effective_project_no_context(self, isolated_config):
        # No context, no path mapping, no default
        proj = get_effective_project()
        assert proj is None

    def test_effective_project_from_context(self, isolated_config):
        create_context(
            name="with-proj",
            server_url="http://x.com",
            default_project="proj_from_ctx",
            set_active=True,
        )
        proj = get_effective_project()
        assert proj == "proj_from_ctx"


class TestContextOverride:
    """Test global context override (--context flag / SIBYL_CONTEXT env)."""

    def test_context_override_flag(self, monkeypatch):
        """Test that set_context_override sets the override."""
        from sibyl.cli import state

        # Clear any existing override
        state.clear_context_override()
        assert state.get_context_override() is None

        # Set override
        state.set_context_override("prod")
        assert state.get_context_override() == "prod"

        # Clear again
        state.clear_context_override()
        assert state.get_context_override() is None

    def test_context_override_env_var(self, monkeypatch):
        """Test that SIBYL_CONTEXT env var is used as fallback."""
        from sibyl.cli import state

        # Clear any existing override
        state.clear_context_override()

        # Set env var
        monkeypatch.setenv("SIBYL_CONTEXT", "staging")
        assert state.get_context_override() == "staging"

        # Clear env var
        monkeypatch.delenv("SIBYL_CONTEXT", raising=False)
        assert state.get_context_override() is None

    def test_context_override_priority(self, monkeypatch):
        """Test that explicit flag takes precedence over env var."""
        from sibyl.cli import state

        # Set env var
        monkeypatch.setenv("SIBYL_CONTEXT", "from-env")

        # Set explicit override
        state.set_context_override("from-flag")

        # Flag should take precedence
        assert state.get_context_override() == "from-flag"

        # Clear flag, env should be used
        state.clear_context_override()
        assert state.get_context_override() == "from-env"


class TestClientContextAwareness:
    """Test that get_client() respects context settings."""

    def test_get_client_uses_override(self, isolated_config, monkeypatch):
        """Test get_client uses context override when set."""
        from sibyl.cli import state
        from sibyl.cli.client import _get_default_api_url, clear_client_cache

        # Create contexts
        create_context(
            name="local",
            server_url="http://localhost:3334",
            set_active=True,
        )
        create_context(
            name="prod",
            server_url="https://prod.sibyl.example.com",
        )

        clear_client_cache()

        # Default uses active context
        url = _get_default_api_url()
        assert "localhost" in url

        # Set override to prod
        state.set_context_override("prod")
        url = _get_default_api_url(context_name="prod")
        assert "prod.sibyl" in url

        # Cleanup
        state.clear_context_override()
        clear_client_cache()
