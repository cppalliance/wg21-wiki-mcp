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

import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlparse

import mwclient
from mwclient.errors import APIError, MwClientError

from .config import Config, Credentials
from .log_safety import auth_path_failure_label, summarize_auth_failures
from .models import AuthError, FetchError

_AUTH_ERROR_CODES = frozenset({"readapidenied", "assertuserfailed", "notloggedin", "badtoken"})
_BACKOFF_CODES = frozenset({"maxlag", "ratelimited"})
_MAX_RETRIES = 6
_MAX_TITLES_PER_BATCH = 50  # safe limit for accounts without apihighlimits


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

    A single shared session is used; ``api()`` serializes network calls with a
    lock so concurrent callers (the bounded fetch pool) cannot corrupt the
    underlying requests session or trigger overlapping re-logins.
    """

    def __init__(self, config: Config) -> None:
        """Initialize the client from config (no network until login/api)."""
        self._config = config
        parsed = urlparse(config.base_url)
        self._host = parsed.netloc or parsed.path
        self._scheme = parsed.scheme or "https"
        self._site: mwclient.Site | None = None
        self._active: Credentials | None = None
        self._lock = threading.RLock()
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
    def login(self) -> None:
        """Authenticate using the first credential path that works (bot first).

        Raises:
            AuthError: if no configured credential path can log in.
            RuntimeError: if the client has been closed.
        """
        with self._lock:
            self._require_open()
            path_failures: list[str] = []
            for cred in self._config.ordered_credentials:
                try:
                    self._login_with(cred)
                    self._active = cred
                    return
                except Exception as exc:  # noqa: BLE001 - record and try next path
                    path_failures.append(auth_path_failure_label(cred.label, exc))
            raise AuthError(summarize_auth_failures(path_failures))

    def _relogin(self) -> None:
        """Re-run only the pinned credential path (no re-probing)."""
        if self._active is None:
            self.login()
            return
        self._login_with(self._active)

    def _new_site(self) -> mwclient.Site:
        return mwclient.Site(
            self._host,
            path="/",
            scheme=self._scheme,
            clients_useragent=self._config.user_agent,
            max_lag=5,
        )

    def _login_with(self, cred: Credentials) -> None:
        if cred.label == "bot":
            self._bot_login(cred)
        else:
            self._user_login(cred)

    def _bot_login(self, cred: Credentials) -> None:
        site = self._new_site()
        site.login(cred.username, cred.password)
        self._site = site

    def _user_login(self, cred: Credentials) -> None:
        site = self._new_site()
        # Try local clientlogin first (works only if the wiki allows local login).
        if self._try_clientlogin(site, cred):
            self._site = site
            return
        # Fall back to the headless SimpleSAMLphp web-SSO flow.
        self._saml_login(site, cred)
        site.site_init()
        if not self._is_authenticated(site):
            raise AuthError("SAML login completed but the API still sees an anonymous session.")
        self._site = site

    def _try_clientlogin(self, site: mwclient.Site, cred: Credentials) -> bool:
        # Call the API directly: mwclient.clientlogin() calls require(1, 27),
        # which fails on a read-protected wiki before login.
        try:
            token = site.get_token("login")
            resp = site.post(
                "clientlogin",
                username=cred.username,
                password=cred.password,
                logintoken=token,
                loginreturnurl=f"{self._scheme}://{self._host}",
            )
        except (APIError, MwClientError):
            return False
        if resp.get("clientlogin", {}).get("status") == "PASS":
            site.site_init()
            return True
        return False

    def _saml_login(self, site: mwclient.Site, cred: Credentials) -> None:
        from bs4 import BeautifulSoup  # local import: only needed for the user path

        session = site.connection  # reuse mwclient's requests.Session so cookies persist
        start = f"{self._config.base_url}/index.php?title=Special:PluggableAuthLogin"
        resp = session.get(start, allow_redirects=True, timeout=30)

        soup = BeautifulSoup(resp.text, "lxml")
        form = next((f for f in soup.find_all("form") if f.find("input", {"type": "password"})), None)
        if form is None:
            raise AuthError("SAML IdP login form not found (page changed or extra step required).")

        action = urljoin(resp.url, str(form.get("action") or resp.url))
        fields = {str(i.get("name")): str(i.get("value", "")) for i in form.find_all("input") if i.get("name")}
        user_field = (
            "username"
            if "username" in fields
            else next((n for n in fields if "user" in n.lower() or "email" in n.lower()), None)
        )
        pass_field = "password" if "password" in fields else next((n for n in fields if "pass" in n.lower()), None)
        if not (user_field and pass_field):
            raise AuthError("Could not locate username/password fields on the IdP form.")
        fields[user_field] = cred.username
        fields[pass_field] = cred.password

        posted = session.post(action, data=fields, allow_redirects=True, timeout=30)
        soup2 = BeautifulSoup(posted.text, "lxml")
        saml_form = next((f for f in soup2.find_all("form") if f.find("input", {"name": "SAMLResponse"})), None)
        if saml_form is None:
            if "SAMLResponse" not in posted.text:
                raise AuthError("SAML login failed (no SAMLResponse; check credentials/MFA).")
            return  # the client auto-followed the POST
        acs = urljoin(posted.url, str(saml_form.get("action")))
        payload = {str(i["name"]): str(i.get("value", "")) for i in saml_form.find_all("input") if i.get("name")}
        session.post(acs, data=payload, allow_redirects=True, timeout=30)

    @staticmethod
    def _is_authenticated(site: mwclient.Site) -> bool:
        info = site.api("query", meta="userinfo")["query"]["userinfo"]
        return bool(info.get("name")) and "anon" not in info

    # -- API call wrapper ---------------------------------------------------
    def api(self, action: str, **params: object) -> dict:
        """Call the Action API with retry + automatic re-login on session loss."""
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            with self._lock:
                self._require_open()
                if self._site is None:
                    self.login()
                assert self._site is not None  # login() sets the site or raises
                try:
                    return self._site.api(action, **params)
                except APIError as exc:
                    last_exc = exc
                    if exc.code in _AUTH_ERROR_CODES:
                        time.sleep(min(2**attempt, 30))
                        self._relogin()
                        continue
                    if exc.code in _BACKOFF_CODES:
                        time.sleep(min(2**attempt, 30))
                        continue
                    raise
                except (MwClientError, ConnectionError, OSError) as exc:
                    last_exc = exc
                    time.sleep(min(2**attempt, 30))
                    continue
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
    def fetch_pages(self, titles: list[str]) -> dict[str, FetchedPage]:
        """Batch-fetch verbatim wikitext + metadata for many titles at once.

        Returns a map keyed by the originally requested title. Handles MediaWiki
        title normalization and redirects so content is attributed correctly.
        """
        results: dict[str, FetchedPage] = {}
        for start in range(0, len(titles), _MAX_TITLES_PER_BATCH):
            batch = titles[start : start + _MAX_TITLES_PER_BATCH]
            resp = self.api(
                "query",
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

    def fetch_page_section(self, title: str, section: int) -> FetchedPage:
        """Fetch one section's verbatim wikitext (server-side ``rvsection`` split)."""
        resp = self.api(
            "query",
            titles=title,
            prop="revisions",
            rvprop="ids|timestamp|size|content",
            rvslots="main",
            rvsection=section,
            redirects=1,
        )
        mapped = self._map_batch([title], resp.get("query", {}))
        return mapped.get(title) or FetchedPage(title, title, None, None, None, None, None, True)

    def page_revisions(self, titles: list[str]) -> dict[str, int | None]:
        """Cheaply fetch current revids (for cache revalidation)."""
        out: dict[str, int | None] = {}
        for start in range(0, len(titles), _MAX_TITLES_PER_BATCH):
            batch = titles[start : start + _MAX_TITLES_PER_BATCH]
            resp = self.api("query", titles="|".join(batch), prop="revisions", rvprop="ids", redirects=1)
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
        return self.api("query", **params)

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
        return self.api("query", **params)

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
        return self.api("query", **params)

    def page_links(self, title: str, *, limit: int, cont: str | None) -> dict:
        """Fetch the internal links on a page via ``prop=links``; returns the raw response."""
        params: dict[str, object] = {"titles": title, "prop": "links", "pllimit": limit}
        if cont:
            params["plcontinue"] = cont
        return self.api("query", **params)

    def statistics(self) -> dict:
        """Return the wiki's ``siteinfo`` statistics as the raw response."""
        return self.api("query", meta="siteinfo", siprop="statistics")

    def close(self) -> None:
        """Close the underlying HTTP session and release the site handle."""
        if self._closed:
            return
        with self._lock:
            try:
                if self._site is not None:
                    self._site.connection.close()
            finally:
                self._site = None
                self._active = None
                self._closed = True
