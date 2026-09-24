"""Bluesky search with public AppView and authenticated PDS-proxy fallback."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from harken.models import Mention
from harken.sources.base import FetchPage, Source

_PUBLIC_APIS = (
    "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
    "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
)
_APPVIEW_SERVICE = "did:web:api.bsky.app#bsky_appview"
_CREATE_SESSION_PATH = "/xrpc/com.atproto.server.createSession"
_SEARCH_PATH = "/xrpc/app.bsky.feed.searchPosts"


class BlueskySource(Source):
    name = "bluesky"
    label = "Bluesky"
    needs_config = False

    # A source instance is recreated for each tracked query. Cache the short-lived
    # access JWT across instances so a multi-keyword Primovezo scan does not log in
    # once per query when Hetzner/public AppView traffic is being blocked.
    _session_cache: dict[tuple[str, str], str] = {}
    _pds_cache: dict[str, str] = {}

    def __init__(
        self,
        identifier: str | None = None,
        app_password: str | None = None,
        pds: str | None = None,
        **options,
    ):
        super().__init__(**options)
        normalized_identifier = (identifier or "").strip().lstrip("@")
        normalized_password = "".join((app_password or "").split())
        self.identifier = normalized_identifier or None
        self.app_password = normalized_password or None
        self.pds = (pds or "").strip().rstrip("/") or None

    def fetch(self, query: str, limit: int = 50) -> list[Mention]:
        return self.fetch_page(query, limit=limit).mentions

    def fetch_page(
        self,
        query: str,
        limit: int = 50,
        *,
        cursor: str | None = None,
        since: datetime | None = None,
    ) -> FetchPage:
        params = {"q": query, "limit": min(limit, 100), "sort": "latest"}
        lang = (self.options.get("lang") or "").strip()
        if lang:
            params["lang"] = lang
        if cursor:
            params["cursor"] = cursor
        if since:
            params["since"] = since.isoformat().replace("+00:00", "Z")

        data = self._search(params)
        mentions: list[Mention] = []
        for post in data.get("posts", []):
            author = post.get("author", {})
            record = post.get("record", {})
            handle = author.get("handle")
            uri = post.get("uri", "")
            rkey = uri.split("/")[-1] if uri else ""
            created = _parse(post.get("indexedAt") or record.get("createdAt"))
            mentions.append(
                Mention(
                    source=self.name,
                    query=query,
                    author=handle,
                    title=None,
                    text=record.get("text", ""),
                    url=f"https://bsky.app/profile/{handle}/post/{rkey}"
                    if handle and rkey
                    else None,
                    created_at=created,
                    score=post.get("likeCount"),
                )
            )
        return FetchPage(mentions, data.get("cursor"))

    def _search(self, params: dict) -> dict:
        # Production operators can configure a dedicated app password to avoid
        # datacenter/CDN blocks entirely. When present, use the authenticated
        # PDS proxy directly instead of probing public AppView hosts first.
        if self.identifier and self.app_password:
            return self._authenticated_search(params)

        last_response: httpx.Response | None = None
        with self._client() as client:
            for endpoint in _PUBLIC_APIS:
                response = client.get(endpoint, params=params)
                last_response = response
                if response.status_code not in {401, 403}:
                    response.raise_for_status()
                    return response.json()

        if last_response is not None and last_response.status_code in {401, 403}:
            raise RuntimeError(
                "Bluesky public search is blocked from this host; configure "
                "HARKEN_BLUESKY_IDENTIFIER and HARKEN_BLUESKY_APP_PASSWORD "
                "for authenticated PDS-proxy access"
            )
        if last_response is not None:
            last_response.raise_for_status()
        raise RuntimeError("Bluesky search failed without a response")

    def _authenticated_search(self, params: dict) -> dict:
        pds = self._authenticated_pds()
        cache_key = (pds, self.identifier or "")
        token = self._session_cache.get(cache_key)
        if not token:
            token = self._create_session()
            self._session_cache[cache_key] = token

        response = self._proxy_search(params, token)
        if response.status_code == 401:
            # Access JWT expired or was revoked. Re-authenticate once with the
            # app password and retry without leaking either credential to logs.
            self._session_cache.pop(cache_key, None)
            token = self._create_session()
            self._session_cache[cache_key] = token
            response = self._proxy_search(params, token)

        if response.status_code in {401, 403}:
            raise RuntimeError(
                "Bluesky authenticated PDS proxy denied search; check the configured "
                "account, app password, and PDS"
            )
        response.raise_for_status()
        return response.json()

    def _authenticated_pds(self) -> str:
        if self.pds:
            return self.pds
        if not self.identifier:
            raise RuntimeError("Bluesky PDS discovery requires an account identifier")
        if "@" in self.identifier:
            raise RuntimeError(
                "Bluesky PDS cannot be auto-discovered from an email identifier; "
                "set HARKEN_BLUESKY_PDS explicitly"
            )

        cached = self._pds_cache.get(self.identifier.casefold())
        if cached:
            self.pds = cached
            return cached

        pds = self._discover_pds(self.identifier)
        self._pds_cache[self.identifier.casefold()] = pds
        self.pds = pds
        return pds

    def _discover_pds(self, handle: str) -> str:
        try:
            with self._client() as client:
                did_response = client.get(f"https://{handle}/.well-known/atproto-did")
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Bluesky DID discovery failed: {type(exc).__name__}"
            ) from None

        if did_response.status_code != 200:
            raise RuntimeError(
                f"Bluesky DID discovery failed with HTTP {did_response.status_code}"
            )

        did = did_response.text.strip()
        if not did.startswith("did:plc:"):
            raise RuntimeError(
                "Bluesky DID discovery returned an unsupported DID; "
                "set HARKEN_BLUESKY_PDS explicitly"
            )

        try:
            with self._client() as client:
                doc_response = client.get(f"https://plc.directory/{did}")
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Bluesky DID document lookup failed: {type(exc).__name__}"
            ) from None

        if doc_response.status_code != 200:
            raise RuntimeError(
                f"Bluesky DID document lookup failed with HTTP {doc_response.status_code}"
            )

        try:
            document = doc_response.json()
            services = document["service"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("Bluesky DID document lookup returned invalid JSON") from None

        for service in services:
            if not isinstance(service, dict):
                continue
            if service.get("id") != "#atproto_pds":
                continue
            endpoint = service.get("serviceEndpoint")
            if isinstance(endpoint, str) and endpoint.startswith("https://"):
                return endpoint.rstrip("/")

        raise RuntimeError(
            "Bluesky DID document contains no HTTPS #atproto_pds service endpoint"
        )

    def _create_session(self) -> str:
        try:
            with self._client() as client:
                response = client.post(
                    self._authenticated_pds() + _CREATE_SESSION_PATH,
                    json={
                        "identifier": self.identifier,
                        "password": self.app_password,
                    },
                )
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Bluesky authenticated fallback login failed: {type(exc).__name__}"
            ) from None

        if response.status_code != 200:
            detail = _safe_error_detail(response)
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"Bluesky authenticated fallback login failed with HTTP "
                f"{response.status_code}{suffix}"
            )

        try:
            payload = response.json()
            access_jwt = payload["accessJwt"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError(
                "Bluesky authenticated fallback login returned invalid JSON"
            ) from None
        if not isinstance(access_jwt, str) or not access_jwt.strip():
            raise RuntimeError("Bluesky authenticated fallback login returned no access token")
        return access_jwt.strip()

    def _proxy_search(self, params: dict, token: str) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {token}",
            "atproto-proxy": _APPVIEW_SERVICE,
        }
        try:
            with self._client(headers=headers) as client:
                return client.get(self._authenticated_pds() + _SEARCH_PATH, params=params)
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Bluesky authenticated proxy search failed: {type(exc).__name__}"
            ) from None


def _safe_error_detail(response: httpx.Response) -> str | None:
    """Return only provider error metadata, never request credentials/tokens."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    message = payload.get("message")
    parts = [
        str(value).strip().replace("\n", " ")[:160]
        for value in (error, message)
        if isinstance(value, str) and value.strip()
    ]
    return " · ".join(parts) or None


def _parse(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
