"""Primovezo keyword profile tests."""

from types import SimpleNamespace

from typer.testing import CliRunner

from harken import cli
from harken.lead_profiles import PRIMOVEZO_DISCOVERY_KEYWORDS, PRIMOVEZO_LEAD_KEYWORDS

runner = CliRunner()

ORIGINAL_QUERIES = (
    "meklēju interneta veikalu",
    "vajag interneta veikalu",
    "interneta veikala izstrāde",
    "interneta veikala izveide",
    "e-veikala izstrāde",
    "e-veikala izveide",
    "meklēju e-komercijas platformu",
    "e-komercijas platforma",
    "e-komercijas risinājums",
    "Shopify alternatīva",
    "WooCommerce alternatīva",
    "migrācija no Shopify",
    "migrācija no WooCommerce",
    "Shopify",
    "WooCommerce",
    "Mozello",
    "Etsy",
    "interneta veikala platforma",
    "e-veikala platforma",
    "veikala platforma",
    "interneta veikals",
    "internetveikals",
    "e-komercija",
    "e-veikals",
    "pārdot internetā",
    "veikals internetā",
    "online veikals",
)

ADDED_QUERIES = (
    "gribu atvērt veikalu",
    "gribu izveidot e-veikalu",
    "vēlos izveidot interneta veikalu",
    "kā izveidot interneta veikalu",
    "kur izveidot e-veikalu",
    "izveidot savu internetveikalu",
    "meklēju mājaslapas izstrādātāju",
    "kurš taisa e-veikalus",
    "mājaslapa ar e-veikalu",
    "mājaslapa ar maksājumiem",
    "sākt pārdot internetā",
    "pārdot preces internetā",
    "pārdot savus produktus",
    "sākt tirgot internetā",
    "tirdzniecība internetā",
    "savs interneta veikals",
    "internetveikalu",
    "e-veikalu",
    "e veikals",
    "internetveikala",
    "Shopify cenas",
    "Shopify izmaksas",
    "Shopify pārāk dārgs",
    "Shopify problēmas",
    "WooCommerce problēmas",
    "Mozello ierobežojumi",
    "Etsy komisijas",
    "pāriet no Mozello",
    "mainīt e-veikala platformu",
    "pārcelt interneta veikalu",
)


def _profile_queries():
    return [query for _, query in (*PRIMOVEZO_LEAD_KEYWORDS, *PRIMOVEZO_DISCOVERY_KEYWORDS)]


def test_profile_has_57_unique_queries():
    queries = _profile_queries()
    assert len(queries) == 57
    assert len(set(queries)) == 57
    assert len({query.casefold() for query in queries}) == 57


def test_profile_keeps_every_original_query():
    assert set(ORIGINAL_QUERIES) <= set(_profile_queries())


def test_profile_contains_every_added_query_verbatim():
    assert len(ADDED_QUERIES) == 30
    assert set(ADDED_QUERIES) <= set(_profile_queries())
    assert set(_profile_queries()) == set(ORIGINAL_QUERIES) | set(ADDED_QUERIES)


def test_profile_groups_are_unchanged():
    assert all(group == "ecommerce" for group, _ in PRIMOVEZO_LEAD_KEYWORDS)
    assert all(group == "discovery" for group, _ in PRIMOVEZO_DISCOVERY_KEYWORDS)


def test_primovezo_runner_scans_every_query_on_bluesky_and_threads(tmp_path, monkeypatch):
    monkeypatch.delenv("HARKEN_THREADS_ACCESS_TOKEN", raising=False)
    calls = []

    class FakePipeline:
        def __init__(self, config):
            self.config = config

        def track(self, query, pages=3):
            calls.append((query, tuple(self.config.sources), self.config.lead_fallback_alerts))
            return SimpleNamespace(
                errors={},
                retry_counts={},
                lead_analysis_error=None,
                lead_candidates=0,
                lead_candidate_mentions=[],
                alert_error=None,
                alert_pending=0,
                threshold_pending=0,
                alerted=0,
                threshold_alerted=0,
                threshold_events=[],
                fetched=1,
                new=0,
            )

        def close(self):
            pass

    monkeypatch.setenv("HARKEN_LEAD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HARKEN_LLM_API_KEY", "test-key")
    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    result = runner.invoke(
        cli.app,
        [
            "leads",
            "primovezo",
            "--sources",
            "bluesky,threads",
            "--delay",
            "0",
            "--db",
            str(tmp_path / "leads.db"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert [query for query, _, _ in calls] == _profile_queries()
    assert all(sources == ("bluesky", "threads") for _, sources, _ in calls)
    assert all(not fallback for _, _, fallback in calls)
    assert "57 Latvian intent keywords" in result.output
