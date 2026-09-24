"""TikTok keyword discovery through an optional Apify Actor.

TikTok does not provide a self-serve commercial API for arbitrary organic
keyword search. This adapter therefore uses a user-configured Apify token and
the Clockworks TikTok Scraper Actor by default. It only reads public search
results and never logs the token.
"""

from __future__ import annotations

from datetime import datetime, timezone

from harken.models import Mention
from harken.sources.base import FetchPage, Source

_DEFAULT_ACTOR = "clockworks~tiktok-scraper"
_APIFY_BASE = "https://api.apify.com/v2/actors"


class TikTokSource(Source):
    name = "tiktok"
    label = "TikTok"
    needs_config = True

    def __init__(
        self,
        apify_token: str | None = None,
        actor: str = _DEFAULT_ACTOR,
        proxy_country: str = "LV",
        max_results: int = 15,
        **options,
    ):
        super().__init__(**options)
        self.apify_token = (apify_token or "").strip() or None
        self.actor = (actor or _DEFAULT_ACTOR).strip()
        self.proxy_country = (proxy_country or "LV").strip().upper()
        self.max_results = max(1, min(int(max_results), 100))

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
        if not self.apify_token:
            raise RuntimeError("TikTok requires HARKEN_TIKTOK_APIFY_TOKEN")

        # The synchronous Actor call returns the result dataset directly. It
        # does not expose a stable provider cursor, so Harken relies on its own
        # mention de-duplication and the local time boundary below.
        if cursor:
            return FetchPage([])

        payload = {
            "searchQueries": [query],
            "searchSection": "/video",
            "resultsPerPage": min(limit, self.max_results),
            "proxyCountryCode": self.proxy_country,
            "scrapeRelatedVideos": False,
            "scrapeRelatedSearchWords": False,
            "shouldDownloadAvatars": False,
            "shouldDownloadCovers": False,
            "shouldDownloadMusicCovers": False,
            "shouldDownloadSlideshowImages": False,
            "shouldDownloadVideos": False,
            "downloadSubtitlesOptions": "NEVER_DOWNLOAD_SUBTITLES",
            "commentsPerPost": 0,
            "videoSearchSorting": "LATEST",
        }
        headers = {
            "Authorization": f"Bearer {self.apify_token}",
            "Content-Type": "application/json",
        }
        url = f"{_APIFY_BASE}/{self.actor}/run-sync-get-dataset-items"
        with self._client(headers=headers, timeout=180.0) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()

        mentions: list[Mention] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or item.get("errorCode"):
                continue
            created = _parse_datetime(item.get("createTimeISO"), item.get("createTime"))
            if since and created <= since:
                continue
            author_meta = item.get("authorMeta") or {}
            url = item.get("webVideoUrl")
            post_id = item.get("id")
            if not url and post_id and author_meta.get("name"):
                url = f"https://www.tiktok.com/@{author_meta['name']}/video/{post_id}"
            mentions.append(
                Mention(
                    source=self.name,
                    query=query,
                    author=author_meta.get("name") or author_meta.get("nickName"),
                    text=str(item.get("text") or ""),
                    url=url,
                    created_at=created,
                    score=_int_or_none(item.get("diggCount")),
                )
            )
        return FetchPage(mentions)


def _parse_datetime(iso_value, epoch_value) -> datetime:
    if isinstance(iso_value, str) and iso_value:
        try:
            return datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        except ValueError:
            pass
    if isinstance(epoch_value, (int, float)):
        return datetime.fromtimestamp(epoch_value, tz=timezone.utc)
    return datetime.now(timezone.utc)


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
