"""Mastodon source via an instance's search API.

Defaults to mastodon.social; override with ``instance=`` (or ``HARKEN_MASTODON_INSTANCE``).
Full-text status search is instance-dependent and normally requires a user token;
set ``HARKEN_MASTODON_ACCESS_TOKEN`` when the chosen instance requires one.
"""

from __future__ import annotations

from datetime import datetime, timezone

from harken.models import Mention
from harken.sources.base import FetchPage, Source, strip_html


class MastodonSource(Source):
    name = "mastodon"
    label = "Mastodon"
    needs_config = True

    def __init__(
        self,
        instance: str = "mastodon.social",
        access_token: str | None = None,
        **options,
    ):
        super().__init__(**options)
        self.instance = instance.replace("https://", "").rstrip("/")
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
        url = f"https://{self.instance}/api/v2/search"
        params = {"q": query, "type": "statuses", "limit": min(limit, 40)}
        if cursor:
            params["max_id"] = cursor
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
        with self._client(headers=headers) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        mentions: list[Mention] = []
        statuses = data.get("statuses", [])
        for st in statuses:
            acct = st.get("account", {})
            created = _parse(st.get("created_at"))
            mentions.append(
                Mention(
                    source=self.name,
                    query=query,
                    author=acct.get("acct"),
                    title=None,
                    text=strip_html(st.get("content", "")),
                    url=st.get("url"),
                    created_at=created,
                    score=st.get("favourites_count"),
                )
            )
        next_cursor = None
        if len(statuses) >= min(limit, 40):
            next_cursor = statuses[-1].get("id")
        return FetchPage(mentions, next_cursor)


def _parse(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
