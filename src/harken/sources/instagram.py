"""Instagram public hashtag media via the Meta Graph API.

The official Instagram hashtag discovery surface is hashtag-based rather than
arbitrary full-text search. Primovezo normalizes each discovery phrase into one
hashtag candidate and still applies the strict Latvian classifier afterwards.
"""

from __future__ import annotations

from datetime import datetime, timezone

from harken.models import Mention
from harken.sources.base import FetchPage, Source


class InstagramSource(Source):
    name = "instagram"
    label = "Instagram"
    needs_config = True

    def __init__(
        self,
        access_token: str | None = None,
        user_id: str | None = None,
        graph_base: str = "https://graph.facebook.com",
        **options,
    ):
        super().__init__(**options)
        self.access_token = (access_token or "").strip() or None
        self.user_id = (user_id or "").strip() or None
        self.graph_base = graph_base.rstrip("/")
        self._hashtag_ids: dict[str, str | None] = {}

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
        if not self.access_token or not self.user_id:
            raise RuntimeError(
                "Instagram requires HARKEN_INSTAGRAM_ACCESS_TOKEN and "
                "HARKEN_INSTAGRAM_USER_ID"
            )

        hashtag = _hashtag_candidate(query)
        if not hashtag:
            return FetchPage([])

        hashtag_id = self._hashtag_ids.get(hashtag)
        if hashtag not in self._hashtag_ids:
            hashtag_id = self._resolve_hashtag(hashtag)
            self._hashtag_ids[hashtag] = hashtag_id
        if not hashtag_id:
            return FetchPage([])

        params = {
            "user_id": self.user_id,
            "fields": "id,caption,media_type,permalink,timestamp",
            "limit": min(limit, 50),
        }
        if cursor:
            params["after"] = cursor

        headers = {"Authorization": f"Bearer {self.access_token}"}
        with self._client(headers=headers) as client:
            response = client.get(
                f"{self.graph_base}/{hashtag_id}/recent_media",
                params=params,
            )
            response.raise_for_status()
            payload = response.json()

        mentions: list[Mention] = []
        for item in payload.get("data", []):
            permalink = item.get("permalink")
            created = _parse_datetime(item.get("timestamp"))
            if since and created <= since:
                continue
            mentions.append(
                Mention(
                    source=self.name,
                    query=query,
                    author=None,
                    title=f"#{hashtag}",
                    text=item.get("caption", ""),
                    url=permalink,
                    created_at=created,
                )
            )

        paging = payload.get("paging") or {}
        next_cursor = (paging.get("cursors") or {}).get("after")
        return FetchPage(mentions, next_cursor)

    def _resolve_hashtag(self, hashtag: str) -> str | None:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        params = {"user_id": self.user_id, "q": hashtag}
        with self._client(headers=headers) as client:
            response = client.get(
                f"{self.graph_base}/ig_hashtag_search",
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
        for item in payload.get("data", []):
            hashtag_id = item.get("id")
            if hashtag_id:
                return str(hashtag_id)
        return None


def _hashtag_candidate(query: str) -> str:
    """Convert a radar phrase into the closest Instagram hashtag candidate."""
    return "".join(character for character in query.casefold() if character.isalnum())


def _parse_datetime(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)
