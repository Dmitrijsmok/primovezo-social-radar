"""Source adapter tests — HTTP is mocked, so these run offline and deterministically."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from harken.sources.bluesky import BlueskySource
from harken.sources.hackernews import HackerNewsSource
from harken.sources.instagram import InstagramSource
from harken.sources.reddit import RedditSource
from harken.sources.stackoverflow import StackOverflowSource
from harken.sources.threads import ThreadsSource
from harken.sources.tiktok import TikTokSource
from harken.sources.x import XSource
from harken.sources.youtube import YouTubeSource


@respx.mock
def test_hackernews_parses_hits():
    payload = {
        "hits": [
            {
                "objectID": "111",
                "title": "Acme is great",
                "author": "alice",
                "points": 42,
                "created_at_i": 1_700_000_000,
            },
            {
                "objectID": "222",
                "comment_text": "I tried <b>Acme</b> and it&#x27;s fast",
                "story_title": "Show HN: Acme",
                "author": "bob",
                "created_at_i": 1_700_000_500,
            },
        ]
    }
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(200, json=payload)
    )
    out = HackerNewsSource().fetch("acme", limit=10)
    assert len(out) == 2
    assert out[0].source == "hackernews"
    assert out[0].author == "alice"
    assert out[0].url == "https://news.ycombinator.com/item?id=111"
    # html stripped + entities decoded
    assert "fast" in out[1].text
    assert "<b>" not in out[1].text
    assert "it's" in out[1].text


@respx.mock
def test_hackernews_page_uses_stable_time_boundaries():
    route = respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [{"objectID": "1", "created_at_i": 1_700_000_000}],
                "page": 0,
                "nbPages": 2,
            },
        )
    )
    since = datetime(2023, 1, 1, tzinfo=timezone.utc)
    page = HackerNewsSource().fetch_page("acme", limit=1, cursor="1700000100", since=since)
    params = route.calls[0].request.url.params
    # Inclusive lower bound so items sharing the boundary second are not skipped
    # when a page cap splits that group (store dedup absorbs the overlap).
    assert "created_at_i<=1700000100" in params["numericFilters"]
    assert f"created_at_i>{int(since.timestamp())}" in params["numericFilters"]
    assert page.next_cursor == "1700000000"
    assert params["typoTolerance"] == "false"


@respx.mock
def test_hackernews_drops_typo_tolerant_false_positives():
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(
            200,
            json={
                "hits": [
                    {"objectID": "1", "title": "Harken release", "created_at_i": 1},
                    {"objectID": "2", "title": "A hardened service", "created_at_i": 2},
                ]
            },
        )
    )
    out = HackerNewsSource().fetch("harken")
    assert [mention.title for mention in out] == ["Harken release"]


@respx.mock
def test_reddit_parses_children():
    payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "title": "Thoughts on Acme?",
                        "selftext": "is it any good",
                        "author": "carol",
                        "score": 7,
                        "permalink": "/r/test/comments/1/thoughts",
                        "created_utc": 1_700_000_000,
                    }
                }
            ]
        }
    }
    respx.get("https://oauth.reddit.com/search").mock(
        return_value=httpx.Response(200, json=payload)
    )
    out = RedditSource(access_token="test-token").fetch("acme")
    assert len(out) == 1
    assert out[0].source == "reddit"
    assert out[0].url == "https://www.reddit.com/r/test/comments/1/thoughts"
    assert out[0].score == 7


@respx.mock
def test_bluesky_parses_posts():
    payload = {
        "posts": [
            {
                "uri": "at://did:plc:abc/app.bsky.feed.post/xyz",
                "author": {"handle": "dave.bsky.social"},
                "record": {"text": "acme rocks", "createdAt": "2026-06-01T12:00:00Z"},
                "likeCount": 3,
            }
        ]
    }
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(200, json=payload)
    )
    out = BlueskySource().fetch("acme")
    assert len(out) == 1
    assert out[0].author == "dave.bsky.social"
    assert out[0].url == "https://bsky.app/profile/dave.bsky.social/post/xyz"


@respx.mock
def test_bluesky_fails_over_to_api_appview_on_forbidden():
    primary = respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(403)
    )
    fallback = respx.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(
            200,
            json={
                "posts": [
                    {
                        "uri": "at://did:plc:abc/app.bsky.feed.post/fallback",
                        "author": {"handle": "fallback.bsky.social"},
                        "record": {
                            "text": "Shopify alternatīva",
                            "createdAt": "2026-09-24T09:00:00Z",
                        },
                    }
                ]
            },
        )
    )

    out = BlueskySource(lang="lv").fetch("Shopify")

    assert primary.called
    assert fallback.called
    assert len(out) == 1
    assert out[0].author == "fallback.bsky.social"


@respx.mock
def test_bluesky_uses_authenticated_pds_proxy_without_probing_public_appviews():
    BlueskySource._session_cache.clear()
    public = respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(403)
    )
    alternate = respx.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(403)
    )
    login = respx.post("https://bsky.social/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(
            200,
            json={
                "accessJwt": "access-jwt",
                "refreshJwt": "refresh-jwt",
                "handle": "radar.bsky.social",
                "did": "did:plc:radar",
            },
        )
    )
    proxied = respx.get("https://bsky.social/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(
            200,
            json={
                "posts": [
                    {
                        "uri": "at://did:plc:abc/app.bsky.feed.post/proxied",
                        "author": {"handle": "prospect.bsky.social"},
                        "record": {
                            "text": "Meklēju Shopify alternatīvu",
                            "createdAt": "2026-09-24T10:00:00Z",
                        },
                    }
                ]
            },
        )
    )

    out = BlueskySource(
        lang="lv",
        identifier="radar.bsky.social",
        app_password="app-password-secret",
        pds="https://bsky.social",
    ).fetch("Shopify")

    assert not public.called and not alternate.called
    assert login.called and proxied.called
    login_payload = json.loads(login.calls[0].request.content)
    assert login_payload == {
        "identifier": "radar.bsky.social",
        "password": "app-password-secret",
    }
    proxy_request = proxied.calls[0].request
    assert proxy_request.headers["authorization"] == "Bearer access-jwt"
    assert proxy_request.headers["atproto-proxy"] == "did:web:api.bsky.app#bsky_appview"
    assert proxy_request.url.params["q"] == "Shopify"
    assert proxy_request.url.params["lang"] == "lv"
    assert len(out) == 1
    assert out[0].author == "prospect.bsky.social"


@respx.mock
def test_bluesky_auto_discovers_account_pds_from_handle_did_document():
    BlueskySource._session_cache.clear()
    BlueskySource._pds_cache.clear()
    did = "did:plc:zhlgr4h57wecaecsmbvugeop"
    pds = "https://coral.us-east.host.bsky.network"

    well_known = respx.get(
        "https://dorsmok.bsky.social/.well-known/atproto-did"
    ).mock(return_value=httpx.Response(200, text=did))
    plc = respx.get(f"https://plc.directory/{did}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": did,
                "alsoKnownAs": ["at://dorsmok.bsky.social"],
                "service": [
                    {
                        "id": "#atproto_pds",
                        "type": "AtprotoPersonalDataServer",
                        "serviceEndpoint": pds,
                    }
                ],
            },
        )
    )
    login = respx.post(f"{pds}/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(
            200,
            json={
                "accessJwt": "access-jwt",
                "refreshJwt": "refresh-jwt",
                "handle": "dorsmok.bsky.social",
                "did": did,
            },
        )
    )
    proxied = respx.get(f"{pds}/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(200, json={"posts": []})
    )

    out = BlueskySource(
        identifier="dorsmok.bsky.social",
        app_password="app-password-secret",
    ).fetch("Shopify")

    assert out == []
    assert well_known.called and plc.called and login.called and proxied.called
    assert login.calls[0].request.url.host == "coral.us-east.host.bsky.network"
    assert proxied.calls[0].request.headers["atproto-proxy"] == (
        "did:web:api.bsky.app#bsky_appview"
    )


@respx.mock
def test_bluesky_public_block_without_auth_has_actionable_sanitized_error():
    BlueskySource._session_cache.clear()
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(403)
    )
    respx.get("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(403)
    )

    with pytest.raises(RuntimeError, match="HARKEN_BLUESKY_APP_PASSWORD") as exc:
        BlueskySource().fetch("acme")

    assert "403" not in str(exc.value)


def test_bluesky_normalizes_handle_prefix_and_app_password_whitespace():
    source = BlueskySource(
        identifier="  @Radar.Bsky.Social  ",
        app_password="abcd- efgh\n-ijkl -mnop",
    )

    assert source.identifier == "Radar.Bsky.Social"
    assert source.app_password == "abcd-efgh-ijkl-mnop"


@respx.mock
def test_bluesky_login_error_is_sanitized_but_actionable():
    BlueskySource._session_cache.clear()
    secret = "abcd-efgh-ijkl-mnop"
    respx.post("https://bsky.social/xrpc/com.atproto.server.createSession").mock(
        return_value=httpx.Response(
            401,
            json={
                "error": "AuthenticationRequired",
                "message": "Invalid identifier or password",
            },
        )
    )

    with pytest.raises(RuntimeError) as exc:
        BlueskySource(
            identifier="radar.bsky.social",
            app_password=secret,
            pds="https://bsky.social",
        ).fetch("Shopify")

    message = str(exc.value)
    assert "HTTP 401" in message
    assert "AuthenticationRequired" in message
    assert "Invalid identifier or password" in message
    assert secret not in message
    assert "accessJwt" not in message


@respx.mock
def test_bluesky_authenticated_proxy_relogs_once_after_expired_session():
    BlueskySource._session_cache.clear()
    login = respx.post("https://bsky.social/xrpc/com.atproto.server.createSession").mock(
        side_effect=[
            httpx.Response(200, json={"accessJwt": "old-jwt"}),
            httpx.Response(200, json={"accessJwt": "new-jwt"}),
        ]
    )
    proxied = respx.get("https://bsky.social/xrpc/app.bsky.feed.searchPosts").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"posts": []}),
        ]
    )

    out = BlueskySource(
        identifier="radar.bsky.social",
        app_password="app-password-secret",
        pds="https://bsky.social",
    ).fetch("Shopify")

    assert out == []
    assert login.call_count == 2
    assert proxied.call_count == 2
    assert proxied.calls[0].request.headers["authorization"] == "Bearer old-jwt"
    assert proxied.calls[1].request.headers["authorization"] == "Bearer new-jwt"


@respx.mock
def test_bluesky_page_preserves_api_cursor_and_since_boundary():
    route = respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(200, json={"posts": [], "cursor": "next-page"})
    )
    since = datetime(2026, 6, 1, tzinfo=timezone.utc)
    page = BlueskySource().fetch_page("acme", cursor="current-page", since=since)
    params = route.calls[0].request.url.params
    assert params["cursor"] == "current-page"
    assert params["since"] == "2026-06-01T00:00:00Z"
    assert page.next_cursor == "next-page"


@respx.mock
def test_bluesky_can_filter_by_language():
    route = respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts").mock(
        return_value=httpx.Response(200, json={"posts": []})
    )
    BlueskySource(lang="lv").fetch("Shopify alternatīva", limit=100)
    params = route.calls[0].request.url.params
    assert params["lang"] == "lv"


@respx.mock
def test_reddit_can_get_an_app_only_oauth_token():
    token = respx.post("https://www.reddit.com/api/v1/access_token").mock(
        return_value=httpx.Response(200, json={"access_token": "oauth-token"})
    )
    search = respx.get("https://oauth.reddit.com/search").mock(
        return_value=httpx.Response(200, json={"data": {"children": []}})
    )
    out = RedditSource(client_id="client", client_secret="secret").fetch("acme")
    assert out == []
    assert token.called and search.called
    assert search.calls[0].request.headers["authorization"] == "Bearer oauth-token"


def test_reddit_requires_oauth_configuration():
    with pytest.raises(RuntimeError, match="requires OAuth"):
        RedditSource().fetch("acme")


@respx.mock
def test_stackoverflow_parses_questions_and_preserves_backoff(monkeypatch):
    StackOverflowSource._backoff_until = 0
    StackOverflowSource._quota_remaining = None
    monkeypatch.setattr("harken.sources.stackoverflow.time.monotonic", lambda: 100.0)
    route = respx.get("https://api.stackexchange.com/2.3/search/advanced").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "question_id": 123,
                        "title": "Using Acme &amp; Python",
                        "body": "<p>Acme is <strong>broken</strong></p>",
                        "link": "https://stackoverflow.com/questions/123/acme",
                        "owner": {"display_name": "Alice &amp; Bob"},
                        "creation_date": 1_700_000_000,
                        "score": 4,
                    }
                ],
                "backoff": 30,
                "quota_remaining": 99,
            },
        )
    )
    out = StackOverflowSource().fetch("acme", limit=5)
    assert len(out) == 1
    assert out[0].title == "Using Acme & Python"
    assert out[0].text == "Acme is broken"
    assert out[0].author == "Alice & Bob"
    assert out[0].score == 4
    with pytest.raises(RuntimeError, match="backoff"):
        StackOverflowSource().fetch("acme", limit=5)
    assert len(route.calls) == 1
    StackOverflowSource._backoff_until = 0
    StackOverflowSource._quota_remaining = None


@respx.mock
def test_stackoverflow_centers_long_body_on_the_match():
    StackOverflowSource._backoff_until = 0
    StackOverflowSource._quota_remaining = None
    respx.get("https://api.stackexchange.com/2.3/search/advanced").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "question_id": 1,
                        "title": "A long question",
                        "body": f"<p>{'before ' * 300}AcmeMatch {'after ' * 300}</p>",
                        "creation_date": 1_700_000_000,
                    }
                ],
                "quota_remaining": 99,
            },
        )
    )
    mention = StackOverflowSource().fetch("acmematch")[0]
    assert "AcmeMatch" in mention.text
    assert len(mention.text) <= 802


@respx.mock
def test_youtube_parses_video_search_and_pagination():
    route = respx.get("https://www.googleapis.com/youtube/v3/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "nextPageToken": "older-videos",
                "items": [
                    {
                        "id": {"videoId": "abc123"},
                        "snippet": {
                            "publishedAt": "2026-07-20T12:30:00Z",
                            "channelTitle": "Alice &amp; Bob",
                            "title": "An &lt;Acme&gt; review",
                            "description": "Acme is fast &amp; friendly",
                        },
                    }
                ],
            },
        )
    )
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    page = YouTubeSource(api_key="secret-key").fetch_page(
        "acme", limit=25, cursor="current", since=since
    )
    params = route.calls[0].request.url.params
    assert "key" not in params
    assert route.calls[0].request.headers["x-goog-api-key"] == "secret-key"
    assert params["type"] == "video"
    assert params["order"] == "date"
    assert params["pageToken"] == "current"
    assert params["publishedAfter"] == "2026-07-01T00:00:00Z"
    assert page.next_cursor == "older-videos"
    assert len(page.mentions) == 1
    assert page.mentions[0].title == "An <Acme> review"
    assert page.mentions[0].author == "Alice & Bob"
    assert page.mentions[0].url == "https://www.youtube.com/watch?v=abc123"


@respx.mock
def test_instagram_resolves_hashtag_and_fetches_recent_public_media():
    hashtag = respx.get("https://graph.facebook.com/ig_hashtag_search").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "17843819167049166", "name": "internetveikals"}]},
        )
    )
    media = respx.get("https://graph.facebook.com/17843819167049166/recent_media").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "ig-1",
                        "caption": "Jauns #internetveikals Latvijā",
                        "media_type": "IMAGE",
                        "permalink": "https://www.instagram.com/p/abc/",
                        "timestamp": "2026-09-23T10:00:00+0000",
                    }
                ],
                "paging": {"cursors": {"after": "next-instagram"}},
            },
        )
    )

    page = InstagramSource(
        access_token="instagram-token",
        user_id="ig-user-id",
    ).fetch_page(
        "interneta veikals",
        limit=25,
        since=datetime(2026, 9, 22, tzinfo=timezone.utc),
    )

    assert hashtag.calls[0].request.headers["authorization"] == "Bearer instagram-token"
    assert hashtag.calls[0].request.url.params["q"] == "internetaveikals"
    assert hashtag.calls[0].request.url.params["user_id"] == "ig-user-id"
    assert media.calls[0].request.headers["authorization"] == "Bearer instagram-token"
    assert media.calls[0].request.url.params["user_id"] == "ig-user-id"
    assert page.next_cursor == "next-instagram"
    assert len(page.mentions) == 1
    assert page.mentions[0].source == "instagram"
    assert page.mentions[0].title == "#internetaveikals"
    assert page.mentions[0].url == "https://www.instagram.com/p/abc/"


@respx.mock
def test_instagram_caches_hashtag_id_across_pages():
    hashtag = respx.get("https://graph.facebook.com/ig_hashtag_search").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "tag-1"}]})
    )
    media = respx.get("https://graph.facebook.com/tag-1/recent_media").mock(
        return_value=httpx.Response(200, json={"data": [], "paging": {}})
    )
    source = InstagramSource(access_token="token", user_id="user")
    source.fetch_page("e-komercija")
    source.fetch_page("e-komercija", cursor="after-1")
    assert hashtag.call_count == 1
    assert media.call_count == 2
    assert media.calls[1].request.url.params["after"] == "after-1"


@respx.mock
def test_threads_parses_keyword_search_and_pagination():
    route = respx.get("https://graph.threads.net/keyword_search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "th-123",
                        "username": "alice",
                        "text": "Looking for an ecommerce platform",
                        "permalink": "https://www.threads.net/@alice/post/th-123",
                        "timestamp": "2026-09-23T09:30:00+0000",
                    }
                ],
                "paging": {"cursors": {"after": "older-threads"}},
            },
        )
    )
    since = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
    page = ThreadsSource(access_token="secret-token").fetch_page(
        "ecommerce", limit=25, cursor="current", since=since
    )
    request = route.calls[0].request
    params = request.url.params
    assert request.headers["authorization"] == "Bearer secret-token"
    assert "access_token" not in params
    assert params["q"] == "ecommerce"
    assert params["search_type"] == "RECENT"
    assert params["search_mode"] == "KEYWORD"
    assert params["after"] == "current"
    assert params["since"] == "2026-09-23T08:00:00Z"
    assert page.next_cursor == "older-threads"
    assert len(page.mentions) == 1
    assert page.mentions[0].source == "threads"
    assert page.mentions[0].author == "alice"
    assert page.mentions[0].url == "https://www.threads.net/@alice/post/th-123"


@respx.mock
def test_threads_reply_keyword_hit_is_normalized_to_root_conversation():
    search = respx.get("https://graph.threads.net/keyword_search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "reply-1",
                        "username": "dmitry.mokeyev",
                        "text": "Man Shopify vairāk nepatīk par Mozello.",
                        "permalink": "https://www.threads.com/@dmitry.mokeyev/post/reply-1",
                        "timestamp": "2026-09-24T09:30:00+0000",
                        "is_reply": True,
                        "root_post": {"id": "root-1"},
                        "replied_to": {"id": "root-1"},
                    }
                ],
                "paging": {},
            },
        )
    )
    root = respx.get("https://graph.threads.net/root-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "root-1",
                "username": "shop.owner",
                "text": (
                    "Internetveikala īpašnieki — kurā platformā izveidojāt savu veikalu, "
                    "un vai ar savu izvēli esat apmierināti?"
                ),
                "permalink": "https://www.threads.com/@shop.owner/post/root-1",
                "timestamp": "2026-09-24T08:00:00+0000",
                "has_replies": True,
            },
        )
    )
    conversation = respx.get("https://graph.threads.net/root-1/conversation").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "reply-1",
                        "username": "dmitry.mokeyev",
                        "text": "Man Shopify vairāk nepatīk par Mozello.",
                        "permalink": "https://www.threads.com/@dmitry.mokeyev/post/reply-1",
                        "timestamp": "2026-09-24T09:30:00+0000",
                        "is_reply": True,
                        "root_post": {"id": "root-1"},
                        "replied_to": {"id": "root-1"},
                    },
                    {
                        "id": "reply-2",
                        "username": "another.user",
                        "text": "Es izvēlējos WooCommerce.",
                        "permalink": "https://www.threads.com/@another.user/post/reply-2",
                        "timestamp": "2026-09-24T09:40:00+0000",
                        "is_reply": True,
                        "root_post": {"id": "root-1"},
                        "replied_to": {"id": "reply-1"},
                    },
                ]
            },
        )
    )

    page = ThreadsSource(access_token="secret-token").fetch_page("Shopify", limit=10)

    assert search.called and root.called and conversation.called
    assert len(page.mentions) == 1
    mention = page.mentions[0]
    assert mention.author == "shop.owner"
    assert mention.url == "https://www.threads.com/@shop.owner/post/root-1"
    assert "kurā platformā" in mention.text
    assert [item.author for item in mention.conversation] == [
        "shop.owner",
        "dmitry.mokeyev",
        "another.user",
    ]
    assert mention.conversation[0].depth == 0
    assert mention.conversation[1].depth == 1
    assert mention.conversation[1].matched is True
    assert mention.conversation[2].depth == 2


@respx.mock
def test_threads_reply_context_survives_missing_full_conversation_permission():
    respx.get("https://graph.threads.net/keyword_search").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "reply-1",
                        "username": "reply.user",
                        "text": "Shopify",
                        "timestamp": "2026-09-24T09:30:00+0000",
                        "is_reply": True,
                        "root_post": {"id": "root-1"},
                        "replied_to": {"id": "root-1"},
                    }
                ]
            },
        )
    )
    respx.get("https://graph.threads.net/root-1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "root-1",
                "username": "prospect",
                "text": "Kuru platformu izvēlēties interneta veikalam?",
                "timestamp": "2026-09-24T08:00:00+0000",
            },
        )
    )
    respx.get("https://graph.threads.net/root-1/conversation").mock(
        return_value=httpx.Response(403)
    )

    mention = ThreadsSource(access_token="token").fetch("Shopify")[0]

    assert mention.author == "prospect"
    assert len(mention.conversation) == 2
    assert mention.conversation[0].author == "prospect"
    assert mention.conversation[1].author == "reply.user"
    assert mention.conversation[1].matched is True


@respx.mock
def test_threads_keyword_search_falls_back_when_relation_fields_are_rejected():
    route = respx.get("https://graph.threads.net/keyword_search").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "unsupported fields"}}),
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "th-plain",
                            "username": "alice",
                            "text": "Shopify",
                            "timestamp": "2026-09-24T09:30:00+0000",
                        }
                    ]
                },
            ),
        ]
    )
    respx.get("https://graph.threads.net/th-plain").mock(return_value=httpx.Response(403))

    mention = ThreadsSource(access_token="token").fetch("Shopify")[0]

    assert route.call_count == 2
    assert (
        route.calls[0].request.url.params["fields"] != route.calls[1].request.url.params["fields"]
    )
    assert mention.author == "alice"
    assert mention.conversation == []


def test_threads_requires_access_token():
    with pytest.raises(RuntimeError, match="HARKEN_THREADS_ACCESS_TOKEN"):
        ThreadsSource().fetch("ecommerce")


@respx.mock
def test_x_parses_posts_authors_metrics_and_pagination():
    route = respx.get("https://api.x.com/2/tweets/search/recent").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "123",
                        "author_id": "42",
                        "text": "Acme shipped today",
                        "created_at": "2026-07-20T12:30:00Z",
                        "public_metrics": {"like_count": 17},
                    }
                ],
                "includes": {"users": [{"id": "42", "username": "alice", "name": "Alice"}]},
                "meta": {"next_token": "older-posts"},
            },
        )
    )
    since = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    page = XSource(bearer_token="secret-token").fetch_page(
        "acme", limit=5, cursor="current", since=since
    )
    request = route.calls[0].request
    params = request.url.params
    assert request.headers["authorization"] == "Bearer secret-token"
    assert params["max_results"] == "10"
    assert params["next_token"] == "current"
    assert params["start_time"] == since.isoformat().replace("+00:00", "Z")
    assert page.next_cursor == "older-posts"
    assert len(page.mentions) == 1
    assert page.mentions[0].author == "alice"
    assert page.mentions[0].score == 17
    assert page.mentions[0].url == "https://x.com/alice/status/123"


@respx.mock
def test_tiktok_research_api_queries_lv_and_includes_voice_to_text():
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=2)
    fresh = int((now - timedelta(hours=1)).timestamp())
    old = int((now - timedelta(days=5)).timestamp())

    token_route = respx.post("https://open.tiktokapis.com/v2/oauth/token/").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "research-token",
                "expires_in": 7200,
                "token_type": "Bearer",
            },
        )
    )
    query_route = respx.post("https://open.tiktokapis.com/v2/research/video/query/").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "videos": [
                        {
                            "id": "123",
                            "video_description": "Vai Shopify ir tā vērts Latvijā?",
                            "voice_to_text": "Man vajag vienkāršāku e-komercijas platformu.",
                            "create_time": fresh,
                            "region_code": "LV",
                            "username": "alice",
                            "like_count": 17,
                        },
                        {
                            "id": "456",
                            "video_description": "Vecs video",
                            "create_time": old,
                            "region_code": "LV",
                            "username": "bob",
                        },
                    ],
                    "cursor": 2,
                    "has_more": False,
                    "search_id": "search-1",
                },
                "error": {"code": "ok", "message": "", "log_id": "log-1"},
            },
        )
    )

    page = TikTokSource(
        client_key="research-key",
        client_secret="research-secret",
        region_code="lv",
    ).fetch_page("Shopify", limit=50, since=since)

    assert token_route.call_count == 1
    token_request = token_route.calls[0].request
    token_body = token_request.read().decode()
    assert "client_key=research-key" in token_body
    assert "client_secret=research-secret" in token_body
    assert "grant_type=client_credentials" in token_body

    request = query_route.calls[0].request
    assert request.headers["authorization"] == "Bearer research-token"
    assert "research-secret" not in str(request.url)
    assert "voice_to_text" in request.url.params["fields"]

    payload = json.loads(request.read())
    assert payload["query"]["and"] == [
        {
            "operation": "IN",
            "field_name": "region_code",
            "field_values": ["LV"],
        },
        {
            "operation": "EQ",
            "field_name": "keyword",
            "field_values": ["Shopify"],
        },
    ]
    assert payload["start_date"] == since.strftime("%Y%m%d")
    assert payload["end_date"] == now.strftime("%Y%m%d")
    assert payload["max_count"] == 50
    assert payload["is_random"] is False

    assert len(page.mentions) == 1
    mention = page.mentions[0]
    assert mention.source == "tiktok"
    assert mention.author == "alice"
    assert mention.score == 17
    assert mention.url == "https://www.tiktok.com/@alice/video/123"
    assert "[voice_to_text] Man vajag vienkāršāku e-komercijas platformu." in mention.text


@respx.mock
def test_tiktok_research_api_returns_opaque_pagination_cursor_and_reuses_token():
    token_route = respx.post("https://open.tiktokapis.com/v2/oauth/token/").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "research-token", "expires_in": 7200},
        )
    )
    query_route = respx.post("https://open.tiktokapis.com/v2/research/video/query/").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "videos": [],
                    "cursor": 100,
                    "has_more": True,
                    "search_id": "search-1",
                },
                "error": {"code": "ok", "message": ""},
            },
        )
    )

    source = TikTokSource(client_key="key", client_secret="secret")
    first = source.fetch_page("e-komercija")
    source.fetch_page("e-komercija", cursor=first.next_cursor)

    assert token_route.call_count == 1
    assert query_route.call_count == 2
    payload = json.loads(query_route.calls[1].request.read())
    assert payload["cursor"] == 100
    assert payload["search_id"] == "search-1"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (YouTubeSource(), "HARKEN_YOUTUBE_API_KEY"),
        (XSource(), "HARKEN_X_BEARER_TOKEN"),
        (InstagramSource(), "HARKEN_INSTAGRAM_ACCESS_TOKEN"),
        (TikTokSource(), "HARKEN_TIKTOK_CLIENT_KEY"),
    ],
)
def test_keyed_sources_fail_before_network_without_credentials(source, message):
    with pytest.raises(RuntimeError, match=message):
        source.fetch("acme")


@respx.mock
def test_youtube_http_error_does_not_expose_api_key():
    respx.get("https://www.googleapis.com/youtube/v3/search").mock(
        return_value=httpx.Response(403, json={"error": {"message": "denied"}})
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        YouTubeSource(api_key="do-not-leak").fetch("acme")
    assert "do-not-leak" not in str(exc_info.value)


@respx.mock
def test_x_omits_stale_or_too_recent_time_boundary():
    route = respx.get("https://api.x.com/2/tweets/search/recent").mock(
        return_value=httpx.Response(200, json={"data": [], "meta": {}})
    )
    source = XSource(bearer_token="token")
    source.fetch_page("acme", since=datetime.now(timezone.utc) - timedelta(days=8))
    source.fetch_page("acme", since=datetime.now(timezone.utc) - timedelta(seconds=5))
    assert all("start_time" not in call.request.url.params for call in route.calls)


def test_mention_id_is_stable_and_dedupes():
    from datetime import datetime, timezone

    from harken.models import Mention

    kw = dict(source="hackernews", query="acme", created_at=datetime.now(timezone.utc))
    a = Mention(url="https://x/1", text="hello", **kw)
    b = Mention(url="https://x/1", text="hello", **kw)
    c = Mention(url="https://x/2", text="hello", **kw)
    assert a.id == b.id  # same url -> same id
    assert a.id != c.id
