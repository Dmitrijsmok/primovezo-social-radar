"""Pluggable mention sources.

Each source implements :class:`~harken.sources.base.Source`. New sources are
registered in :data:`REGISTRY`. Hacker News and Bluesky need no credentials;
keyed sources are enabled with operator-supplied configuration.
"""

from __future__ import annotations

from harken.sources.base import Source
from harken.sources.bluesky import BlueskySource
from harken.sources.hackernews import HackerNewsSource
from harken.sources.mastodon import MastodonSource
from harken.sources.reddit import RedditSource
from harken.sources.rss import RSSSource
from harken.sources.stackoverflow import StackOverflowSource
from harken.sources.threads import ThreadsSource
from harken.sources.x import XSource
from harken.sources.youtube import YouTubeSource

REGISTRY: dict[str, type[Source]] = {
    HackerNewsSource.name: HackerNewsSource,
    RedditSource.name: RedditSource,
    MastodonSource.name: MastodonSource,
    BlueskySource.name: BlueskySource,
    RSSSource.name: RSSSource,
    StackOverflowSource.name: StackOverflowSource,
    ThreadsSource.name: ThreadsSource,
    XSource.name: XSource,
    YouTubeSource.name: YouTubeSource,
}

# Sources verified to work with zero configuration (no key, no account).
DEFAULT_SOURCES = ["hackernews", "bluesky"]

__all__ = ["Source", "REGISTRY", "DEFAULT_SOURCES"]
