"""X / Twitter posts via the keyed X API v2 recent-search endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from harken.models import Mention
from harken.sources.base import FetchPage, Source

_API = "https://api.x.com/2/tweets/search/recent"


class XSource(Source):
    name = "x"
    label = "X / Twitter"
    needs_config = True

    def __init__(self, bearer_token: str | None = None, **options):
        super().__init__(**options)
        self.bearer_token = bearer_token

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
        if not self.bearer_token:
            raise RuntimeError("X requires HARKEN_X_BEARER_TOKEN")

        # X requires 10 <= max_results <= 100. We request its minimum and trim
        # locally when a caller asks for fewer than ten rows.
        params = {
            "query": query,
            "max_results": max(10, min(limit, 100)),
            "tweet.fields": "created_at,public_metrics,author_id",
            "expansions": "author_id",
            "user.fields": "username,name",
        }
        if cursor:
            params["next_token"] = cursor
        start_time = _recent_start(since)
        if start_time:
            params["start_time"] = start_time
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        with self._client(headers=headers) as client:
            response = client.get(_API, params=params)
            response.raise_for_status()
            data = response.json()

        users = {
            str(user.get("id")): user
            for user in (data.get("includes") or {}).get("users", [])
            if user.get("id") is not None
        }
        mentions: list[Mention] = []
        # Process every returned tweet: X enforces a 10-row minimum, and
        # `next_token` advances past all of them, so trimming to a smaller
        # `limit` here would silently drop the surplus rows on every page.
        for post in data.get("data", []):
            post_id = post.get("id")
            if not post_id:
                continue
            user = users.get(str(post.get("author_id")), {})
            metrics = post.get("public_metrics") or {}
            username = user.get("username")
            mentions.append(
                Mention(
                    source=self.name,
                    query=query,
                    author=username or user.get("name"),
                    text=post.get("text", ""),
                    url=(
                        f"https://x.com/{username}/status/{post_id}"
                        if username
                        else f"https://x.com/i/web/status/{post_id}"
                    ),
                    created_at=_parse_datetime(post.get("created_at")),
                    score=metrics.get("like_count"),
                )
            )
        return FetchPage(mentions, (data.get("meta") or {}).get("next_token"))


def _parse_datetime(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _recent_start(value: datetime | None, *, now: datetime | None = None) -> str | None:
    """Return a provider-valid recent-search boundary, otherwise use its default window."""
    if value is None:
        return None
    current = now or datetime.now(timezone.utc)
    boundary = value.astimezone(timezone.utc)
    if current - timedelta(days=7) < boundary < current - timedelta(seconds=30):
        return _rfc3339(boundary)
    return None
