"""RSS/Atom source — point it at any feed (blogs, news, Google Alerts RSS).

Filters feed entries to those mentioning the query. Configure feeds via
``feeds=[...]`` or the ``HARKEN_RSS_FEEDS`` env var (comma-separated).
"""

from __future__ import annotations

from calendar import timegm
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import feedparser
import httpx

from harken.models import Mention
from harken.sources.base import Source, strip_html


class RSSSource(Source):
    name = "rss"
    label = "RSS"
    needs_config = True  # needs at least one feed URL

    def __init__(self, feeds: list[str] | None = None, **options):
        super().__init__(**options)
        self.feeds = feeds or []

    def fetch(self, query: str, limit: int = 50) -> list[Mention]:
        if not self.feeds:
            raise RuntimeError("RSS requires at least one URL in HARKEN_RSS_FEEDS")
        q = query.casefold()
        mentions: list[Mention] = []
        with self._client() as client:
            for feed_url in self.feeds:
                try:
                    resp = client.get(feed_url)
                    resp.raise_for_status()
                except httpx.HTTPError:
                    continue  # one bad/slow feed shouldn't cost the others their fetch
                parsed = feedparser.parse(resp.content)
                for entry in parsed.entries:
                    # Google Alerts wraps every matched word in <b> tags, so a
                    # multi-word query only matches the plain text.
                    title = strip_html(entry.get("title", ""))
                    text = strip_html(entry.get("summary", ""))
                    blob = f"{title} {text}".casefold()
                    if q not in blob:
                        continue
                    created = _entry_time(entry)
                    mentions.append(
                        Mention(
                            source=self.name,
                            query=query,
                            author=entry.get("author"),
                            title=title or None,
                            text=text,
                            url=_unwrap_redirect(entry.get("link")),
                            created_at=created,
                        )
                    )
        return mentions[:limit]


def _unwrap_redirect(link: str | None) -> str | None:
    """Return the target of a Google Alerts redirect link, or the link unchanged."""
    if not link:
        return link
    parts = urlsplit(link)
    host = parts.netloc.lower()
    if parts.path == "/url" and (host == "google.com" or host.endswith(".google.com")):
        target = parse_qs(parts.query).get("url", [""])[0]
        if target.startswith(("https://", "http://")):
            return target
    return link


def _entry_time(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            # feedparser's struct_time is UTC; time.mktime() interprets it as
            # local time and shifts timestamps on non-UTC hosts.
            return datetime.fromtimestamp(timegm(t), tz=timezone.utc)
    return datetime.now(timezone.utc)
