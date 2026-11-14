"""RSS source tests — HTTP is mocked, so these run offline and deterministically."""

from datetime import datetime, timezone
from time import struct_time

import httpx
import pytest
import respx

from harken.sources.rss import RSSSource, _entry_time, _unwrap_redirect

_FEED_A = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Acme one</title><description>people are talking</description>
<link>https://example.com/a1</link><author>alice</author>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>Acme two</title><description>more chatter</description>
<link>https://example.com/a2</link><author>alice</author>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
</channel></rss>"""

_FEED_B = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<item><title>Acme three</title><description>from another feed entirely</description>
<link>https://example.com/b1</link><author>bob</author>
<pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
</channel></rss>"""


@respx.mock
def test_fetches_every_configured_feed_even_once_limit_is_hit():
    feed_a = respx.get("https://feeds.example/a.xml").mock(
        return_value=httpx.Response(200, content=_FEED_A)
    )
    feed_b = respx.get("https://feeds.example/b.xml").mock(
        return_value=httpx.Response(200, content=_FEED_B)
    )
    src = RSSSource(feeds=["https://feeds.example/a.xml", "https://feeds.example/b.xml"])
    out = src.fetch("acme", limit=1)

    assert feed_a.called
    assert feed_b.called  # regression: used to never be requested once feed A hit `limit`
    assert len(out) == 1  # the limit is still respected in the returned result


@respx.mock
def test_isolates_a_single_bad_feed():
    respx.get("https://feeds.example/broken.xml").mock(return_value=httpx.Response(500))
    respx.get("https://feeds.example/b.xml").mock(return_value=httpx.Response(200, content=_FEED_B))
    src = RSSSource(feeds=["https://feeds.example/broken.xml", "https://feeds.example/b.xml"])
    out = src.fetch("acme", limit=50)

    assert len(out) == 1
    assert out[0].author == "bob"


@respx.mock
def test_filters_entries_by_query():
    respx.get("https://feeds.example/a.xml").mock(return_value=httpx.Response(200, content=_FEED_A))
    src = RSSSource(feeds=["https://feeds.example/a.xml"])
    out = src.fetch("nonexistent-brand", limit=50)
    assert out == []


def test_uses_the_shared_timeout_client():
    # regression: fetch() used to call feedparser.parse(url) directly, which
    # bypasses Source._client()'s 15s timeout entirely (unbounded hang risk).
    src = RSSSource(feeds=["https://feeds.example/a.xml"])
    client = src._client()
    try:
        assert client.timeout == httpx.Timeout(15.0)
    finally:
        client.close()


def test_requires_at_least_one_feed():
    with pytest.raises(RuntimeError, match="HARKEN_RSS_FEEDS"):
        RSSSource().fetch("acme")


_GOOGLE_ALERTS_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<id>tag:google.com,2013:googlealerts/feed:1</id>
<title>Google Alert - interneta veikals</title>
<entry>
<id>tag:google.com,2013:googlealerts/feed:1:entry:1</id>
<title type="html">K&#257; izveidot &lt;b&gt;interneta&lt;/b&gt; &lt;b&gt;veikalu&lt;/b&gt; bez programm&#275;t&#257;ja</title>
<link href="https://www.google.com/url?rct=j&amp;sa=t&amp;url=https://forums.example.lv/t/123&amp;ct=ga&amp;cd=X&amp;usg=Y"/>
<published>2026-11-14T08:00:00Z</published>
<content type="html">Meklēju &lt;b&gt;interneta&lt;/b&gt; &lt;b&gt;veikalu&lt;/b&gt; ar &amp;quot;Mozello&amp;quot;?</content>
</entry>
</feed>"""


@respx.mock
def test_matches_multi_word_query_inside_google_alerts_bold_markup():
    respx.get("https://www.google.com/alerts/feeds/1/2").mock(
        return_value=httpx.Response(200, content=_GOOGLE_ALERTS_FEED.encode("utf-8"))
    )
    src = RSSSource(feeds=["https://www.google.com/alerts/feeds/1/2"])

    out = src.fetch("meklēju interneta veikalu", limit=50)

    assert len(out) == 1
    assert out[0].title == "Kā izveidot interneta veikalu bez programmētāja"
    assert out[0].text == 'Meklēju interneta veikalu ar "Mozello"?'
    assert out[0].url == "https://forums.example.lv/t/123"
    assert out[0].created_at == datetime(2026, 11, 14, 8, 0, tzinfo=timezone.utc)


def test_unwrap_redirect_only_unwraps_google_redirects():
    google = "https://www.google.com/url?rct=j&sa=t&url=https://shop.example.lv/a&ct=ga"
    assert _unwrap_redirect(google) == "https://shop.example.lv/a"
    other = "https://evil.example/url?url=https://shop.example.lv/a"
    assert _unwrap_redirect(other) == other
    no_target = "https://www.google.com/url?rct=j&sa=t&url=javascript:alert(1)"
    assert _unwrap_redirect(no_target) == no_target
    assert _unwrap_redirect(None) is None


def test_feed_struct_time_is_interpreted_as_utc():
    entry = {"published_parsed": struct_time((2024, 1, 1, 0, 0, 0, 0, 1, 0))}
    assert _entry_time(entry) == datetime(2024, 1, 1, tzinfo=timezone.utc)
