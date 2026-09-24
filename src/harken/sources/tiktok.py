"""TikTok public-video discovery through the official Research API.

This adapter requires an approved TikTok Research Tools project. TikTok limits
Research Tools to qualifying non-commercial research, so Primovezo does not
auto-enable this source for commercial lead discovery. The generic Harken
source remains available for approved research projects and content analysis.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from harken.models import Mention
from harken.sources.base import FetchPage, Source

_TOKEN_API = "https://open.tiktokapis.com/v2/oauth/token/"
_VIDEO_API = "https://open.tiktokapis.com/v2/research/video/query/"
_VIDEO_FIELDS = (
    "id",
    "video_description",
    "create_time",
    "region_code",
    "like_count",
    "comment_count",
    "share_count",
    "view_count",
    "hashtag_names",
    "username",
    "voice_to_text",
    "video_duration",
)


class TikTokSource(Source):
    name = "tiktok"
    label = "TikTok Research"
    needs_config = True

    def __init__(
        self,
        client_key: str | None = None,
        client_secret: str | None = None,
        region_code: str = "LV",
        **options,
    ):
        super().__init__(**options)
        self.client_key = (client_key or "").strip() or None
        self.client_secret = (client_secret or "").strip() or None
        self.region_code = (region_code or "LV").strip().upper() or "LV"
        self._access_token: str | None = None
        self._token_expires_at = 0.0

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
        if not self.client_key or not self.client_secret:
            raise RuntimeError(
                "TikTok Research API requires HARKEN_TIKTOK_CLIENT_KEY and "
                "HARKEN_TIKTOK_CLIENT_SECRET"
            )

        state = _decode_cursor(cursor)
        if state is None:
            start_date, end_date = _research_window(since)
            state = {
                "cursor": 0,
                "search_id": None,
                "start_date": start_date,
                "end_date": end_date,
            }

        body = {
            "query": {
                "and": [
                    {
                        "operation": "IN",
                        "field_name": "region_code",
                        "field_values": [self.region_code],
                    },
                    {
                        "operation": "EQ",
                        "field_name": "keyword",
                        "field_values": [query],
                    },
                ]
            },
            "max_count": min(max(int(limit), 1), 100),
            "cursor": int(state["cursor"]),
            "start_date": state["start_date"],
            "end_date": state["end_date"],
            "is_random": False,
        }
        if state.get("search_id"):
            body["search_id"] = state["search_id"]

        data = self._query_videos(body)

        mentions: list[Mention] = []
        for video in data.get("videos") or []:
            if not isinstance(video, dict):
                continue
            video_id = video.get("id") or video.get("video_id")
            if video_id is None:
                continue
            created = _parse_datetime(video.get("create_time"))
            if since and created <= since:
                continue
            username = (video.get("username") or "").strip() or None
            mentions.append(
                Mention(
                    source=self.name,
                    query=query,
                    author=username,
                    text=_content_text(
                        video.get("video_description"),
                        video.get("voice_to_text"),
                    ),
                    url=(
                        f"https://www.tiktok.com/@{username}/video/{video_id}"
                        if username
                        else f"https://www.tiktok.com/video/{video_id}"
                    ),
                    created_at=created,
                    score=_int_or_none(video.get("like_count")),
                )
            )

        next_cursor = None
        if data.get("has_more") and data.get("search_id") and data.get("cursor") is not None:
            next_cursor = _encode_cursor(
                cursor=data["cursor"],
                search_id=data["search_id"],
                start_date=state["start_date"],
                end_date=state["end_date"],
            )
        return FetchPage(mentions, next_cursor)

    def _query_videos(self, body: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {self._client_access_token()}",
            "Content-Type": "application/json",
        }
        params = {"fields": ",".join(_VIDEO_FIELDS)}
        with self._client(headers=headers) as client:
            response = client.post(_VIDEO_API, params=params, json=body)
            if response.status_code == 401:
                self._access_token = None
                self._token_expires_at = 0.0
                headers["Authorization"] = f"Bearer {self._client_access_token()}"
                response = client.post(_VIDEO_API, params=params, json=body)
            response.raise_for_status()
            payload = response.json()

        error = payload.get("error") or {}
        code = error.get("code")
        if code not in (None, "", "ok"):
            message = error.get("message") or "unknown TikTok Research API error"
            raise RuntimeError(f"TikTok Research API error {code}: {message}")
        data = payload.get("data") or {}
        return data if isinstance(data, dict) else {}

    def _client_access_token(self) -> str:
        if self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token

        with self._client(
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        ) as client:
            response = client.post(
                _TOKEN_API,
                data={
                    "client_key": self.client_key,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            )
            response.raise_for_status()
            payload = response.json()

        token = (payload.get("access_token") or "").strip()
        if not token:
            description = payload.get("error_description") or payload.get("error") or "no token"
            raise RuntimeError(f"TikTok Research API authentication failed: {description}")

        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        self._access_token = token
        self._token_expires_at = time.monotonic() + max(0, expires_in - 60)
        return token


def _research_window(
    since: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    earliest = current - timedelta(days=29)
    boundary = since.astimezone(timezone.utc) if since else earliest
    if boundary < earliest:
        boundary = earliest
    if boundary > current:
        boundary = current
    return boundary.strftime("%Y%m%d"), current.strftime("%Y%m%d")


def _encode_cursor(
    *,
    cursor: int,
    search_id: str,
    start_date: str,
    end_date: str,
) -> str:
    return json.dumps(
        {
            "cursor": int(cursor),
            "search_id": str(search_id),
            "start_date": start_date,
            "end_date": end_date,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode_cursor(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        data = json.loads(value)
        return {
            "cursor": int(data["cursor"]),
            "search_id": str(data["search_id"]),
            "start_date": str(data["start_date"]),
            "end_date": str(data["end_date"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid TikTok Research API cursor") from exc


def _content_text(description, voice_to_text) -> str:
    caption = str(description or "").strip()
    voice = str(voice_to_text or "").strip()
    if not voice or voice == caption:
        return caption
    if not caption:
        return voice
    return f"{caption}\n\n[voice_to_text] {voice}"


def _parse_datetime(value) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc)


def _int_or_none(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
