"""Tests for log redaction and auth error message safety (Issue #3)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from conftest import FakePage, FakeWikiClient
from filelock import Timeout
from mwclient.errors import LoginError

from wg21_wiki_mcp import wiki_client as wc
from wg21_wiki_mcp.cache import Cache
from wg21_wiki_mcp.config import Config, Credentials
from wg21_wiki_mcp.context import ServerContext
from wg21_wiki_mcp.errors import AUTH_ERROR, AuthError, to_mcp_error
from wg21_wiki_mcp.fetch import PageFetcher
from wg21_wiki_mcp.log import get_logger
from wg21_wiki_mcp.log_safety import (
    AUTH_FAILURE_MESSAGE,
    LogSafetyFilter,
    auth_error_mcp_message,
    auth_path_failure_label,
    clear_redactions,
    is_safe_auth_message,
    register_config_secrets,
    register_redactions,
    safe_exception_summary,
    sanitize_text,
    summarize_auth_failures,
)
from wg21_wiki_mcp.models import AuthError as AuthErrorModel


@pytest.fixture(autouse=True)
def _clean_redactions():
    clear_redactions()
    yield
    clear_redactions()


class TestSanitizeText:
    def test_redacts_registered_secret(self):
        register_redactions("supersecretpassword")
        assert "supersecretpassword" not in sanitize_text("login failed: supersecretpassword")
        assert "[REDACTED]" in sanitize_text("login failed: supersecretpassword")

    def test_redacts_registered_wiki_content(self):
        content = "CONFIDENTIAL committee wikitext body that must not leak"
        register_redactions(content)
        assert content not in sanitize_text(f"accidental log: {content}")
        assert "[REDACTED]" in sanitize_text(f"accidental log: {content}")

    def test_redacts_credential_patterns(self):
        out = sanitize_text("Auth failed password=MySecret123 token=abc")
        assert "MySecret123" not in out
        assert "abc" not in out
        assert out.count("[REDACTED]") >= 2
        bearer = sanitize_text("Authorization: Bearer mytoken123")
        assert "mytoken123" not in bearer
        assert "[REDACTED]" in bearer

    def test_ignores_short_secrets(self):
        register_redactions("abc")
        assert sanitize_text("abc") == "abc"

    def test_session_reauth_message_requires_full_format(self):
        assert is_safe_auth_message("Session could not be re-established after 6 attempts.")
        assert not is_safe_auth_message("Session could not be re-established after 6 attempts. leaked-secret")

    def test_empty_text_is_unchanged(self):
        assert sanitize_text("") == ""

    def test_known_safe_auth_messages(self):
        assert is_safe_auth_message(AUTH_FAILURE_MESSAGE)
        assert is_safe_auth_message("SAML login failed (no SAMLResponse; check credentials/MFA).")
        assert is_safe_auth_message("Authentication failed (bot: LoginError); verify wiki credentials.")

    def test_summarize_auth_failures_empty_paths(self):
        assert summarize_auth_failures([]) == AUTH_FAILURE_MESSAGE

    def test_safe_exception_summary_empty_message(self):
        assert safe_exception_summary(RuntimeError()) == "RuntimeError"

    def test_auth_error_mcp_message_passes_safe_message(self):
        safe = summarize_auth_failures(["bot: LoginError"])
        assert auth_error_mcp_message(AuthErrorModel(safe)) == safe

    def test_register_config_secrets(self, tmp_path):
        config = Config(
            base_url="https://w.example",
            bot=Credentials("bot", "b", "bot-password-value"),
            user=Credentials("user", "u", "user-password-value"),
            cache_dir=tmp_path / "c",
        )
        register_config_secrets(config)
        assert "bot-password-value" not in sanitize_text("leak bot-password-value")
        assert "user-password-value" not in sanitize_text("leak user-password-value")

    def test_server_context_create_registers_secrets(self, tmp_path):
        config = Config(
            base_url="https://w.example",
            bot=Credentials("bot", "b", "ctx-secret-password"),
            user=None,
            cache_dir=tmp_path / "c",
        )
        ctx = ServerContext.create(config)
        try:
            assert "ctx-secret-password" not in sanitize_text("ctx-secret-password")
        finally:
            ctx.close()


class TestLogSafetyFilter:
    def test_filter_scrubs_message_and_args(self, caplog):
        secret = "bot-password-value-xyz"
        content = "verbatim wiki page content block"
        register_redactions(secret, content)

        caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.test_log_safety")
        logger = get_logger("test_log_safety")
        logger.warning("failure with %s and %s", secret, content)

        combined = caplog.text
        assert secret not in combined
        assert content not in combined
        assert "[REDACTED]" in combined

    def test_filter_applies_to_root_logger_records(self, caplog):
        secret = "registered-runtime-secret"
        register_redactions(secret)

        caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.fetch")
        logger = get_logger("fetch")
        logger.warning("lock issue: %s", secret)

        assert secret not in caplog.text
        assert "[REDACTED]" in caplog.text


class TestAuthFailureMessages:
    def test_summarize_auth_failures_omits_upstream_text(self):
        msg = summarize_auth_failures(["bot: LoginError", "user: ConnectionError"])
        assert "LoginError" in msg
        assert "verify wiki credentials" in msg
        assert "bad creds" not in msg

    def test_auth_path_failure_label_is_type_only(self):
        exc = ValueError("password=leaked-from-idp")
        label = auth_path_failure_label("bot", exc)
        assert label == "bot: ValueError"
        assert "leaked" not in label
        assert "password" not in label

    def test_simulated_login_failure_raises_safe_auth_error(self, tmp_path, monkeypatch):
        secret = "my-bot-password-should-not-appear"

        class LeakySite:
            connection = MagicMock()

            def login(self, _u, _p):
                raise LoginError(self, "Failed", f"invalid password {secret}")

        client = wc.WikiClient(
            Config(
                base_url="https://w.example",
                bot=Credentials("bot", "Acct@bot", secret),
                user=None,
                cache_dir=tmp_path / "c",
            )
        )
        monkeypatch.setattr(client, "_new_site", lambda: LeakySite())

        with pytest.raises(AuthErrorModel) as exc_info:
            client.login()

        message = str(exc_info.value)
        assert secret not in message
        assert "invalid password" not in message
        assert "bot: LoginError" in message

    def test_auth_error_mcp_message_contains_no_secret(self):
        secret = "my-wiki-bot-password-value"
        err = to_mcp_error(AuthError(f"legacy unsafe text with {secret}"))
        assert err.error.code == AUTH_ERROR
        assert secret not in err.error.message

    def test_calendar_failure_log_contains_no_secret(self, tmp_path, caplog):
        secret = "calendar-log-secret-value"
        register_redactions(secret)
        caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.meetings")

        from wg21_wiki_mcp.meetings import MeetingCalendar

        class _BrokenSession:
            headers: dict[str, str] = {}

            def get(self, *_a, **_k):
                raise RuntimeError(f"network down password={secret}")

        cal = MeetingCalendar(
            Config(
                base_url="https://w.example",
                bot=Credentials("bot", "b", secret),
                user=None,
                cache_dir=tmp_path / "c",
            ),
            session=_BrokenSession(),
        )
        cal.is_meeting_active()

        assert secret not in caplog.text
        assert "network down" in caplog.text.lower() or "RuntimeError" in caplog.text

    def test_fetch_lock_timeout_log_contains_no_page_title(self, tmp_path, caplog):
        page_title = "SecretCommitteePageTitle"
        page_content = "confidential wikitext that must never be logged"
        register_redactions(page_content)

        caplog.set_level(logging.WARNING, logger="wg21_wiki_mcp.fetch")

        client = FakeWikiClient()
        client.pages[page_title] = FakePage(page_content, 1)
        cache = Cache(tmp_path / "c")
        fetcher = PageFetcher(client, cache)  # type: ignore[arg-type]

        with patch("wg21_wiki_mcp.fetch.FileLock") as mock_fl:
            mock_lock = MagicMock()
            mock_lock.acquire.side_effect = Timeout(f"blocked on {page_title}")
            mock_fl.return_value = mock_lock
            fetcher.get_page(page_title, ttl_seconds=1000)

        assert page_title not in caplog.text
        assert page_content not in caplog.text
        assert "title_hash=" in caplog.text
        cache.close()


class TestLogSafetyFilterUnit:
    def test_filter_returns_true(self):
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        assert LogSafetyFilter().filter(record) is True

    def test_filter_handles_mapping_style_args(self):
        register_redactions("supersecretpassword")
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="value=%(key)s",
            args=({"key": "supersecretpassword"},),
            exc_info=None,
        )
        assert LogSafetyFilter().filter(record) is True
        assert record.args == {"key": "[REDACTED]"}

    def test_filter_handles_tuple_wrapped_mapping_args(self):
        register_redactions("tuple-secret-value")
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="value=%(key)s",
            args=(),
            exc_info=None,
        )
        record.args = ({"key": "tuple-secret-value"},)
        assert LogSafetyFilter().filter(record) is True
        assert record.args == ({"key": "[REDACTED]"},)

    def test_filter_skips_non_string_message(self):
        record = logging.LogRecord(
            name="wg21_wiki_mcp.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=12345,
            args=(),
            exc_info=None,
        )
        assert LogSafetyFilter().filter(record) is True
        assert record.msg == 12345
