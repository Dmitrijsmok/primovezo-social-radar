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

    def __init__(
        self,
        identifier: str | None = None,
        app_password: str | None = None,
        pds: str = "https://bsky.social",
        **options,
    ):
        super().__init__(**options)
        self.identifier = (identifier or "").strip() or None
        self.app_password = (app_password or "").strip() or None
        self.pds = (pds or "https://bsky.social").strip().rstrip("/")

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
        last_response: httpx.Response | None = None
        with self._client() as client:
            for endpoint in _PUBLIC_APIS:
                response = client.get(endpoint, params=params)
                last_response = response
                if response.status_code not in {401, 403}:
                    response.raise_for_status()
                    return response.json()

        if self.identifier and self.app_password:
            return self._authenticated_search(params)

        if last_response is not None and last_response.status_code in {401, 403}:
            raise RuntimeError(
                "Bluesky public search is blocked from this host; configure "
                "HARKEN_BLUESKY_IDENTIFIER and HARKEN_BLUESKY_APP_PASSWORD "
                "for authenticated PDS-proxy fallback"
            )
        if last_response is not None:
            last_response.raise_for_status()
        raise RuntimeError("Bluesky search failed without a response")

    def _authenticated_search(self, params: dict) -> dict:
        cache_key = (self.pds, self.identifier or "")
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

        response.raise_for_status()
        return response.json()

    def _create_session(self) -> str:
        try:
            with self._client() as client:
                response = client.post(
                    self.pds + _CREATE_SESSION_PATH,
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
            raise RuntimeError(
                f"Bluesky authenticated fallback login failed with HTTP {response.status_code}"
            )

        try:
            payload = response.json()
            access_jwt = payload["accessJwt"]
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("Bluesky authenticated fallback login returned invalid JSON") from None
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
                return client.get(self.pds + _SEARCH_PATH, params=params)
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Bluesky authenticated proxy search failed: {type(exc).__name__}"
            ) from None


def _parse(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
