"""Offline tests for the unified MCP error contract (Issue #4).

Asserts that every domain error surfaces as a structured ``McpError`` with a
distinct, documented code before it crosses the tool boundary.  No network
access; no real wiki content.

Error-code reference
--------------------
PAGE_NOT_FOUND = 1   page does not exist
AUTH_ERROR     = 2   authentication failed
FETCH_ERROR    = 3   network / API failure after retries
CONFIG_ERROR   = 4   missing / invalid configuration
INVALID_PARAMS = -32602  (pagination layer; bad cursor)
"""

from __future__ import annotations

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS

from wg21_wiki_mcp.errors import (
    AUTH_ERROR,
    CONFIG_ERROR,
    FETCH_ERROR,
    PAGE_NOT_FOUND,
    AuthError,
    ConfigError,
    FetchError,
    PageNotFound,
    to_mcp_error,
)
from wg21_wiki_mcp.pagination import decode_cursor

# ---------------------------------------------------------------------------
# to_mcp_error unit tests
# ---------------------------------------------------------------------------


class TestToMcpError:
    """Unit tests for ``to_mcp_error()`` — one test per exception type and edge case."""

    def test_page_not_found_maps_to_code_1(self):
        exc = PageNotFound("Page not found: 'Ghost'")
        err = to_mcp_error(exc)
        assert isinstance(err, McpError)
        assert err.error.code == PAGE_NOT_FOUND

    def test_page_not_found_message_contains_title(self):
        exc = PageNotFound("Page not found: 'Ghost'")
        err = to_mcp_error(exc)
        assert "Ghost" in err.error.message

    def test_auth_error_maps_to_code_2(self):
        exc = AuthError("All configured credential paths failed: bot: LoginError: bad")
        err = to_mcp_error(exc)
        assert err.error.code == AUTH_ERROR

    def test_auth_error_message_contains_no_credential_detail(self):
        secret = "supersecretpassword"
        exc = AuthError(f"failed: {secret}")
        err = to_mcp_error(exc)
        # The fixed message must NOT reflect the original exception string.
        assert secret not in err.error.message

    def test_fetch_error_maps_to_code_3(self):
        exc = FetchError("API call 'query' failed after 6 retries: ConnectionError")
        err = to_mcp_error(exc)
        assert err.error.code == FETCH_ERROR

    def test_config_error_maps_to_code_4(self):
        exc = ConfigError("No credentials configured: set WIKI_BOT_USERNAME/WIKI_BOT_PASSWORD")
        err = to_mcp_error(exc)
        assert err.error.code == CONFIG_ERROR

    def test_config_error_message_names_env_vars(self):
        exc = ConfigError("No credentials configured: set WIKI_BOT_USERNAME/WIKI_BOT_PASSWORD")
        err = to_mcp_error(exc)
        assert "WIKI_BOT_USERNAME" in err.error.message

    def test_existing_mcp_error_passes_through_unchanged(self):
        from mcp.types import ErrorData

        original = McpError(ErrorData(code=INVALID_PARAMS, message="bad cursor"))
        result = to_mcp_error(original)
        assert result is original

    def test_raw_api_error_maps_to_fetch_error_code(self):
        try:
            from mwclient.errors import APIError

            # APIError(info, code, args) is the constructor signature.
            exc = APIError("protectedpage", "protectedpage", [])
            err = to_mcp_error(exc)
            assert err.error.code == FETCH_ERROR
            assert "protectedpage" in err.error.message
        except ImportError:
            pytest.skip("mwclient not installed")

    def test_unknown_exception_maps_to_fetch_error_code(self):
        exc = RuntimeError("something unexpected")
        err = to_mcp_error(exc)
        assert err.error.code == FETCH_ERROR


# ---------------------------------------------------------------------------
# Missing-page error shape through tool layer
# ---------------------------------------------------------------------------


class TestMissingPageThroughToolBoundary:
    """Verify PageNotFound reaches the transport as McpError(PAGE_NOT_FOUND)."""

    def test_get_page_missing_raises_mcp_error(self, fake_client, make_ctx):
        from wg21_wiki_mcp import tools

        ctx = make_ctx(fake_client)  # no pages registered → page is missing
        with pytest.raises(McpError) as exc_info:
            try:
                tools.get_page(ctx, "NonExistent")
            except PageNotFound as e:
                raise to_mcp_error(e) from e
        assert exc_info.value.error.code == PAGE_NOT_FOUND

    def test_server_wrap_converts_page_not_found(self, fake_client, make_ctx):
        """_wrap() in server.py should convert PageNotFound to McpError."""
        from wg21_wiki_mcp import tools
        from wg21_wiki_mcp.server import _wrap

        ctx = make_ctx(fake_client)
        with pytest.raises(McpError) as exc_info:
            _wrap(tools.get_page, ctx, "NonExistent")
        assert exc_info.value.error.code == PAGE_NOT_FOUND

    def test_section_not_found_raises_mcp_error(self, fake_client, make_ctx):
        from wg21_wiki_mcp import tools
        from wg21_wiki_mcp.server import _wrap

        ctx = make_ctx(fake_client)
        with pytest.raises(McpError) as exc_info:
            _wrap(tools.get_page, ctx, "NonExistent", section=1)
        assert exc_info.value.error.code == PAGE_NOT_FOUND


# ---------------------------------------------------------------------------
# Bad-cursor error shape (INVALID_PARAMS)
# ---------------------------------------------------------------------------


class TestBadCursorErrorShape:
    """Verify malformed or expired pagination cursors raise McpError(INVALID_PARAMS)."""

    def test_malformed_cursor_raises_invalid_params(self):
        with pytest.raises(McpError) as exc_info:
            decode_cursor("not-valid-base64!!!")
        assert exc_info.value.error.code == INVALID_PARAMS

    def test_truncated_cursor_raises_invalid_params(self):
        with pytest.raises(McpError) as exc_info:
            decode_cursor("YQ==")  # valid base64 but decodes to "a", not a JSON dict
        assert exc_info.value.error.code == INVALID_PARAMS

    def test_empty_string_cursor_raises_invalid_params(self):
        with pytest.raises(McpError):
            decode_cursor("")

    def test_none_cursor_returns_empty_dict(self):
        assert decode_cursor(None) == {}


# ---------------------------------------------------------------------------
# Config-without-credentials error shape
# ---------------------------------------------------------------------------


class TestConfigErrorShape:
    """Verify missing credentials raise ConfigError that maps to McpError(CONFIG_ERROR)."""

    def test_config_from_env_raises_config_error_without_credentials(self, monkeypatch):
        """Config.from_env() raises ConfigError when no credentials are set."""
        from wg21_wiki_mcp.config import Config

        for var in (
            "WIKI_BOT_USERNAME",
            "WIKI_BOT_PASSWORD",
            "WIKI_USER_USERNAME",
            "WIKI_USER_PASSWORD",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr("wg21_wiki_mcp.config._load_dotenv", None, raising=False)

        with pytest.raises(ConfigError):
            Config.from_env(load_env_file=False)

    def test_config_error_maps_to_code_4(self, monkeypatch):
        """Config.from_env() raises ConfigError that maps to error code 4."""
        from wg21_wiki_mcp.config import Config

        for var in (
            "WIKI_BOT_USERNAME",
            "WIKI_BOT_PASSWORD",
            "WIKI_USER_USERNAME",
            "WIKI_USER_PASSWORD",
        ):
            monkeypatch.delenv(var, raising=False)

        with pytest.raises(ConfigError) as exc_info:
            Config.from_env(load_env_file=False)
        assert to_mcp_error(exc_info.value).error.code == CONFIG_ERROR


# ---------------------------------------------------------------------------
# Simulated auth-failure error shape
# ---------------------------------------------------------------------------


class TestAuthFailureErrorShape:
    """Verify AuthError maps to McpError(AUTH_ERROR) with a sanitized, fixed message."""

    def test_auth_error_maps_to_code_2(self):
        exc = AuthError("All configured credential paths failed: bot: LoginError: bad")
        err = to_mcp_error(exc)
        assert err.error.code == AUTH_ERROR

    def test_auth_error_message_is_safe(self):
        """The McpError message must not reproduce the original exception text."""
        sensitive = "my-wiki-bot-password-value"
        exc = AuthError(f"login failed with {sensitive}")
        err = to_mcp_error(exc)
        assert sensitive not in err.error.message

    def test_server_wrap_converts_auth_error(self, fake_client, make_ctx):
        """_wrap() in server.py converts AuthError raised inside a tool."""
        from wg21_wiki_mcp.server import _wrap

        def _raise_auth():
            raise AuthError("simulated auth lapse")

        with pytest.raises(McpError) as exc_info:
            _wrap(_raise_auth)
        assert exc_info.value.error.code == AUTH_ERROR

    def test_exhausted_auth_retries_emit_code_2_through_wrap(self, tmp_path, monkeypatch):
        """wiki_client.api() exhausting auth retries → AuthError → _wrap() → code 2.

        Simulates _MAX_RETRIES consecutive readapidenied responses so that the
        retry loop exits with an auth-class APIError as last_exc; the resulting
        AuthError must reach _wrap() and be mapped to AUTH_ERROR (code 2), not
        FETCH_ERROR (code 3).
        """
        import types

        from mwclient.errors import APIError

        from wg21_wiki_mcp import wiki_client as wc
        from wg21_wiki_mcp.config import Config, Credentials
        from wg21_wiki_mcp.server import _wrap

        monkeypatch.setattr(wc.time, "sleep", lambda *_a, **_k: None)

        class AlwaysAuthDeniedSite:
            connection = types.SimpleNamespace(cookies={})

            def login(self, _u, _p):
                pass

            def api(self, action, **params):
                if params.get("meta") == "userinfo":
                    return {"query": {"userinfo": {"name": "Bot"}}}
                raise APIError("readapidenied", "denied", [])

        cfg = Config(
            base_url="https://w.example",
            bot=Credentials("bot", "Bot@bot", "secret"),
            user=None,
            cache_dir=tmp_path / "c",
        )
        client = wc.WikiClient(cfg)
        monkeypatch.setattr(client, "_new_site", lambda: AlwaysAuthDeniedSite())
        client.login()

        with pytest.raises(McpError) as exc_info:
            _wrap(client.api, "query", titles="SomePage")
        assert exc_info.value.error.code == AUTH_ERROR
