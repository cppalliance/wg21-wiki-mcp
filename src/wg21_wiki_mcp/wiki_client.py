"""Authenticated MediaWiki client.

Ported and hardened from the project's data-collection spike. Responsibilities:

* Auto-select the auth path: try bot credentials first (the default), fall back
  to headless user SSO (SimpleSAMLphp form-flow) on failure, fail fast if neither
  works. The path that succeeds is pinned and reused for every later re-login.
* Provide an ``api()`` wrapper that retries transient errors and transparently
  re-logs-in when the server drops the session (surfaces as ``readapidenied``).
* Offer thin, batch-friendly read methods used by the cache/fetch layer.

The client never transforms page content; it returns exact wikitext.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import mwclient
import requests
from mwclient.errors import APIError, MwClientError

from .config import Config, Credentials
from .deadlines import API_TIMEOUT_MSG, composite_deadline, http_timeout, timeout_remaining
from .log_safety import auth_path_failure_label, summarize_auth_failures
from .models import AuthError, FetchError

_AUTH_ERROR_CODES = frozenset({"readapidenied", "assertuserfailed", "notloggedin", "badtoken"})
_BACKOFF_CODES = frozenset({"maxlag", "ratelimited"})
_MAX_RETRIES = 6
_MAX_TITLES_PER_BATCH = 50  # safe limit for accounts without apihighlimits
_SAML_MAX_RETRIES = 2
_TRANSIENT_HTTP_CODES = frozenset(range(500, 600))
_UNSET_TIMEOUT = object()

_API_REQUEST_TIMEOUT: ContextVar[float | None] = ContextVar("_API_REQUEST_TIMEOUT", default=None)

_log = logging.getLogger(__name__)


def _install_per_call_request_timeout(session: requests.Session) -> None:
    """Wrap ``session.request`` so per-call API timeouts override mwclient defaults."""
    orig = session.request

    def request(method: str | bytes, url: str | bytes, **kwargs: Any) -> requests.Response:
        override = _API_REQUEST_TIMEOUT.get()
        if override is not None:
            kwargs["timeout"] = override
        return orig(method, url, **kwargs)

    session.request = request  # type: ignore[method-assign, assignment]


def _log_saml_step(step: str, **context: object) -> None:
    """Emit a DEBUG log for one SAML flow stage (no credential values)."""
    if not _log.isEnabledFor(logging.DEBUG):
        return
    if context:
        details = " ".join(f"{key}={value!r}" for key, value in context.items())
        _log.debug("SAML %s %s", step, details)
    else:
        _log.debug("SAML %s", step)


def _saml_error(
    message: str,
    *,
    url: str | None = None,
    status: int | None = None,
    field_names: list[str] | None = None,
) -> AuthError:
    parts = [message]
    if url:
        parts.append(f"url={url}")
    if status is not None:
        parts.append(f"status={status}")
    if field_names is not None:
        parts.append(f"fields={field_names}")
    return AuthError("; ".join(parts))


def _form_named_inputs(form: object) -> dict[str, str]:
    from bs4 import Tag

    if not isinstance(form, Tag):
        return {}
    return {str(i.get("name")): str(i.get("value", "")) for i in form.find_all("input") if i.get("name")}


def _resolve_saml_credential_fields(
    form: object,
    fields: dict[str, str],
    config: Config,
) -> tuple[str | None, str | None]:
    from bs4 import Tag

    if not isinstance(form, Tag):
        return None, None

    if config.saml_username_field and config.saml_username_field in fields:
        user_field: str | None = config.saml_username_field
    else:
        user_input = form.find("input", {"type": "email"}) or form.find("input", {"type": "text", "name": True})
        name = user_input.get("name") if user_input is not None else None
        if name:
            user_field = str(name)
        elif "username" in fields:
            user_field = "username"
        else:
            user_field = next((n for n in fields if "user" in n.lower() or "email" in n.lower()), None)

    if config.saml_password_field and config.saml_password_field in fields:
        pass_field: str | None = config.saml_password_field
    else:
        pass_input = form.find("input", {"type": "password"})
        pass_name = pass_input.get("name") if pass_input is not None else None
        if pass_name:
            pass_field = str(pass_name)
        elif "password" in fields:
            pass_field = "password"
        else:
            pass_field = next((n for n in fields if "pass" in n.lower()), None)

    return user_field, pass_field


def _saml_http_request(  # type: ignore[return]
    method: Callable[..., requests.Response],
    *args: Any,
    deadline: float | None,
    cap: float,
    step: str,
    **kwargs: Any,
) -> requests.Response:
    """Run one SAML HTTP hop with retry on transient failures."""
    request_url = str(args[0]) if args else str(kwargs.get("url", ""))
    for attempt in range(_SAML_MAX_RETRIES):
        timeout = http_timeout(deadline, cap=cap, on_exceeded=API_TIMEOUT_MSG)
        _log_saml_step(step, attempt=attempt + 1, max_attempts=_SAML_MAX_RETRIES, url=request_url or None)
        try:
            resp = method(*args, timeout=timeout, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            _log_saml_step(
                f"{step}_transient_error",
                error=type(exc).__name__,
                attempt=attempt + 1,
                url=request_url or None,
            )
            if attempt + 1 < _SAML_MAX_RETRIES:
                sleep_s = min(2**attempt, 5)
                if deadline is not None:
                    remaining = timeout_remaining(deadline, on_exceeded=API_TIMEOUT_MSG)
                    sleep_s = min(sleep_s, remaining)
                time.sleep(sleep_s)
                continue
            raise _saml_error(
                f"SAML SSO request failed: {type(exc).__name__}.",
                url=request_url or None,
            ) from exc
        except requests.RequestException as exc:
            raise _saml_error(
                f"SAML SSO request failed: {type(exc).__name__}.",
                url=request_url or None,
            ) from exc

        if resp.status_code in _TRANSIENT_HTTP_CODES and attempt + 1 < _SAML_MAX_RETRIES:
            _log_saml_step(
                f"{step}_retry",
                status=resp.status_code,
                url=resp.url,
                attempt=attempt + 1,
            )
            sleep_s = min(2**attempt, 5)
            if deadline is not None:
                remaining = timeout_remaining(deadline, on_exceeded=API_TIMEOUT_MSG)
                sleep_s = min(sleep_s, remaining)
            time.sleep(sleep_s)
            continue
        return resp


class _RWLock:
    """Readers-writer lock with a reentrant writer (same thread may nest writes)."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer_depth = 0
        self._writers_waiting = 0
        self._writer_tid: int | None = None

    @contextmanager
    def read(self) -> Iterator[None]:
        with self._cond:
            while self._writer_depth > 0 or self._writers_waiting > 0:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def write(self) -> Iterator[None]:
        tid = threading.get_ident()
        with self._cond:
            if self._writer_depth > 0 and self._writer_tid == tid:
                self._writer_depth += 1
            else:
                self._writers_waiting += 1
                try:
                    while self._readers > 0 or (self._writer_depth > 0 and self._writer_tid != tid):
                        self._cond.wait()
                finally:
                    self._writers_waiting -= 1
                self._writer_depth += 1
                self._writer_tid = tid
        try:
            yield
        finally:
            with self._cond:
                self._writer_depth -= 1
                if self._writer_depth == 0:
                    self._writer_tid = None
                self._cond.notify_all()


@dataclass
class FetchedPage:
    """Result of fetching one page by its originally requested title."""

    requested_title: str
    title: str
    redirected_from: str | None
    revid: int | None
    timestamp: str | None
    size: int | None
    content: str | None
    missing: bool


class WikiClient:
    """Thread-safe-ish authenticated MediaWiki client.

    A single shared session is used. Read-only ``query`` API calls may run
    concurrently under a shared reader lock, including timed ``query`` calls
    (per-call HTTP timeouts do not mutate ``site.requests``). Login, re-login,
    and other mutations take an exclusive writer lock so the session cannot be
    corrupted.
    """

    def __init__(self, config: Config) -> None:
        """Initialize the client from config (no network until login/api)."""
        self._config = config
        parsed = urlparse(config.base_url)
        self._host = parsed.netloc or parsed.path
        self._scheme = parsed.scheme or "https"
        self._site: mwclient.Site | None = None
        self._active: Credentials | None = None
        self._lock = _RWLock()
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("WikiClient is closed")

    # -- properties ---------------------------------------------------------
    @property
    def active_label(self) -> str | None:
        """Label of the pinned credential path ("bot"/"user"), or None."""
        return self._active.label if self._active else None

    @property
    def username(self) -> str | None:
        """Username of the pinned credential path, or None if not logged in."""
        return self._active.username if self._active else None

    # -- login --------------------------------------------------------------
    def login(self, *, deadline: float | None = None) -> None:
        """Authenticate using the first credential path that works (bot first).

        Raises:
            AuthError: if no configured credential path can log in.
            RuntimeError: if the client has been closed.
        """
        with self._lock.write():
            self._require_open()
            self._login_locked(deadline=deadline)

    def _login_locked(self, *, deadline: float | None = None) -> None:
        """Authenticate; caller must hold the write lock."""
        timeout_remaining(deadline, on_exceeded=API_TIMEOUT_MSG)
        path_failures: list[str] = []
        for cred in self._config.ordered_credentials:
            timeout_remaining(deadline, on_exceeded=API_TIMEOUT_MSG)
            try:
                self._login_with(cred, deadline=deadline)
                self._active = cred
                return
            except FetchError:
                raise
            except Exception as exc:  # noqa: BLE001 - record and try next path
                path_failures.append(auth_path_failure_label(cred.label, exc))
        raise AuthError(summarize_auth_failures(path_failures))

    def _relogin_locked(self, *, deadline: float | None = None) -> None:
        """Re-run only the pinned credential path; caller must hold the write lock."""
        timeout_remaining(deadline, on_exceeded=API_TIMEOUT_MSG)
        if self._active is None:
            self._login_locked(deadline=deadline)
            return
        self._login_with(self._active, deadline=deadline)

    def _new_site(self) -> mwclient.Site:
        site = mwclient.Site(
            self._host,
            path="/",
            scheme=self._scheme,
            clients_useragent=self._config.user_agent,
            max_lag=5,
        )
        _install_per_call_request_timeout(site.connection)
        return site

    @staticmethod
    def _close_site(site: mwclient.Site | None) -> None:
        """Close one mwclient site handle (hygiene; errors propagate to caller)."""
        if site is not None:
            site.connection.close()

    def _replace_site(self, site: mwclient.Site) -> None:
        """Install a new site handle, closing any prior session first."""
        old = self._site
        self._site = site
        if old is not None and old is not site:
            self._close_site(old)

    def _login_with(self, cred: Credentials, *, deadline: float | None = None) -> None:
        timeout_remaining(deadline, on_exceeded=API_TIMEOUT_MSG)
        if cred.label == "bot":
            self._bot_login(cred, deadline=deadline)
        else:
            self._user_login(cred, deadline=deadline)

    @staticmethod
    def _timeout_remaining(deadline: float | None) -> float | None:
        """Return seconds left until ``deadline`` (API budget)."""
        return timeout_remaining(deadline, on_exceeded=API_TIMEOUT_MSG)

    @contextmanager
    def _site_request_timeout(self, site: mwclient.Site, deadline: float | None) -> Iterator[None]:
        saved_request_timeout: object = _UNSET_TIMEOUT
        request_opts = getattr(site, "requests", None)
        request_timeout_modified = False
        if deadline is not None:
            remaining = self._timeout_remaining(deadline)
            if isinstance(request_opts, dict):
                saved_request_timeout = request_opts.get("timeout", _UNSET_TIMEOUT)
                request_opts["timeout"] = remaining
                request_timeout_modified = True
        try:
            yield
        finally:
            if request_timeout_modified and isinstance(request_opts, dict):
                if saved_request_timeout is _UNSET_TIMEOUT:
                    request_opts.pop("timeout", None)
                else:
                    request_opts["timeout"] = saved_request_timeout

    @contextmanager
    def _api_request_timeout_scope(self, deadline: float | None) -> Iterator[None]:
        """Per-call HTTP timeout for API reads without mutating ``site.requests``."""
        if deadline is None:
            yield
            return
        remaining = self._timeout_remaining(deadline)
        token: Token[float | None] = _API_REQUEST_TIMEOUT.set(remaining)
        try:
            yield
        finally:
            _API_REQUEST_TIMEOUT.reset(token)

    def _bot_login(self, cred: Credentials, *, deadline: float | None = None) -> None:
        self._timeout_remaining(deadline)
        site = self._new_site()
        with self._site_request_timeout(site, deadline):
            site.login(cred.username, cred.password)
        self._replace_site(site)

    def _user_login(self, cred: Credentials, *, deadline: float | None = None) -> None:
        self._timeout_remaining(deadline)
        site = self._new_site()
        # Try local clientlogin first (works only if the wiki allows local login).
        if self._try_clientlogin(site, cred, deadline=deadline):
            self._replace_site(site)
            return
        # Fall back to the headless SimpleSAMLphp web-SSO flow.
        try:
            self._saml_login(site, cred, deadline=deadline)
        except AuthError as exc:
            raise AuthError(f"clientlogin unavailable; {exc}") from exc
        with self._site_request_timeout(site, deadline):
            site.site_init()
            if not self._is_authenticated(site):
                raise AuthError("SAML login completed but the API still sees an anonymous session.")
        self._replace_site(site)

    def _try_clientlogin(self, site: mwclient.Site, cred: Credentials, *, deadline: float | None = None) -> bool:
        self._timeout_remaining(deadline)
        # Call the API directly: mwclient.clientlogin() calls require(1, 27),
        # which fails on a read-protected wiki before login.
        try:
            with self._site_request_timeout(site, deadline):
                token = site.get_token("login")
                resp = site.post(
                    "clientlogin",
                    username=cred.username,
                    password=cred.password,
                    logintoken=token,
                    loginreturnurl=f"{self._scheme}://{self._host}",
                )
                if resp.get("clientlogin", {}).get("status") == "PASS":
                    site.site_init()
                    return True
        except (APIError, MwClientError):
            return False
        return False

    def _saml_login(self, site: mwclient.Site, cred: Credentials, *, deadline: float | None = None) -> None:
        from bs4 import BeautifulSoup  # local import: only needed for the user path

        session = site.connection  # reuse mwclient's requests.Session so cookies persist
        start = f"{self._config.base_url}/index.php?title=Special:PluggableAuthLogin"
        saml_cap = float(self._config.saml_timeout_s)

        resp = _saml_http_request(
            session.get,
            start,
            allow_redirects=True,
            deadline=deadline,
            cap=saml_cap,
            step="sso_get",
        )
        _log_saml_step("sso_get_done", url=resp.url, status=resp.status_code)

        if not resp.ok:
            raise _saml_error(
                "SAML SSO entry point returned HTTP error.",
                url=resp.url,
                status=resp.status_code,
            )

        soup = BeautifulSoup(resp.text, "lxml")
        form = next((f for f in soup.find_all("form") if f.find("input", {"type": "password"})), None)
        if form is None:
            field_names = [str(i.get("name")) for i in soup.find_all("input") if i.get("name")]
            raise _saml_error(
                "SAML IdP login form not found (page changed or extra step required).",
                url=resp.url,
                status=resp.status_code,
                field_names=field_names or None,
            )

        action = urljoin(resp.url, str(form.get("action") or resp.url))
        fields = _form_named_inputs(form)
        _log_saml_step("idp_form_parse", url=resp.url, field_names=list(fields.keys()))

        user_field, pass_field = _resolve_saml_credential_fields(form, fields, self._config)
        if not (user_field and pass_field):
            raise _saml_error(
                "Could not locate username/password fields on the IdP form.",
                url=resp.url,
                status=resp.status_code,
                field_names=list(fields.keys()),
            )
        fields[user_field] = cred.username
        fields[pass_field] = cred.password

        posted = _saml_http_request(
            session.post,
            action,
            data=fields,
            allow_redirects=True,
            deadline=deadline,
            cap=saml_cap,
            step="idp_post",
        )
        _log_saml_step("idp_post_done", url=posted.url, status=posted.status_code)

        if not posted.ok:
            raise _saml_error(
                "SAML IdP POST returned HTTP error.",
                url=posted.url,
                status=posted.status_code,
            )

        soup2 = BeautifulSoup(posted.text, "lxml")
        saml_form = next((f for f in soup2.find_all("form") if f.find("input", {"name": "SAMLResponse"})), None)
        if saml_form is None:
            if "SAMLResponse" not in posted.text:
                raise _saml_error(
                    "SAML login failed (no SAMLResponse; check credentials/MFA).",
                    url=posted.url,
                    status=posted.status_code,
                )
            _log_saml_step("acs_auto_follow", url=posted.url)
            return  # the client auto-followed the POST
        acs = urljoin(posted.url, str(saml_form.get("action")))
        payload = _form_named_inputs(saml_form)
        acs_resp = _saml_http_request(
            session.post,
            acs,
            data=payload,
            allow_redirects=True,
            deadline=deadline,
            cap=saml_cap,
            step="acs_post",
        )
        _log_saml_step("acs_post_done", url=acs_resp.url, status=acs_resp.status_code)
        if not acs_resp.ok:
            raise _saml_error(
                "SAML ACS endpoint rejected the response.",
                url=acs_resp.url,
                status=acs_resp.status_code,
            )

    @staticmethod
    def _is_authenticated(site: mwclient.Site) -> bool:
        info = site.api("query", meta="userinfo")["query"]["userinfo"]
        return bool(info.get("name")) and "anon" not in info

    # -- API call wrapper ---------------------------------------------------
    def _api_call_locked(
        self,
        site: mwclient.Site,
        action: str,
        deadline: float | None,
        params: dict[str, object],
    ) -> dict:
        """Invoke ``site.api``; caller must hold read or write lock."""
        with self._api_request_timeout_scope(deadline):
            return site.api(action, **params)

    def _api_attempt(
        self,
        action: str,
        *,
        deadline: float | None,
        params: dict[str, object],
    ) -> tuple[dict | None, Exception | None, bool]:
        """One locked API attempt. Returns (result, last_exc, relogin)."""
        relogin = False
        read_only = action == "query"
        use_read_lock = read_only

        if use_read_lock:
            with self._lock.read():
                self._require_open()
                site = self._site
                if site is not None:
                    try:
                        return self._api_call_locked(site, action, deadline, params), None, False
                    except APIError as exc:
                        last_exc = exc
                        if exc.code in _AUTH_ERROR_CODES:
                            relogin = True
                        elif exc.code not in _BACKOFF_CODES:
                            raise
                    except (MwClientError, ConnectionError, OSError) as exc:
                        last_exc = exc
                    return None, last_exc, relogin

        with self._lock.write():
            self._require_open()
            if self._site is None:
                self._login_locked(deadline=deadline)
            assert self._site is not None
            try:
                return self._api_call_locked(self._site, action, deadline, params), None, False
            except APIError as exc:
                last_exc = exc
                if exc.code in _AUTH_ERROR_CODES:
                    relogin = True
                elif exc.code not in _BACKOFF_CODES:
                    raise
            except (MwClientError, ConnectionError, OSError) as exc:
                last_exc = exc

        return None, last_exc, relogin

    def api(self, action: str, *, timeout: float | None = None, **params: object) -> dict:
        """Call the Action API with retry + automatic re-login on session loss.

        Args:
            timeout: Optional wall-clock limit in seconds for this call (all
                retries and backoff sleeps included).
        """
        deadline = composite_deadline(timeout) if timeout is not None else None
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            self._timeout_remaining(deadline)
            result, last_exc, relogin = self._api_attempt(action, deadline=deadline, params=params)
            if result is not None:
                return result
            sleep_s = min(2**attempt, 30)
            if deadline is not None:
                remaining = self._timeout_remaining(deadline)
                assert remaining is not None
                sleep_s = min(sleep_s, remaining)
            time.sleep(sleep_s)
            if relogin:
                self._timeout_remaining(deadline)
                with self._lock.write():
                    self._require_open()
                    self._relogin_locked(deadline=deadline)
        if isinstance(last_exc, APIError) and last_exc.code in _AUTH_ERROR_CODES:
            raise AuthError(f"Session could not be re-established after {_MAX_RETRIES} attempts.") from last_exc
        raise FetchError(f"API call '{action}' failed after {_MAX_RETRIES} retries.") from last_exc

    # -- URL helpers --------------------------------------------------------
    def canonical_url(self, title: str) -> str:
        """Return the canonical clickable page URL for a title."""
        return f"{self._config.base_url}/index.php?title={quote(title.replace(' ', '_'), safe=':/')}"

    def oldid_url(self, title: str, revid: int | None) -> str | None:
        """Return the permanent URL pinned to ``revid`` (None if no revid)."""
        if revid is None:
            return None
        base = self.canonical_url(title)
        return f"{base}&oldid={revid}"

    # -- read methods -------------------------------------------------------
    def fetch_pages(self, titles: list[str], *, timeout: float | None = None) -> dict[str, FetchedPage]:
        """Batch-fetch verbatim wikitext + metadata for many titles at once.

        Returns a map keyed by the originally requested title. Handles MediaWiki
        title normalization and redirects so content is attributed correctly.
        """
        deadline = composite_deadline(timeout) if timeout is not None else None
        results: dict[str, FetchedPage] = {}
        for start in range(0, len(titles), _MAX_TITLES_PER_BATCH):
            batch = titles[start : start + _MAX_TITLES_PER_BATCH]
            resp = self.api(
                "query",
                timeout=self._timeout_remaining(deadline),
                titles="|".join(batch),
                prop="revisions",
                rvprop="ids|timestamp|size|content",
                rvslots="main",
                redirects=1,
            )
            results.update(self._map_batch(batch, resp.get("query", {})))
        return results

    def _map_batch(self, requested: list[str], query: dict) -> dict[str, FetchedPage]:
        # Build requested-title -> final-title resolution via normalized + redirects.
        normalized = {n["from"]: n["to"] for n in query.get("normalized", [])}
        redirects = {r["from"]: r["to"] for r in query.get("redirects", [])}

        def resolve(title: str) -> tuple[str, str | None]:
            redirected_from: str | None = None
            current = normalized.get(title, title)
            seen = set()
            while current in redirects and current not in seen:
                seen.add(current)
                redirected_from = redirected_from or title
                current = redirects[current]
            return current, redirected_from

        by_title: dict[str, dict] = {p["title"]: p for p in query.get("pages", {}).values()}

        out: dict[str, FetchedPage] = {}
        for req in requested:
            final, redirected_from = resolve(req)
            page = by_title.get(final)
            if page is None or "missing" in page:
                out[req] = FetchedPage(req, final, redirected_from, None, None, None, None, True)
                continue
            revisions = page.get("revisions") or []
            if not revisions:
                out[req] = FetchedPage(req, final, redirected_from, None, None, None, None, True)
                continue
            rev = revisions[0]
            slot = rev.get("slots", {}).get("main", rev)
            content = slot.get("*", "")
            out[req] = FetchedPage(
                requested_title=req,
                title=page["title"],
                redirected_from=redirected_from,
                revid=rev.get("revid"),
                timestamp=rev.get("timestamp"),
                size=rev.get("size", len(content.encode("utf-8"))),
                content=content,
                missing=False,
            )
        return out

    def fetch_page_section(self, title: str, section: int, *, timeout: float | None = None) -> FetchedPage:
        """Fetch one section's verbatim wikitext (server-side ``rvsection`` split)."""
        resp = self.api(
            "query",
            timeout=timeout,
            titles=title,
            prop="revisions",
            rvprop="ids|timestamp|size|content",
            rvslots="main",
            rvsection=section,
            redirects=1,
        )
        mapped = self._map_batch([title], resp.get("query", {}))
        return mapped.get(title) or FetchedPage(title, title, None, None, None, None, None, True)

    def page_revisions(self, titles: list[str], *, timeout: float | None = None) -> dict[str, int | None]:
        """Cheaply fetch current revids (for cache revalidation)."""
        deadline = composite_deadline(timeout) if timeout is not None else None
        out: dict[str, int | None] = {}
        for start in range(0, len(titles), _MAX_TITLES_PER_BATCH):
            batch = titles[start : start + _MAX_TITLES_PER_BATCH]
            resp = self.api(
                "query",
                timeout=self._timeout_remaining(deadline),
                titles="|".join(batch),
                prop="revisions",
                rvprop="ids",
                redirects=1,
            )
            mapped = self._map_batch(batch, resp.get("query", {}))
            out.update({title: page.revid for title, page in mapped.items()})
        return out

    def search(self, query: str, *, limit: int, namespace: int | None, offset: int) -> dict:
        """Run a CirrusSearch full-text query; returns the raw API response."""
        params: dict[str, object] = {
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
            "sroffset": offset,
            "srprop": "size|wordcount|timestamp|snippet",
        }
        if namespace is not None:
            params["srnamespace"] = namespace
        return self.api("query", timeout=None, **params)

    def list_pages(self, *, namespace: int, prefix: str | None, limit: int, cont: str | None) -> dict:
        """Enumerate pages in a namespace via ``allpages``; returns the raw response."""
        params: dict[str, object] = {
            "list": "allpages",
            "apnamespace": namespace,
            "aplimit": limit,
        }
        if prefix:
            params["apprefix"] = prefix
        if cont:
            params["apcontinue"] = cont
        return self.api("query", timeout=None, **params)

    def list_namespaces(self) -> dict:
        """Return the wiki's namespace table (``siteinfo``) as the raw response."""
        return self.api("query", meta="siteinfo", siprop="namespaces")

    def recent_changes(self, *, namespace: int | None, since: str | None, limit: int, cont: str | None) -> dict:
        """Fetch recent edits/new pages via ``recentchanges``; returns the raw response."""
        params: dict[str, object] = {
            "list": "recentchanges",
            "rclimit": limit,
            "rcprop": "title|ids|timestamp|user|comment|flags",
            "rctype": "edit|new",
        }
        if namespace is not None:
            params["rcnamespace"] = namespace
        if since:
            params["rcend"] = since
        if cont:
            params["rccontinue"] = cont
        return self.api("query", timeout=None, **params)

    def page_links(self, title: str, *, limit: int, cont: str | None, timeout: float | None = None) -> dict:
        """Fetch the internal links on a page via ``prop=links``; returns the raw response."""
        params: dict[str, object] = {"titles": title, "prop": "links", "pllimit": limit}
        if cont:
            params["plcontinue"] = cont
        return self.api("query", timeout=timeout, **params)

    def statistics(self) -> dict:
        """Return the wiki's ``siteinfo`` statistics as the raw response."""
        return self.api("query", meta="siteinfo", siprop="statistics")

    def close(self) -> None:
        """Close the underlying HTTP session and release the site handle."""
        if self._closed:
            return
        with self._lock.write():
            try:
                self._close_site(self._site)
            finally:
                self._site = None
                self._active = None
                self._closed = True
