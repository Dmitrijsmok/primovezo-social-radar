"""Lead-radar classification and persistence tests."""

import json
from datetime import datetime, timezone

import pytest

from harken.analyze.leads import classify_leads
from harken.config import Config
from harken.models import Mention
from harken.pipeline import Pipeline


class _Provider:
    available = True

    def complete(self, prompt, system=None, max_tokens=1024):
        records = json.loads(prompt.split("\n\n")[-1])
        return json.dumps(
            {
                record["id"]: {
                    "relevant": "need" in record["text"].lower(),
                    "score": 92 if "need" in record["text"].lower() else 10,
                    "category": "ecommerce" if "need" in record["text"].lower() else "other",
                    "reason_lv": (
                        "Aktīvs pieprasījums"
                        if "need" in record["text"].lower()
                        else "Nav pirkšanas nolūka"
                    ),
                    "reply_lv": (
                        "Varu parādīt piemērotu variantu."
                        if "need" in record["text"].lower()
                        else ""
                    ),
                }
                for record in records
            }
        )


def _mention(text: str, url: str = "https://example.test/1") -> Mention:
    return Mention(
        source="test",
        query="interneta veikals",
        text=text,
        url=url,
        created_at=datetime.now(timezone.utc),
    )


def test_classify_leads_sets_relevance_score_reason_and_reply():
    mention = _mention("I need a new ecommerce platform")
    classify_leads([mention], _Provider())

    assert mention.lead_relevant is True
    assert mention.lead_score == 92
    assert mention.lead_category == "ecommerce"
    assert mention.lead_reason == "Aktīvs pieprasījums"
    assert mention.suggested_reply == "Varu parādīt piemērotu variantu."


def test_classify_leads_rejects_incomplete_provider_response():
    class IncompleteProvider:
        available = True

        def complete(self, *args, **kwargs):
            return "{}"

    with pytest.raises(ValueError, match="incomplete"):
        classify_leads([_mention("need ecommerce")], IncompleteProvider())


def test_pipeline_persists_lead_analysis_for_new_mentions(tmp_path, monkeypatch):
    from harken.sources import REGISTRY

    class LeadSource:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            return [
                Mention(
                    source="leadtest",
                    query=query,
                    text="We need an ecommerce platform",
                    url="https://example.test/lead",
                    created_at=datetime.now(timezone.utc),
                ),
                Mention(
                    source="leadtest",
                    query=query,
                    text="General ecommerce news",
                    url="https://example.test/news",
                    created_at=datetime.now(timezone.utc),
                ),
            ]

    provider = _Provider()
    monkeypatch.setitem(REGISTRY, "leadtest", LeadSource)
    monkeypatch.setattr("harken.pipeline.get_provider", lambda name: provider)

    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "leads.db"),
            sources=["leadtest"],
            lead_enabled=True,
            lead_min_score=70,
            lead_llm_provider="test",
        )
    )
    result = pipe.track("interneta veikals")

    assert result.new == 2
    assert result.lead_analysis_error is None

    mentions = pipe.store.mentions("interneta veikals", limit=None)
    lead_id = next(m.id for m in mentions if "need" in m.text.lower())
    noise_id = next(m.id for m in mentions if "news" in m.text.lower())

    lead = pipe.store.lead_analysis("interneta veikals", lead_id)
    noise = pipe.store.lead_analysis("interneta veikals", noise_id)
    assert lead is not None and lead["relevant"] is True and lead["score"] == 92
    assert noise is not None and noise["relevant"] is False and noise["score"] == 10
    pipe.close()


def test_invalid_lead_threshold_is_rejected():
    with pytest.raises(ValueError, match="at most 100"):
        Config(lead_min_score=101)


def test_lead_alerts_are_deduped_across_keywords(tmp_path, monkeypatch):
    from harken.sources import REGISTRY

    class SharedPostSource:
        def __init__(self, **options):
            pass

        def fetch(self, query, limit=50):
            return [
                Mention(
                    source="shared-lead",
                    query=query,
                    text="We need a new ecommerce platform",
                    url="https://example.test/shared-lead",
                    created_at=datetime.now(timezone.utc),
                )
            ]

    deliveries = []
    monkeypatch.setitem(REGISTRY, "shared-lead", SharedPostSource)
    monkeypatch.setattr("harken.pipeline.get_provider", lambda name: _Provider())
    monkeypatch.setattr(
        "harken.pipeline.send_lead_email",
        lambda settings, query, mentions: deliveries.append(
            (query, [mention.id for mention in mentions])
        ),
    )

    pipe = Pipeline(
        Config(
            db_path=str(tmp_path / "lead-dedupe.db"),
            sources=["shared-lead"],
            lead_enabled=True,
            lead_min_score=70,
            lead_llm_provider="test",
            email_to=["ops@example.test"],
            email_from="harken@example.test",
            smtp_host="smtp.example.test",
            smtp_security="none",
        )
    )

    first = pipe.track("interneta veikals")
    second = pipe.track("e-komercija")

    assert first.alerted == 1
    assert second.new == 1
    assert second.alerted == 0
    assert second.alert_pending == 0
    assert len(deliveries) == 1
    pipe.close()
