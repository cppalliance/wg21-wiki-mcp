"""Shared server context: the wired-together collaborators used by all tools.

Building this is cheap and side-effect-light; the network login happens lazily on
first use (or explicitly via :meth:`login`). Tests construct a context with a
fake client to exercise tool logic without network access.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cache import Cache
from .config import Config
from .fetch import FetchOutcome, PageFetcher
from .log_safety import register_config_secrets
from .meetings import MeetingCalendar
from .models import Provenance
from .wiki_client import WikiClient


@dataclass
class ServerContext:
    """The wired-together collaborators (config, client, calendar, cache, fetcher) shared by all tools."""

    config: Config
    client: WikiClient
    calendar: MeetingCalendar
    cache: Cache
    fetcher: PageFetcher

    @classmethod
    def create(cls, config: Config) -> ServerContext:
        """Build a context from config (no network until first use)."""
        register_config_secrets(config)
        client = WikiClient(config)
        cache = Cache(config.cache_dir)
        return cls(
            config=config,
            client=client,
            calendar=MeetingCalendar(config),
            cache=cache,
            fetcher=PageFetcher(client, cache),
        )

    def login(self) -> None:
        """Authenticate the wiki client (fail fast if no credential path works)."""
        self.client.login()

    # -- shared helpers -----------------------------------------------------
    def current_ttl(self) -> int:
        """Return the current meeting-aware cache TTL in seconds."""
        return self.calendar.ttl_seconds()

    def provenance(self, outcome: FetchOutcome) -> Provenance:
        """Build a Provenance record (URLs, revid, timestamps) for a fetch outcome."""
        return Provenance(
            requested_title=outcome.requested_title,
            title=outcome.title,
            redirected_from=outcome.redirected_from,
            revid=outcome.revid,
            last_modified=outcome.timestamp,
            fetched_at=outcome.fetched_at,
            url=self.client.canonical_url(outcome.title),
            oldid_url=self.client.oldid_url(outcome.title, outcome.revid),
            from_cache=outcome.from_cache,
        )

    def close(self) -> None:
        """Release cache, calendar, and wiki client resources."""
        first_error: BaseException | None = None
        try:
            self.cache.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        try:
            self.calendar.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        try:
            self.client.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error
