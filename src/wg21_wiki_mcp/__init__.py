"""wg21-wiki-mcp: a local stdio MCP server for the WG21 (ISO C++) wiki.

The package exposes a FastMCP server (see :mod:`wg21_wiki_mcp.server`) that
serves WG21 committee wiki pages over the live MediaWiki API as a verifiable
source of truth: every result is exact wiki content with a clickable URL and
revision id. See ``ARCHITECTURE.md`` for the design and ``README.md`` for usage.
"""

__version__ = "0.2.0"
