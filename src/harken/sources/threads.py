"""Threads posts via Meta's keyword-search API."""

from __future__ import annotations

from datetime import datetime, timezone

from harken.models import Mention
from harken.sources.base import FetchPage, Source

_API = "https://graph.threads.net/keyword_search"


class ThreadsSource(Source):
    name = "threads"
    label = "Threads"
    needs_config = True

    def __init__(self, access_token: str | None = None, **options):
        super().__init__(**options)
        self.access_token = access_token

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
        if not self.access_token:
            raise RuntimeError("Threads requires HARKEN_THREADS_ACCESS_TOKEN")

        params = {
            "q": query,
            "search_type": "RECENT",
            "search_mode": "KEYWORD",
            "fields": "id,text,username,permalink,timestamp",
            "limit": min(limit, 50),
        }
        if cursor:
            params["after"] = cursor
        if since:
            params["since"] = _rfc3339(since)

        # Keep the OAuth token out of the URL so it cannot leak into logs.
        headers = {"Authorization": f"Bearer {self.access_token}"}
        with self._client(headers=headers) as client:
            response = client.get(_API, params=params)
            response.raise_for_status()
            data = response.json()

        mentions: list[Mention] = []
        for post in data.get("data", []):
            post_id = post.get("id")
            if not post_id:
                continue
            mentions.append(
                Mention(
                    source=self.name,
                    query=query,
                    author=post.get("username"),
                    text=post.get("text", ""),
                    url=post.get("permalink"),
                    created_at=_parse_datetime(post.get("timestamp")),
                )
            )

        paging = data.get("paging") or {}
        cursors = paging.get("cursors") or {}
        return FetchPage(mentions, cursors.get("after"))


def _parse_datetime(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
