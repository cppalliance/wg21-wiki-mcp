"""Centralized error types, codes, and MCP error-contract mapping.

Every domain error that crosses a tool boundary is converted here to a
structured ``McpError`` / ``ErrorData`` with a distinct, documented code.
All error messages are safe: they contain no credential values and no wiki
page content.

Error codes
-----------
The JSON-RPC 2.0 spec reserves -32768 to -32000 for transport / protocol
errors. Application-defined codes are kept in the positive range to make the
distinction unambiguous.

============== ====  ===========================================
Name           Code  Meaning
============== ====  ===========================================
PAGE_NOT_FOUND    1  The requested page does not exist on the wiki.
AUTH_ERROR        2  Authentication failed for every configured
                     credential path; check wiki credentials.
FETCH_ERROR       3  A network or API error prevented retrieval
                     after all retries; check connectivity.
CONFIG_ERROR      4  Required server configuration is missing or
                     invalid; set the credential env vars.
============== ====  ===========================================

The cursor / pagination code (``INVALID_PARAMS`` / ``-32602``) is defined in
``pagination.py`` and is part of the MCP protocol layer, not the domain layer.
"""

from __future__ import annotations

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from .log_safety import auth_error_mcp_message

# ---------------------------------------------------------------------------
# Application error codes (positive integers, distinct from JSON-RPC reserved)
# ---------------------------------------------------------------------------

PAGE_NOT_FOUND: int = 1
AUTH_ERROR: int = 2
FETCH_ERROR: int = 3
CONFIG_ERROR: int = 4

# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class WikiMcpError(RuntimeError):
    """Base class for all server-raised domain errors."""


class AuthError(WikiMcpError):
    """Authentication failed for every configured credential path."""


class PageNotFound(WikiMcpError):
    """The requested page does not exist on the wiki.

    The exception message must describe what was searched for (title, section
    number) and must never include wiki page content.  ``to_mcp_error`` passes
    the message through as-is; the safety invariant is enforced by caller
    convention.
    """


class FetchError(WikiMcpError):
    """A network or API error prevented page retrieval after all retries."""


class ConfigError(WikiMcpError):
    """Required server configuration is missing or invalid.

    Raised at startup when no credentials are present in the environment.
    The message always names the env vars to set and never carries credential
    values.
    """


# ---------------------------------------------------------------------------
# Mapping to structured McpError
# ---------------------------------------------------------------------------


def to_mcp_error(exc: BaseException) -> McpError:
    """Convert any domain or transport exception to a structured ``McpError``.

    Use this at every tool boundary so agents always receive a spec-shaped
    error with a distinct, documented code.  Error messages are sanitized:
    they contain no credential values and no wiki page content.

    ``McpError`` instances are returned unchanged so the cursor / pagination
    layer (``INVALID_PARAMS``) passes through unmodified.
    """
    if isinstance(exc, McpError):
        return exc

    if isinstance(exc, PageNotFound):
        # str(exc) is always "Page not found: <title>" — user-supplied title,
        # not confidential content.
        return McpError(ErrorData(code=PAGE_NOT_FOUND, message=str(exc) or "Page not found."))

    if isinstance(exc, AuthError):
        # AuthError messages are built by log_safety helpers; unsafe legacy
        # messages fall back to AUTH_FAILURE_MESSAGE at the MCP boundary.
        return McpError(
            ErrorData(
                code=AUTH_ERROR,
                message=auth_error_mcp_message(exc),
            )
        )

    if isinstance(exc, FetchError):
        # Use a fixed message; the original includes the mwclient exception
        # string which, while not containing credentials, is not useful to agents.
        return McpError(
            ErrorData(
                code=FETCH_ERROR,
                message="Wiki API fetch failed after retries; check connectivity or try again.",
            )
        )

    if isinstance(exc, ConfigError):
        # The ConfigError message names the env vars to set — safe and actionable.
        return McpError(ErrorData(code=CONFIG_ERROR, message=str(exc) or "Server not configured."))

    # Wrap raw mwclient APIError that escaped the client layer without being
    # converted to FetchError (non-transient, non-auth error code).
    try:
        from mwclient.errors import APIError as _APIError  # local import: optional dep

        if isinstance(exc, _APIError):
            code_label = getattr(exc, "code", "unknown")
            return McpError(
                ErrorData(
                    code=FETCH_ERROR,
                    message=f"Wiki API returned an error (code: {code_label}).",
                )
            )
    except ImportError:  # pragma: no cover
        pass

    # Fallback for any unexpected exception type.
    return McpError(
        ErrorData(
            code=FETCH_ERROR,
            message="An unexpected server error occurred.",
        )
    )
