"""Bluesky source via the public AT Protocol search endpoints.

``app.bsky.feed.searchPosts`` can be served by multiple Bluesky AppView hosts.
Some datacenter egresses receive 401/403 from one host while the other remains
available, so the adapter fails over before surfacing an error to the pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone

from harken.models import Mention
from harken.sources.base import FetchPage, Source

_APIS = (
    "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
    "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
)


class BlueskySource(Source):
    name = "bluesky"
    label = "Bluesky"
    needs_config = False

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
        with self._client() as client:
            last_response = None
            for endpoint in _APIS:
                resp = client.get(endpoint, params=params)
                last_response = resp
                if resp.status_code not in {401, 403}:
                    resp.raise_for_status()
                    data = resp.json()
                    break
            else:
                assert last_response is not None
                last_response.raise_for_status()
                raise RuntimeError("Bluesky search failed without a response")

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


def _parse(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
