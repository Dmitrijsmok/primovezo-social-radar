"""End-to-end pipeline test with mocked HTTP — fetch → analyze → store."""

import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from harken.config import Config
from harken.models import Mention, Sentiment
from harken.pipeline import Pipeline
from harken.store import Store


@respx.mock
def test_track_fetches_analyzes_and_stores(tmp_path):
    hn = {
        "hits": [
            {
                "objectID": "1",
                "title": "Acme is fantastic and fast",
                "author": "a",
                "points": 10,
                "created_at_i": 1_700_000_000,
            },
            {
                "objectID": "2",
                "title": "Acme is buggy and slow, terrible",
                "author": "b",
                "points": 2,
                "created_at_i": 1_700_000_100,
            },
            {
                "objectID": "3",
                "comment_text": "Acme pricing is too expensive",
                "author": "c",
                "story_title": "Acme",
                "created_at_i": 1_700_000_200,
            },
        ]
    }
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(200, json=hn)
    )

    cfg = Config(db_path=str(tmp_path / "t.db"), sources=["hackernews"], source_retries=0)
    pipe = Pipeline(cfg)
    result = pipe.track("acme")

    assert result.fetched == 3
    assert result.new == 3
    assert result.by_source["hackernews"] == 3
    assert not result.errors
    metrics = pipe.store.source_metrics()[0]
    assert metrics["source"] == "hackernews"
    assert metrics["scans_total"] == 1
    assert metrics["errors_total"] == 0
    assert metrics["fetched_total"] == 3
    assert metrics["pages_total"] == 1
    assert metrics["last_success"] == 1

    # sentiment was applied
    rows = pipe.store.mentions(query="acme")
    sentiments = {m.sentiment for m in rows}
    assert Sentiment.POSITIVE in sentiments
    assert Sentiment.NEGATIVE in sentiments
    pipe.close()


@respx.mock
def test_track_isolates_source_failure(tmp_path):
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(return_value=httpx.Response(500))
    cfg = Config(db_path=str(tmp_path / "t.db"), sources=["hackernews"], source_retries=0)
    pipe = Pipeline(cfg)
    result = pipe.track("acme")
    assert "hackernews" in result.errors
    assert result.fetched == 0  # run survived the failure
    metrics = pipe.store.source_metrics()[0]
    assert metrics["scans_total"] == metrics["errors_total"] == 1
    assert metrics["last_success"] == 0
    assert "HTTPStatusError" in metrics["last_error"]
    pipe.close()


def test_unknown_source_is_reported(tmp_path):
    cfg = Config(db_path=str(tmp_path / "t.db"), sources=["nope"])
    pipe = Pipeline(cfg)
    result = pipe.track("acme")
    assert result.errors["nope"] == "unknown source"
    pipe.close()


def test_backfill_cursor_survives_restart_and_completes(tmp_path, monkeypatch):
    from harken.sources import REGISTRY
    from harken.sources.base import FetchPage

    class PagedSource:
        label = "Paged"
        needs_config = False
        calls = []

        def __init__(self, **options):
            pass

        def fetch_page(self, query, limit=50, *, cursor=None, since=None):
            type(self).calls.append((cursor, since))
            pages = {
                None: ("recent", "older-1", datetime(2026, 7, 20, tzinfo=timezone.utc)),
                "older-1": ("older one", "older-2", datetime(2026, 7, 10, tzinfo=timezone.utc)),
                "older-2": ("oldest", None, datetime(2026, 7, 1, tzinfo=timezone.utc)),
            }
            text, next_cursor, created_at = pages[cursor]
            return FetchPage(
                [
                    Mention(
                        source="paged",
                        query=query,
                        text=text,
                        url=f"https://paged.test/{text}",
                        created_at=created_at,
                    )
                ],
                next_cursor,
            )

    monkeypatch.setitem(REGISTRY, "paged", PagedSource)
    path = str(tmp_path / "paged.db")
    config = Config(db_path=path, sources=["paged"], source_retries=0)

    pipe = Pipeline(config)
    initial = pipe.track("acme")
    assert initial.new == 1
    assert pipe.store.source_state("acme", "paged")["backfill_cursor"] == "older-1"
    pipe.close()

    pipe = Pipeline(config)
    first_backfill = pipe.track("acme", backfill=True, pages=1)
    assert first_backfill.new == 1
    assert not first_backfill.backfill_complete["paged"]
    assert pipe.store.source_state("acme", "paged")["backfill_cursor"] == "older-2"
    pipe.close()

    pipe = Pipeline(config)
    completed = pipe.track("acme", backfill=True, pages=5)
    assert completed.new == 1
    assert completed.backfill_complete["paged"]
    assert pipe.store.source_state("acme", "paged")["backfill_complete"]
    assert [mention.text for mention in pipe.store.mentions("acme", limit=None)] == [
        "recent",
        "older one",
        "oldest",
    ]
    pipe.close()


def test_window_scan_uses_explicit_since_and_does_not_move_incremental_state(tmp_path, monkeypatch):
    from harken.sources import REGISTRY
    from harken.sources.base import FetchPage

    class WindowSource:
        label = "Window"
        needs_config = False
        calls = []

        def __init__(self, **options):
            pass

        def fetch_page(self, query, limit=50, *, cursor=None, since=None):
            type(self).calls.append((cursor, since))
            if cursor is None:
                return FetchPage(
                    [
                        Mention(
                            source="window",
                            query=query,
                            text="first",
                            url="https://window.test/1",
                            created_at=datetime(2026, 9, 22, tzinfo=timezone.utc),
                        )
                    ],
                    "older",
                )
            return FetchPage(
                [
                    Mention(
                        source="window",
                        query=query,
                        text="second",
                        url="https://window.test/2",
                        created_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
                    )
                ],
                None,
            )

    WindowSource.calls = []
    monkeypatch.setitem(REGISTRY, "window", WindowSource)
    cutoff = datetime(2026, 9, 18, tzinfo=timezone.utc)
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "window.db"),
            sources=["window"],
            source_retries=0,
        )
    )

    result = pipe.track(
        "acme",
        pages=5,
        since_override=cutoff,
        update_source_state=False,
    )

    assert result.mode == "window"
    assert result.fetched == 2
    assert WindowSource.calls == [(None, cutoff), ("older", cutoff)]
    state = pipe.store.source_state("acme", "window")
    assert state.get("newest_at") is None
    assert state.get("incremental_cursor") is None
    pipe.close()


def test_sample_demo_data_flows_through(tmp_path):
    from harken.analyze.insights import ThemeExtractor
    from harken.analyze.sentiment import LexiconSentiment
    from harken.sample_data import DEMO_QUERY, sample_mentions

    store = Store(tmp_path / "demo.db")
    mentions = sample_mentions()
    sent = LexiconSentiment()
    for m in mentions:
        r = sent.score(m.content)
        m.sentiment = r.label
    store.upsert(mentions)
    stored = store.mentions(query=DEMO_QUERY, limit=1000)
    themes = ThemeExtractor().extract(stored)
    assert len(stored) >= 30
    assert len(themes) >= 3
    summary = store.summary(query=DEMO_QUERY)
    # the sample data is intentionally mixed sentiment
    assert summary["by_sentiment"].get("positive", 0) > 0
    assert summary["by_sentiment"].get("negative", 0) > 0
    store.close()


def test_rejects_blank_query(tmp_path):
    pipe = Pipeline(Config(db_path=str(tmp_path / "t.db"), sources=["hackernews"]))
    with pytest.raises(ValueError, match="must not be empty"):
        pipe.track("   ")
    pipe.close()


def test_optional_llm_failure_is_reported_without_losing_ingestion(tmp_path, monkeypatch):
    class BrokenProvider:
        available = True

        def complete(self, *args, **kwargs):
            raise RuntimeError("model offline")

    monkeypatch.setattr("harken.pipeline.get_provider", lambda name: BrokenProvider())
    cfg = Config(db_path=str(tmp_path / "t.db"), sources=["hackernews"], llm_provider="ollama")
    pipe = Pipeline(cfg)
    mentions = [
        Mention(
            source="hackernews",
            query="acme",
            text="pricing problem",
            url=f"https://x/{i}",
            created_at=datetime.now(timezone.utc),
        )
        for i in range(2)
    ]
    pipe.store.upsert(mentions)
    themes = pipe.themes.extract(pipe.store.mentions(query="acme"))
    error = pipe._maybe_llm_label(themes, mentions)
    assert error == "RuntimeError: model offline"
    pipe.close()


def test_lead_http_failure_reports_status_without_response_body(tmp_path, monkeypatch):
    class FailingProvider:
        available = True

        def complete(self, *args, **kwargs):
            request = httpx.Request("POST", "https://llm.example.test/chat/completions")
            response = httpx.Response(
                429,
                request=request,
                json={"error": {"message": "sensitive upstream detail"}},
            )
            raise httpx.HTTPStatusError(
                "rate limited",
                request=request,
                response=response,
            )

    monkeypatch.setattr("harken.pipeline.get_provider", lambda name: FailingProvider())
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "lead-http-error.db"),
            sources=["hackernews"],
            lead_enabled=True,
            lead_llm_provider="openai",
        )
    )
    mention = Mention(
        source="hackernews",
        query="acme",
        text="need a website",
        url="https://example.test/lead",
        created_at=datetime.now(timezone.utc),
    )
    error = pipe._analyze_leads([mention])
    assert "HTTP 429" in error
    assert "sensitive upstream detail" not in error
    pipe.close()


def test_retryable_source_errors_use_bounded_exponential_backoff(tmp_path, monkeypatch):
    from harken.sources import REGISTRY

    class FlakySource:
        calls = 0

        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            type(self).calls += 1
            if type(self).calls < 3:
                raise httpx.ConnectError(
                    "offline", request=httpx.Request("GET", "https://source.test")
                )
            return [
                Mention(
                    source="flaky",
                    query=query,
                    text="all good",
                    url="https://source.test/1",
                    created_at=datetime.now(timezone.utc),
                )
            ]

    delays = []
    monkeypatch.setitem(REGISTRY, "flaky", FlakySource)
    monkeypatch.setattr("harken.pipeline.time.sleep", delays.append)
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "retry.db"),
            sources=["flaky"],
            source_retries=2,
            retry_backoff=0.25,
        )
    )
    result = pipe.track("acme")
    assert result.fetched == 1
    assert result.retry_counts == {"flaky": 2}
    assert delays == [0.25, 0.5]
    assert pipe.store.source_metrics()[0]["retries_total"] == 2
    pipe.close()


def test_retry_after_header_is_honored(tmp_path, monkeypatch):
    from harken.sources import REGISTRY

    class RateLimitedSource:
        calls = 0

        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            type(self).calls += 1
            if type(self).calls == 1:
                request = httpx.Request("GET", "https://source.test")
                response = httpx.Response(429, headers={"Retry-After": "7"}, request=request)
                raise httpx.HTTPStatusError("rate limited", request=request, response=response)
            return []

    delays = []
    monkeypatch.setitem(REGISTRY, "limited", RateLimitedSource)
    monkeypatch.setattr("harken.pipeline.time.sleep", delays.append)
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "limited.db"),
            sources=["limited"],
            source_retries=1,
        )
    )
    result = pipe.track("acme")
    assert not result.errors
    assert delays == [7.0]
    pipe.close()


def test_bluesky_403_uses_slow_retry_backoff(tmp_path, monkeypatch):
    from harken.sources import REGISTRY

    class ThrottledBluesky:
        calls = 0

        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            type(self).calls += 1
            if type(self).calls < 3:
                request = httpx.Request(
                    "GET", "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
                )
                response = httpx.Response(403, request=request)
                raise httpx.HTTPStatusError("forbidden", request=request, response=response)
            return []

    delays = []
    monkeypatch.setitem(REGISTRY, "bluesky", ThrottledBluesky)
    monkeypatch.setattr("harken.pipeline.time.sleep", delays.append)
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "bluesky-throttle.db"),
            sources=["bluesky"],
            source_retries=2,
            retry_backoff=0.25,
        )
    )
    result = pipe.track("acme")
    assert not result.errors
    assert result.retry_counts == {"bluesky": 2}
    assert delays == [5.0, 10.0]
    pipe.close()


def test_non_bluesky_403_is_not_retried(tmp_path, monkeypatch):
    from harken.sources import REGISTRY

    class ForbiddenSource:
        calls = 0

        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            type(self).calls += 1
            request = httpx.Request("GET", "https://source.test")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    delays = []
    monkeypatch.setitem(REGISTRY, "forbidden", ForbiddenSource)
    monkeypatch.setattr("harken.pipeline.time.sleep", delays.append)
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "forbidden.db"),
            sources=["forbidden"],
            source_retries=3,
        )
    )
    result = pipe.track("acme")
    assert "forbidden" in result.errors
    assert ForbiddenSource.calls == 1
    assert delays == []
    pipe.close()


def test_nonretryable_source_error_fails_immediately(tmp_path, monkeypatch):
    from harken.sources import REGISTRY

    class MisconfiguredSource:
        calls = 0

        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            type(self).calls += 1
            raise RuntimeError("credentials missing")

    delays = []
    monkeypatch.setitem(REGISTRY, "misconfigured", MisconfiguredSource)
    monkeypatch.setattr("harken.pipeline.time.sleep", delays.append)
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "misconfigured.db"),
            sources=["misconfigured"],
            source_retries=3,
        )
    )
    result = pipe.track("acme")
    assert "misconfigured" in result.errors
    assert MisconfiguredSource.calls == 1
    assert delays == []
    pipe.close()


def test_opt_in_llm_sentiment_batches_and_persists_validated_predictions(tmp_path, monkeypatch):
    from harken.sources import REGISTRY

    class ManyMentions:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            return [
                Mention(
                    source="many",
                    query=query,
                    text="excellent product",
                    url=f"https://source.test/{index}",
                    created_at=datetime.now(timezone.utc),
                )
                for index in range(26)
            ]

    class SentimentProvider:
        available = True

        def __init__(self):
            self.calls = []

        def complete(self, prompt, system=None, max_tokens=1024):
            self.calls.append((prompt, system, max_tokens))
            records = json.loads(prompt.split("\n\n", 1)[1])
            return json.dumps(
                {record["id"]: {"label": "negative", "score": -0.75} for record in records}
            )

    provider = SentimentProvider()
    monkeypatch.setitem(REGISTRY, "many", ManyMentions)
    monkeypatch.setattr("harken.pipeline.get_provider", lambda name: provider)
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "llm-sentiment.db"),
            sources=["many"],
            sentiment_analyzer="llm",
            llm_provider="test",
        )
    )
    result = pipe.track("acme")
    assert result.sentiment_error is None
    sentiment_calls = [call for call in provider.calls if "sentiment classifier" in call[1]]
    assert len(sentiment_calls) == 2
    assert "untrusted data" in sentiment_calls[0][0]
    assert all(
        mention.sentiment is Sentiment.NEGATIVE and mention.sentiment_score == -0.75
        for mention in pipe.store.mentions("acme", limit=None)
    )
    pipe.close()


def test_invalid_llm_sentiment_falls_back_to_lexicon_without_losing_ingestion(
    tmp_path, monkeypatch
):
    from harken.sources import REGISTRY

    class OneMention:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            return [
                Mention(
                    source="one",
                    query=query,
                    text="an excellent and reliable product",
                    url="https://source.test/one",
                    created_at=datetime.now(timezone.utc),
                )
            ]

    class InvalidProvider:
        available = True

        def complete(self, *args, **kwargs):
            return '{"0":{"label":"positive","score":42}}'

    monkeypatch.setitem(REGISTRY, "one", OneMention)
    monkeypatch.setattr("harken.pipeline.get_provider", lambda name: InvalidProvider())
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "llm-fallback.db"),
            sources=["one"],
            sentiment_analyzer="llm",
            llm_provider="test",
        )
    )
    result = pipe.track("acme")
    assert result.new == 1
    assert "used lexicon" in result.sentiment_error
    stored = pipe.store.mentions("acme")[0]
    assert stored.sentiment is Sentiment.POSITIVE
    assert stored.sentiment_score > 0
    pipe.close()
