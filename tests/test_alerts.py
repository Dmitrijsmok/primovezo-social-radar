"""Webhook payload and durable delivery tests."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

import harken.alerts as alerts
from harken.alerts import (
    EmailDeliveryError,
    EmailSettings,
    ResendDeliveryError,
    ResendSettings,
    WebhookDeliveryError,
    email_target_key,
    resend_target_key,
    send_lead_digest_email,
    send_lead_digest_resend,
    send_negative_alert,
    send_negative_email,
    send_threshold_alert,
    send_threshold_email,
    webhook_target_key,
)
from harken.config import Config
from harken.models import Mention, Sentiment
from harken.pipeline import Pipeline
from harken.sources import REGISTRY
from harken.thresholds import ThresholdEvent


class SMTPRecorder:
    instances = []

    def __init__(self, host, port, *, timeout, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.ehlo_calls = 0
        self.starttls_context = None
        self.login_credentials = None
        self.messages = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def ehlo(self):
        self.ehlo_calls += 1

    def starttls(self, *, context):
        self.starttls_context = context

    def login(self, username, password):
        self.login_credentials = (username, password)

    def send_message(self, message, *, from_addr, to_addrs):
        self.messages.append((message, from_addr, to_addrs))


def negative_mention() -> Mention:
    return Mention(
        source="test",
        query="acme",
        author="alice",
        text="Acme is broken and terrible",
        url="https://example.test/post/1",
        created_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        sentiment=Sentiment.NEGATIVE,
        sentiment_score=-0.8,
    )


@respx.mock
def test_generic_webhook_receives_structured_payload():
    route = respx.post("https://alerts.example.test/harken").mock(return_value=httpx.Response(204))
    send_negative_alert("https://alerts.example.test/harken", "acme", [negative_mention()])
    payload = route.calls[0].request.content
    assert b'"event":"harken.negative_mentions"' in payload
    assert b'"count":1' in payload
    assert b'"text":"Harken:' in payload


@respx.mock
def test_slack_webhook_uses_supported_text_payload_only():
    route = respx.post("https://hooks.slack.com/services/T/B/secret").mock(
        return_value=httpx.Response(200, text="ok")
    )
    send_negative_alert("https://hooks.slack.com/services/T/B/secret", "acme", [negative_mention()])
    assert route.calls[0].request.content.startswith(b'{"text":')
    assert b'"event"' not in route.calls[0].request.content


@respx.mock
def test_threshold_webhook_preserves_structured_event_for_generic_targets():
    route = respx.post("https://alerts.example.test/harken").mock(return_value=httpx.Response(204))
    send_threshold_alert(
        "https://alerts.example.test/harken",
        "Harken alert: volume spike",
        {"event": "harken.volume_spike", "query": "acme", "current_count": 12},
    )
    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "text": "Harken alert: volume spike",
        "event": "harken.volume_spike",
        "query": "acme",
        "current_count": 12,
    }


@respx.mock
def test_webhook_errors_do_not_leak_secret_url():
    secret_url = "https://alerts.example.test/a-very-secret-token"
    respx.post(secret_url).mock(return_value=httpx.Response(503))
    with pytest.raises(WebhookDeliveryError) as exc:
        send_negative_alert(secret_url, "acme", [negative_mention()])
    assert "503" in str(exc.value)
    assert "a-very-secret-token" not in str(exc.value)


def test_webhook_url_must_be_http():
    with pytest.raises(ValueError, match="absolute http"):
        webhook_target_key("file:///tmp/alerts")


def test_negative_email_uses_starttls_auth_and_safe_headers(monkeypatch):
    SMTPRecorder.instances = []
    monkeypatch.setattr(alerts.smtplib, "SMTP", SMTPRecorder)
    settings = EmailSettings(
        host="smtp.example.test",
        port=587,
        sender="harken@example.test",
        recipients=("ops@example.test", "owner@example.test"),
        username="mailer",
        password="super-secret-password",
    )
    send_negative_email(settings, "acme\nInjected: no", [negative_mention()])

    smtp = SMTPRecorder.instances[0]
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.test", 587, 15.0)
    assert smtp.ehlo_calls == 2
    assert smtp.starttls_context is not None
    assert smtp.login_credentials == ("mailer", "super-secret-password")
    message, from_addr, to_addrs = smtp.messages[0]
    assert from_addr == "harken@example.test"
    assert to_addrs == ["ops@example.test", "owner@example.test"]
    assert message["Subject"] == "[Harken] 1 new negative mention: acme Injected: no"
    assert "Acme is broken and terrible" in message.get_content()

    first_key = email_target_key(settings)
    reordered = EmailSettings(
        **{
            **settings.__dict__,
            "recipients": tuple(reversed(settings.recipients)),
            "password": "rotated-secret",
        }
    )
    assert email_target_key(reordered) == first_key
    assert "secret" not in first_key


def test_internal_lead_digest_email_marks_draft_as_unsent(monkeypatch):
    SMTPRecorder.instances = []
    monkeypatch.setattr(alerts.smtplib, "SMTP", SMTPRecorder)
    settings = EmailSettings(
        host="smtp.example.test",
        port=587,
        sender="radar@example.test",
        recipients=("owner@example.test",),
        security="none",
    )
    mention = Mention(
        source="bluesky",
        query="interneta veikals",
        author="buyer.bsky.social",
        text="Meklēju e-komercijas platformu interneta veikalam",
        url="https://bsky.app/profile/buyer/post/1",
        created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        lead_relevant=True,
        lead_score=94,
        lead_category="ecommerce",
        lead_reason="Konkrēts e-komercijas pieprasījums",
        suggested_reply="Sveiki! Varu īsi parādīt Primovezo.",
    )

    send_lead_digest_email(settings, [mention])

    message = SMTPRecorder.instances[0].messages[0][0]
    assert message["To"] == "owner@example.test"
    assert message["Subject"] == "[Primovezo Social Radar] 1 new ecommerce lead"
    body = message.get_content()
    assert "Draft reply (not sent automatically)" in body
    assert "buyer.bsky.social" in body
    assert "https://bsky.app/profile/buyer/post/1" in body


@respx.mock
def test_internal_lead_digest_resend_uses_api_and_idempotency():
    route = respx.post("https://api.resend.com/emails").mock(
        return_value=httpx.Response(200, json={"id": "email_123"})
    )
    settings = ResendSettings(
        api_key="re_test_secret",
        sender="noreply@primovezo.com",
        recipients=("owner@example.test",),
    )
    mention = Mention(
        source="bluesky",
        query="interneta veikals",
        author="buyer.bsky.social",
        text="Meklēju e-komercijas platformu interneta veikalam",
        url="https://bsky.app/profile/buyer/post/1",
        created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        lead_relevant=True,
        lead_score=94,
        lead_category="ecommerce",
        lead_reason="Konkrēts e-komercijas pieprasījums",
        suggested_reply="Sveiki! Varu īsi parādīt Primovezo.",
    )

    send_lead_digest_resend(settings, [mention])

    request = route.calls[0].request
    payload = json.loads(request.content)
    assert request.headers["Authorization"] == "Bearer re_test_secret"
    assert request.headers["Idempotency-Key"].startswith("primovezo-lead-digest/")
    assert payload["from"] == "noreply@primovezo.com"
    assert payload["to"] == ["owner@example.test"]
    assert payload["subject"] == "[Primovezo Social Radar] 1 new ecommerce lead"
    assert "Draft reply (not sent automatically)" in payload["text"]

    first_key = request.headers["Idempotency-Key"]
    send_lead_digest_resend(settings, [mention])
    second_key = route.calls[1].request.headers["Idempotency-Key"]
    assert second_key == first_key
    assert resend_target_key(settings).startswith("resend-")


@respx.mock
def test_resend_error_does_not_leak_api_key():
    route = respx.post("https://api.resend.com/emails").mock(
        return_value=httpx.Response(503, text="upstream failure re_test_secret")
    )
    settings = ResendSettings(
        api_key="re_test_secret",
        sender="noreply@primovezo.com",
        recipients=("owner@example.test",),
    )
    mention = negative_mention()
    mention.lead_relevant = True
    mention.lead_score = 90
    mention.lead_category = "ecommerce"

    with pytest.raises(ResendDeliveryError) as exc:
        send_lead_digest_resend(settings, [mention])

    assert "503" in str(exc.value)
    assert "re_test_secret" not in str(exc.value)
    assert route.called


def test_threshold_email_supports_implicit_tls(monkeypatch):
    SMTPRecorder.instances = []
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", SMTPRecorder)
    settings = EmailSettings(
        host="smtp.example.test",
        port=465,
        sender="harken@example.test",
        recipients=("ops@example.test",),
        security="ssl",
    )
    send_threshold_email(
        settings,
        "Harken alert: volume spike",
        {"event": "harken.volume_spike", "query": "acme", "current_count": 12},
    )
    smtp = SMTPRecorder.instances[0]
    assert smtp.context is not None
    assert smtp.starttls_context is None
    message = smtp.messages[0][0]
    assert message["Subject"] == "[Harken] volume spike: acme"
    assert '"current_count": 12' in message.get_content()


def test_email_errors_do_not_leak_credentials(monkeypatch):
    def fail_to_connect(*args, **kwargs):
        raise OSError("connection failed while using super-secret-password")

    monkeypatch.setattr(alerts.smtplib, "SMTP", fail_to_connect)
    settings = EmailSettings(
        host="smtp.example.test",
        port=587,
        sender="harken@example.test",
        recipients=("ops@example.test",),
        username="mailer",
        password="super-secret-password",
    )
    with pytest.raises(EmailDeliveryError) as exc:
        send_negative_email(settings, "acme", [negative_mention()])
    assert "OSError" in str(exc.value)
    assert "super-secret-password" not in str(exc.value)


@pytest.mark.parametrize(
    "settings",
    [
        EmailSettings("smtp.test", 587, "bad address", ("ops@example.test",)),
        EmailSettings("smtp.test", 587, "from@example.test", ("bad address",)),
        EmailSettings("smtp.test", 587, "from@example.test", ("ops@example.test;Bcc:attacker",)),
        EmailSettings("smtp.test", 70000, "from@example.test", ("ops@example.test",)),
        EmailSettings(
            "smtp.test",
            587,
            "from@example.test",
            ("ops@example.test",),
            username="only-user",
        ),
    ],
)
def test_invalid_email_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        email_target_key(settings)


@respx.mock
def test_pipeline_retries_failed_alert_without_duplicate_delivery(tmp_path, monkeypatch):
    class FakeSource:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            mention = negative_mention()
            mention.query = query
            mention.sentiment = None  # the real pipeline owns analysis
            return [mention]

    monkeypatch.setitem(REGISTRY, "fake", FakeSource)
    webhook_url = "https://alerts.example.test/harken"
    route = respx.post(webhook_url).mock(return_value=httpx.Response(503))
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "alerts.db"),
            sources=["fake"],
            webhook_url=webhook_url,
        )
    )

    first = pipe.track("acme")
    assert first.new == 1
    assert first.alerted == 0
    assert first.alert_pending == 1
    assert "503" in first.alert_error

    route.mock(return_value=httpx.Response(204))
    second = pipe.track("acme")
    assert second.new == 0
    assert second.alerted == 1
    assert second.alert_pending == 0

    third = pipe.track("acme")
    assert third.new == 0
    assert third.alerted == 0
    assert len(route.calls) == 2
    pipe.close()


def test_pipeline_retries_email_without_duplicate_delivery(tmp_path, monkeypatch):
    class FakeSource:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            mention = negative_mention()
            mention.query = query
            mention.sentiment = None
            return [mention]

    deliveries = []

    def deliver(settings, query, mentions):
        deliveries.append([mention.id for mention in mentions])
        if len(deliveries) == 1:
            raise RuntimeError("SMTP temporarily unavailable")

    monkeypatch.setitem(REGISTRY, "fake-email", FakeSource)
    monkeypatch.setattr("harken.pipeline.send_negative_email", deliver)
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "email-alerts.db"),
            sources=["fake-email"],
            email_to=["ops@example.test"],
            email_from="harken@example.test",
            smtp_host="smtp.example.test",
            smtp_security="none",
        )
    )

    first = pipe.track("acme")
    assert first.alerted == 0
    assert first.alert_pending == 1
    assert "SMTP temporarily unavailable" in first.alert_error
    second = pipe.track("acme")
    assert second.alerted == 1
    assert second.alert_pending == 0
    third = pipe.track("acme")
    assert third.alerted == 0
    assert len(deliveries) == 2
    pipe.close()


@respx.mock
def test_pipeline_delivers_to_webhook_and_email_independently(tmp_path, monkeypatch):
    class FakeSource:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            mention = negative_mention()
            mention.query = query
            mention.sentiment = None
            return [mention]

    monkeypatch.setitem(REGISTRY, "dual-alert", FakeSource)
    emails = []
    monkeypatch.setattr(
        "harken.pipeline.send_negative_email",
        lambda settings, query, mentions: emails.append([mention.id for mention in mentions]),
    )
    webhook_url = "https://alerts.example.test/dual"
    route = respx.post(webhook_url).mock(return_value=httpx.Response(204))
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "dual-alerts.db"),
            sources=["dual-alert"],
            webhook_url=webhook_url,
            email_to=["ops@example.test"],
            email_from="harken@example.test",
            smtp_host="smtp.example.test",
            smtp_security="none",
        )
    )

    result = pipe.track("acme")
    assert result.alerted == 2
    assert result.alert_pending == 0
    assert route.call_count == 1
    assert len(emails) == 1
    assert pipe.store.operational_stats()["alerts_delivered"] == 2
    pipe.close()


def test_email_only_pipeline_delivers_threshold_episode_once(tmp_path, monkeypatch):
    class EmptySource:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            return []

    event = ThresholdEvent(
        event_type="volume_spike",
        text="Harken alert: synthetic spike",
        payload={"event": "harken.volume_spike", "query": "acme"},
    )
    delivered = []
    monkeypatch.setitem(REGISTRY, "empty-email", EmptySource)
    monkeypatch.setattr(
        "harken.pipeline.evaluate_thresholds",
        lambda *args, **kwargs: {"volume_spike": event, "sentiment_drop": None},
    )
    monkeypatch.setattr(
        "harken.pipeline.send_threshold_email",
        lambda settings, text, payload: delivered.append(payload["event"]),
    )
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "email-threshold.db"),
            sources=["empty-email"],
            email_to=["ops@example.test"],
            email_from="harken@example.test",
            smtp_host="smtp.example.test",
            smtp_security="none",
        )
    )

    first = pipe.track("acme")
    second = pipe.track("acme")
    assert first.threshold_alerted == 1
    assert first.threshold_events == ["volume_spike"]
    assert second.threshold_alerted == 0
    assert delivered == ["harken.volume_spike"]
    pipe.close()


@respx.mock
def test_pipeline_delivers_threshold_episodes_once_while_condition_stays_active(
    tmp_path, monkeypatch
):
    now = datetime.now(timezone.utc)

    class SpikeSource:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            return [
                Mention(
                    source="spike",
                    query=query,
                    text="broken terrible failure",
                    url=f"https://example.test/current/{index}",
                    created_at=now - timedelta(hours=1, minutes=index),
                )
                for index in range(6)
            ]

    monkeypatch.setitem(REGISTRY, "spike", SpikeSource)
    webhook_url = "https://alerts.example.test/thresholds"
    route = respx.post(webhook_url).mock(return_value=httpx.Response(204))
    config = Config(
        db_path=str(tmp_path / "thresholds.db"),
        sources=["spike"],
        webhook_url=webhook_url,
        alert_window_hours=24,
        alert_baseline_windows=2,
        alert_min_mentions=5,
        alert_volume_multiplier=2.0,
        alert_sentiment_drop=0.5,
    )
    pipe = Pipeline(config)
    baseline = [
        Mention(
            source="spike",
            query="acme",
            text="excellent reliable product",
            url=f"https://example.test/baseline/{index}",
            created_at=now - timedelta(hours=30 + index * 7),
            sentiment=Sentiment.POSITIVE,
            sentiment_score=1.0,
        )
        for index in range(6)
    ]
    pipe.store.upsert(baseline)

    first = pipe.track("acme")
    assert first.alerted == 6
    assert first.threshold_alerted == 2
    assert set(first.threshold_events) == {"volume_spike", "sentiment_drop"}
    events = [json.loads(call.request.content)["event"] for call in route.calls]
    assert events == [
        "harken.negative_mentions",
        "harken.volume_spike",
        "harken.sentiment_drop",
    ]

    second = pipe.track("acme")
    assert second.new == 0
    assert second.alerted == 0
    assert second.threshold_alerted == 0
    assert len(route.calls) == 3
    pipe.close()


@respx.mock
def test_backfill_does_not_open_negative_or_threshold_alerts(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)

    class HistoricalSource:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            return [
                Mention(
                    source="history",
                    query=query,
                    text="broken terrible failure",
                    url=f"https://example.test/history/{index}",
                    created_at=now - timedelta(hours=index),
                )
                for index in range(6)
            ]

    monkeypatch.setitem(REGISTRY, "history", HistoricalSource)
    route = respx.post("https://alerts.example.test/no-backfill").mock(
        return_value=httpx.Response(204)
    )
    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "backfill-alerts.db"),
            sources=["history"],
            webhook_url="https://alerts.example.test/no-backfill",
            alert_volume_multiplier=1.0,
            alert_sentiment_drop=0.1,
        )
    )
    result = pipe.track("acme", backfill=True)
    assert result.new == 6
    assert result.alerted == 0
    assert result.threshold_alerted == 0
    assert not route.called
    pipe.close()


def test_standard_alerts_remain_scoped_per_keyword(tmp_path, monkeypatch):
    class SharedNegativeSource:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            mention = negative_mention()
            mention.query = query
            mention.sentiment = None
            return [mention]

    deliveries = []
    monkeypatch.setitem(REGISTRY, "shared-negative", SharedNegativeSource)
    monkeypatch.setattr(
        "harken.pipeline.send_negative_email",
        lambda settings, query, mentions: deliveries.append(
            (query, [mention.id for mention in mentions])
        ),
    )

    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "standard-per-query.db"),
            sources=["shared-negative"],
            email_to=["ops@example.test"],
            email_from="harken@example.test",
            smtp_host="smtp.example.test",
            smtp_security="none",
        )
    )

    first = pipe.track("acme")
    second = pipe.track("acme pricing")

    assert first.alerted == 1
    assert second.alerted == 1
    assert [query for query, _ in deliveries] == ["acme", "acme pricing"]
    pipe.close()
