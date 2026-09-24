"""YouTube video mentions via the keyed YouTube Data API v3."""

from __future__ import annotations

from datetime import datetime, timezone

from harken.models import Mention
from harken.sources.base import FetchPage, Source, strip_html

_API = "https://www.googleapis.com/youtube/v3/search"


class YouTubeSource(Source):
    name = "youtube"
    label = "YouTube"
    needs_config = True

    def __init__(self, api_key: str | None = None, **options):
        super().__init__(**options)
        self.api_key = api_key

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
        if not self.api_key:
            raise RuntimeError("YouTube requires HARKEN_YOUTUBE_API_KEY")

        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": "date",
            "maxResults": min(limit, 50),
        }
        if cursor:
            params["pageToken"] = cursor
        if since:
            params["publishedAfter"] = _rfc3339(since)

        # Google explicitly recommends the header form so keys cannot leak via
        # request URLs, proxy logs, browser history, or HTTP exception strings.
        with self._client(headers={"X-Goog-Api-Key": self.api_key}) as client:
            response = client.get(_API, params=params)
            response.raise_for_status()
            data = response.json()

        mentions: list[Mention] = []
        for item in data.get("items", []):
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if not video_id:
                continue
            mentions.append(
                Mention(
                    source=self.name,
                    query=query,
                    author=strip_html(snippet.get("channelTitle", "")) or None,
                    title=strip_html(snippet.get("title", "")) or None,
                    text=strip_html(snippet.get("description", "")),
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    created_at=_parse_datetime(snippet.get("publishedAt")),
                )
            )
        return FetchPage(mentions, data.get("nextPageToken"))


def _parse_datetime(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
