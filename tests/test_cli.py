"""CLI tests — cover the surface the review found at 0% coverage.

Uses Typer's CliRunner; HTTP is mocked with respx where a command talks to a
live source. `_serve` (which blocks on uvicorn.run) is stubbed out.
"""

import csv
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import respx
from typer.testing import CliRunner

from harken import cli
from harken.models import Mention
from harken.store import Store

runner = CliRunner()


def test_demo_and_serve_default_to_the_same_db(tmp_path, monkeypatch):
    # regression test: `demo` used to write to harken-demo.db while `report`
    # and `serve` defaulted to harken.db, so the documented quickstart
    # (harken demo -> harken serve) opened an empty dashboard.
    monkeypatch.chdir(tmp_path)
    seen_dbs = []
    monkeypatch.setattr(cli, "_serve", lambda db, port, *args: seen_dbs.append(db))

    demo_result = runner.invoke(cli.app, ["demo"])
    assert demo_result.exit_code == 0, demo_result.output

    serve_result = runner.invoke(cli.app, ["serve"])
    assert serve_result.exit_code == 0, serve_result.output

    assert seen_dbs == ["harken.db", "harken.db"]


def test_demo_then_report_share_data_with_no_flags(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    demo_result = runner.invoke(cli.app, ["demo", "--no-serve"])
    assert demo_result.exit_code == 0
    assert "No data yet" not in demo_result.output

    report_result = runner.invoke(cli.app, ["report"])
    assert report_result.exit_code == 0
    assert "No data yet" not in report_result.output
    assert "mentions" in report_result.output


def test_demo_dedupes_on_a_second_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(cli.app, ["demo", "--no-serve"])
    first = runner.invoke(cli.app, ["report"])
    runner.invoke(cli.app, ["demo", "--no-serve"])
    second = runner.invoke(cli.app, ["report"])
    assert first.output == second.output  # same totals, not doubled


def test_report_with_no_data_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(cli.app, ["report", "--db", str(tmp_path / "empty.db")])
    assert result.exit_code == 1
    assert "No data yet" in result.output


def test_report_closes_the_store_on_the_no_data_exit_path(tmp_path, monkeypatch):
    # regression test: `report` used to raise typer.Exit(1) before reaching
    # store.close() on the "no data yet" path, leaking the sqlite connection.
    from harken.store import Store

    closed = []
    original_close = Store.close
    monkeypatch.setattr(Store, "close", lambda self: (closed.append(True), original_close(self)))

    runner.invoke(cli.app, ["report", "--db", str(tmp_path / "empty.db")])
    assert closed == [True]


@respx.mock
def test_track_exits_nonzero_when_every_source_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HARKEN_RETRIES", "0")
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(return_value=httpx.Response(500))
    db = str(tmp_path / "t.db")
    result = runner.invoke(cli.app, ["track", "acme", "--sources", "hackernews", "--db", db])
    assert result.exit_code == 1


@respx.mock
def test_track_exits_zero_when_a_source_succeeds(tmp_path):
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(200, json={"hits": []})
    )
    db = str(tmp_path / "t.db")
    result = runner.invoke(cli.app, ["track", "acme", "--sources", "hackernews", "--db", db])
    assert result.exit_code == 0


@respx.mock
def test_backfill_command_resumes_saved_source_cursor(tmp_path):
    route = respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "new",
                            "title": "Acme now",
                            "created_at_i": 1_700_000_100,
                        }
                    ],
                    "page": 0,
                    "nbPages": 2,
                },
            ),
            httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "old",
                            "title": "Acme before",
                            "created_at_i": 1_699_000_000,
                        }
                    ],
                    "page": 0,
                    "nbPages": 1,
                },
            ),
        ]
    )
    db = str(tmp_path / "backfill.db")
    tracked = runner.invoke(
        cli.app,
        ["track", "acme", "--sources", "hackernews", "--limit", "1", "--db", db],
    )
    assert tracked.exit_code == 0, tracked.output
    result = runner.invoke(cli.app, ["backfill", "acme", "--limit", "1", "--db", db])
    assert result.exit_code == 0, result.output
    assert "history complete" in result.output
    with Store(db) as store:
        assert store.summary("acme")["total"] == 2
    assert route.call_count == 2


def test_sources_lists_registry():
    result = runner.invoke(cli.app, ["sources"])
    assert result.exit_code == 0
    assert "hackernews" in result.output
    assert "reddit" in result.output


def test_project_cli_create_add_report_remove_and_delete(tmp_path):
    db = str(tmp_path / "projects.db")
    assert runner.invoke(cli.app, ["demo", "--no-serve", "--db", db]).exit_code == 0
    created = runner.invoke(cli.app, ["project", "create", "Product Suite", "--db", db])
    assert created.exit_code == 0, created.output
    with Store(db) as store:
        project_id = next(
            project["id"] for project in store.projects() if project["name"] == "Product Suite"
        )

    added = runner.invoke(cli.app, ["project", "add", str(project_id), "Quill", "--db", db])
    assert added.exit_code == 0, added.output
    listed = runner.invoke(cli.app, ["project", "list", "--db", db])
    assert listed.exit_code == 0
    assert "Product Suite" in listed.output
    report = runner.invoke(cli.app, ["project", "report", str(project_id), "--db", db])
    assert report.exit_code == 0
    assert "32 mentions across 1 keyword" in report.output

    removed = runner.invoke(cli.app, ["project", "remove", str(project_id), "Quill", "--db", db])
    assert removed.exit_code == 0
    preview = runner.invoke(cli.app, ["project", "delete", str(project_id), "--db", db])
    assert preview.exit_code == 0
    assert "Would delete" in preview.output
    deleted = runner.invoke(cli.app, ["project", "delete", str(project_id), "--yes", "--db", db])
    assert deleted.exit_code == 0
    with Store(db) as store:
        assert store.project(project_id) is None
        assert store.summary("Quill")["total"] == 32


def test_user_cli_lifecycle_and_last_admin_protection(tmp_path):
    db = str(tmp_path / "users.db")
    created = runner.invoke(
        cli.app,
        ["user", "create", "admin", "--db", db],
        input="admin password 123\nadmin password 123\n",
    )
    assert created.exit_code == 0, created.output
    assert "created admin as admin" in created.output

    viewer = runner.invoke(
        cli.app,
        ["user", "create", "reader", "--role", "viewer", "--db", db],
        input="reader password 123\nreader password 123\n",
    )
    assert viewer.exit_code == 0, viewer.output
    listed = runner.invoke(cli.app, ["user", "list", "--db", db])
    assert listed.exit_code == 0
    assert "admin" in listed.output and "reader" in listed.output
    assert "password_hash" not in listed.output

    protected = runner.invoke(cli.app, ["user", "disable", "admin", "--db", db])
    assert protected.exit_code != 0
    assert "last active admin" in protected.output
    disabled = runner.invoke(cli.app, ["user", "disable", "reader", "--db", db])
    assert disabled.exit_code == 0
    assert runner.invoke(cli.app, ["user", "enable", "reader", "--db", db]).exit_code == 0
    assert runner.invoke(cli.app, ["user", "delete", "reader", "--yes", "--db", db]).exit_code == 0


def test_user_cli_password_policy(tmp_path):
    db = str(tmp_path / "password-policy.db")
    result = runner.invoke(
        cli.app,
        ["user", "create", "admin", "--db", db],
        input="too-short\ntoo-short\n",
    )
    assert result.exit_code != 0
    assert "at least 12 characters" in result.output


@respx.mock
def test_track_can_assign_new_keyword_to_project(tmp_path):
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(200, json={"hits": []})
    )
    db = str(tmp_path / "project-track.db")
    with Store(db) as store:
        project = store.create_project("Focused")
    result = runner.invoke(
        cli.app,
        [
            "track",
            "project-only",
            "--sources",
            "hackernews",
            "--project",
            str(project["id"]),
            "--db",
            db,
        ],
    )
    assert result.exit_code == 0, result.output
    with Store(db) as store:
        assert store.queries(project_id=project["id"]) == ["project-only"]
        assert "project-only" not in store.queries(project_id=1)


def test_primovezo_lead_runner_requires_llm_provider(tmp_path):
    result = runner.invoke(
        cli.app,
        ["leads", "primovezo", "--db", str(tmp_path / "leads.db"), "--delay", "0"],
    )
    assert result.exit_code != 0
    assert "HARKEN_LEAD_LLM_PROVIDER" in result.output


def test_primovezo_lead_runner_scans_profile_with_delay(tmp_path, monkeypatch):
    monkeypatch.delenv("HARKEN_THREADS_ACCESS_TOKEN", raising=False)
    calls = []
    delays = []
    closed = []

    class FakePipeline:
        def __init__(self, config):
            self.config = config

        def track(self, query, pages=3):
            calls.append(
                (
                    query,
                    pages,
                    self.config.lead_enabled,
                    self.config.lead_fallback_alerts,
                    tuple(self.config.sources),
                    self.config.source_retries,
                    self.config.retry_backoff,
                    self.config.bluesky_lang,
                )
            )
            return SimpleNamespace(
                errors={},
                retry_counts={},
                lead_analysis_error=None,
                lead_candidates=1,
                lead_candidate_mentions=[],
                alert_error=None,
                alert_pending=0,
                threshold_pending=0,
                alerted=0,
                threshold_alerted=0,
                threshold_events=[],
                fetched=2,
                new=1,
            )

        def close(self):
            closed.append(True)

    monkeypatch.setenv("HARKEN_LEAD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HARKEN_LLM_API_KEY", "test-key")
    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli.time, "sleep", delays.append)

    result = runner.invoke(
        cli.app,
        [
            "leads",
            "primovezo",
            "--sources",
            "bluesky",
            "--limit",
            "10",
            "--pages",
            "2",
            "--delay",
            "0.25",
            "--db",
            str(tmp_path / "leads.db"),
        ],
    )

    assert result.exit_code == 0, result.output
    expected_queries = [
        query
        for _, query in (*cli.PRIMOVEZO_LEAD_KEYWORDS, *cli.PRIMOVEZO_DISCOVERY_KEYWORDS)
    ]
    assert [query for query, *_ in calls] == expected_queries
    assert all(pages == 2 for _, pages, _, _, _, _, _, _ in calls)
    assert all(lead_enabled for _, _, lead_enabled, _, _, _, _, _ in calls)
    assert all(not fallback for _, _, _, fallback, _, _, _, _ in calls)
    assert all(sources == ("bluesky",) for _, _, _, _, sources, _, _, _ in calls)
    assert all(retries == 3 for _, _, _, _, _, retries, _, _ in calls)
    assert all(backoff == 10.0 for _, _, _, _, _, _, backoff, _ in calls)
    assert all(lang == "lv" for _, _, _, _, _, _, _, lang in calls)
    assert delays == [0.25] * (len(expected_queries) - 1)
    assert closed == [True]
    assert "Primovezo lead scan" in result.output
    expected = len(cli.PRIMOVEZO_LEAD_KEYWORDS) + len(cli.PRIMOVEZO_DISCOVERY_KEYWORDS)
    assert f"{expected * 2} fetched" in result.output
    assert f"{expected} qualified match(es)" in result.output
    assert all(group == "ecommerce" for group, _ in cli.PRIMOVEZO_LEAD_KEYWORDS)


def test_primovezo_recent_scans_bounded_window_without_delivery(tmp_path, monkeypatch):
    monkeypatch.delenv("HARKEN_THREADS_ACCESS_TOKEN", raising=False)
    calls = []

    class FakePipeline:
        def __init__(self, config):
            self.config = config
            assert config.sources == ["bluesky"]
            assert config.bluesky_lang == "lv"
            assert config.email_to == []
            assert config.resend_api_key is None
            assert config.resend_to == []
            assert config.webhook_url is None

        def track(self, query, **kwargs):
            calls.append((query, kwargs))
            return SimpleNamespace(
                errors={},
                retry_counts={},
                lead_analysis_error=None,
                lead_candidates=0,
                lead_candidate_mentions=[],
                fetched=1,
                new=1,
            )

        def close(self):
            pass

    monkeypatch.setenv("HARKEN_LEAD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HARKEN_LLM_API_KEY", "test-key")
    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli, "get_provider", lambda name: SimpleNamespace(available=True))

    result = runner.invoke(
        cli.app,
        [
            "leads",
            "recent",
            "--days",
            "5",
            "--pages",
            "4",
            "--delay",
            "0",
            "--db",
            str(tmp_path / "recent.db"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == len(cli.PRIMOVEZO_DISCOVERY_KEYWORDS)
    assert all(kwargs["pages"] == 4 for _, kwargs in calls)
    assert all(kwargs["classify_fetched"] is True for _, kwargs in calls)
    assert all(kwargs["update_source_state"] is False for _, kwargs in calls)
    assert all(kwargs["since_override"] is not None for _, kwargs in calls)
    assert "recent 5-day scan" in result.output
    assert "No email was sent" in result.output


def test_primovezo_auto_enables_threads_when_token_is_configured(tmp_path, monkeypatch):
    seen = []

    class FakePipeline:
        def __init__(self, config):
            seen.append((tuple(config.sources), config.threads_access_token))

        def track(self, query, pages=3):
            return SimpleNamespace(
                errors={},
                retry_counts={},
                lead_analysis_error=None,
                lead_candidates=0,
                lead_candidate_mentions=[],
                fetched=0,
                new=0,
            )

        def close(self):
            pass

    monkeypatch.setenv("HARKEN_LEAD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HARKEN_LLM_API_KEY", "test-key")
    monkeypatch.setenv("HARKEN_THREADS_ACCESS_TOKEN", "threads-token")
    monkeypatch.setattr(cli, "_prepare_primovezo_threads", lambda cfg: None)
    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli, "get_provider", lambda name: SimpleNamespace(available=True))

    result = runner.invoke(
        cli.app,
        ["leads", "primovezo", "--delay", "0", "--db", str(tmp_path / "auto.db")],
    )

    assert result.exit_code == 0, result.output
    assert seen == [(("bluesky", "threads"), "threads-token")]


def test_primovezo_recent_auto_enables_threads_when_token_is_configured(
    tmp_path, monkeypatch
):
    seen = []

    class FakePipeline:
        def __init__(self, config):
            seen.append((tuple(config.sources), config.threads_access_token))

        def track(self, query, **kwargs):
            return SimpleNamespace(
                errors={},
                retry_counts={},
                lead_analysis_error=None,
                lead_candidates=0,
                lead_candidate_mentions=[],
                fetched=0,
                new=0,
            )

        def close(self):
            pass

    monkeypatch.setenv("HARKEN_LEAD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HARKEN_LLM_API_KEY", "test-key")
    monkeypatch.setenv("HARKEN_THREADS_ACCESS_TOKEN", "threads-token")
    monkeypatch.setattr(cli, "_prepare_primovezo_threads", lambda cfg: None)
    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli, "get_provider", lambda name: SimpleNamespace(available=True))

    result = runner.invoke(
        cli.app,
        ["leads", "recent", "--days", "5", "--delay", "0", "--db", str(tmp_path / "recent.db")],
    )

    assert result.exit_code == 0, result.output
    assert seen == [(("bluesky", "threads"), "threads-token")]


def test_threads_status_reports_health_without_token_value(monkeypatch):
    from harken.threads_auth import ThreadsTokenInfo

    secret = "never-print-this-token"
    monkeypatch.setenv("HARKEN_THREADS_ACCESS_TOKEN", secret)
    monkeypatch.setattr(
        cli,
        "inspect_threads_token",
        lambda token: ThreadsTokenInfo(
            valid=True,
            scopes=("threads_basic", "threads_keyword_search"),
            expires_at=datetime(2026, 11, 23, tzinfo=timezone.utc),
        ),
    )

    result = runner.invoke(cli.app, ["threads", "status"])

    assert result.exit_code == 0, result.output
    assert "Threads API: connected" in result.output
    assert "keyword_search: available" in result.output
    assert "Auto-refresh: enabled" in result.output
    assert secret not in result.output


def test_primovezo_runner_rejects_reddit(monkeypatch):
    monkeypatch.setenv("HARKEN_LEAD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HARKEN_LLM_API_KEY", "test-key")
    monkeypatch.setattr(cli, "get_provider", lambda name: SimpleNamespace(available=True))

    result = runner.invoke(
        cli.app,
        ["leads", "primovezo", "--sources", "bluesky,reddit", "--delay", "0"],
    )

    assert result.exit_code != 0
    assert "does not use: reddit" in result.output
    assert "bluesky, threads, x" in result.output


def test_primovezo_runner_sends_one_operational_warning_for_partial_failures(
    tmp_path, monkeypatch
):
    warnings = []
    calls = []

    class FakeStore:
        def enqueue_alerts(self, *args, **kwargs):
            return None

        def pending_alerts_for_target(self, *args, **kwargs):
            return []

    class FakePipeline:
        def __init__(self, config):
            self.store = FakeStore()

        def track(self, query, pages=3):
            calls.append(query)
            errors = {"bluesky": "HTTPStatusError: HTTP 403"} if len(calls) == 1 else {}
            return SimpleNamespace(
                errors=errors,
                retry_counts={"bluesky": 3} if errors else {},
                lead_analysis_error=None,
                lead_candidates=0,
                lead_candidate_mentions=[],
                fetched=0,
                new=0,
            )

        def close(self):
            pass

    monkeypatch.setenv("HARKEN_LEAD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HARKEN_LLM_API_KEY", "test-key")
    monkeypatch.setenv("HARKEN_RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("HARKEN_RESEND_FROM", "noreply@primovezo.com")
    monkeypatch.setenv("HARKEN_RESEND_TO", "owner@example.test")
    monkeypatch.delenv("HARKEN_THREADS_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        cli,
        "_prepare_primovezo_threads",
        lambda cfg: "Threads token refresh failed; current valid token kept",
    )
    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli, "get_provider", lambda name: SimpleNamespace(available=True))
    monkeypatch.setattr(
        cli,
        "send_operational_resend",
        lambda settings, *, issues, run_label: warnings.append(
            (settings, list(issues), run_label)
        ),
    )

    result = runner.invoke(
        cli.app,
        ["leads", "primovezo", "--delay", "0", "--db", str(tmp_path / "ops.db")],
    )

    assert result.exit_code == 0, result.output
    assert len(warnings) == 1
    settings, issues, run_label = warnings[0]
    assert settings.recipients == ("owner@example.test",)
    assert run_label == "daily ecommerce scan"
    assert any("Threads token refresh failed" in issue for issue in issues)
    assert any("bluesky fetch failed" in issue for issue in issues)
    assert "sent one operational warning" in result.output


def test_primovezo_runner_sends_one_internal_digest(tmp_path, monkeypatch):
    db_path = tmp_path / "digest.db"
    lead = Mention(
        source="bluesky",
        query=cli.PRIMOVEZO_LEAD_KEYWORDS[0][1],
        author="buyer.bsky.social",
        text="Meklēju e-komercijas platformu jaunam interneta veikalam",
        url="https://bsky.app/profile/buyer/post/1",
        created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        lead_relevant=True,
        lead_score=94,
        lead_category="ecommerce",
        lead_reason="Konkrēts e-komercijas platformas pieprasījums",
        suggested_reply="Sveiki! Ja vēl salīdzināt platformas, varu īsi parādīt Primovezo.",
    )
    delivered = []
    calls = []

    class FakePipeline:
        def __init__(self, config):
            self.config = config
            assert config.email_to == []
            assert config.resend_api_key is None
            assert config.resend_to == []
            assert config.webhook_url is None
            self.store = Store(config.db_path)

        def track(self, query, pages=3):
            calls.append(query)
            candidates = []
            if len(calls) == 1:
                candidate = lead.model_copy(update={"query": query})
                self.store.upsert([candidate])
                self.store.save_lead_analysis([candidate])
                candidates = [candidate]
            return SimpleNamespace(
                errors={},
                retry_counts={},
                lead_analysis_error=None,
                lead_candidates=len(candidates),
                lead_candidate_mentions=candidates,
                fetched=len(candidates),
                new=len(candidates),
            )

        def close(self):
            self.store.close()

    monkeypatch.setenv("HARKEN_LEAD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("HARKEN_LLM_API_KEY", "test-key")
    monkeypatch.setenv("HARKEN_RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("HARKEN_RESEND_FROM", "noreply@primovezo.com")
    monkeypatch.setenv("HARKEN_RESEND_TO", "owner@example.test")
    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    monkeypatch.setattr(cli, "get_provider", lambda name: SimpleNamespace(available=True))
    monkeypatch.setattr(
        cli,
        "send_lead_digest_resend",
        lambda settings, mentions: delivered.append((settings, list(mentions))),
    )

    result = runner.invoke(
        cli.app,
        ["leads", "primovezo", "--delay", "0", "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert len(delivered) == 1
    settings, mentions = delivered[0]
    assert settings.sender == "noreply@primovezo.com"
    assert settings.recipients == ("owner@example.test",)
    assert [mention.id for mention in mentions] == [lead.id]
    assert "delivered 1 lead(s) in one internal digest" in result.output

    with Store(db_path) as store:
        target = "lead-" + cli.resend_target_key(settings)
        assert store.pending_alerts_for_target(target) == []


def test_leads_report_shows_one_unique_post_for_overlapping_queries(tmp_path):
    db_path = tmp_path / "leads-report.db"
    with Store(db_path) as store:
        rows = []
        for query, score in (("meklēju interneta veikalu", 90), ("e-komercijas platforma", 95)):
            mention = Mention(
                source="bluesky",
                query=query,
                author="seller.bsky.social",
                text="Meklēju e-komercijas platformu interneta veikalam",
                url="https://bsky.app/profile/seller/post/1",
                created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
                lead_relevant=True,
                lead_score=score,
                lead_category="ecommerce",
                lead_reason="Aktīvs e-komercijas pieprasījums",
                suggested_reply="Varu parādīt Primovezo e-komercijas platformu.",
            )
            rows.append(mention)
        store.upsert(rows)
        store.save_lead_analysis(rows)

    result = runner.invoke(
        cli.app,
        ["leads", "report", "--db", str(db_path), "--min-score", "70"],
    )

    assert result.exit_code == 0, result.output
    assert "Unique qualified leads: 1" in result.output
    assert "95/100" in result.output
    assert "meklēju interneta veikalu" in result.output
    assert "e-komercijas platforma" in result.output


def test_logs_is_shortcut_for_primovezo_leads_report(tmp_path):
    db_path = tmp_path / "logs-alias.db"
    with Store(db_path) as store:
        lead = Mention(
            source="bluesky",
            query="Shopify alternatīva",
            author="buyer.bsky.social",
            text="Meklēju Shopify alternatīvu savam veikalam",
            url="https://bsky.app/profile/buyer/post/1",
            created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            lead_relevant=True,
            lead_score=91,
            lead_category="ecommerce",
            lead_reason="Latvisks e-komercijas pieprasījums",
            suggested_reply="Varu īsi parādīt Primovezo.",
        )
        store.upsert([lead])
        store.save_lead_analysis([lead])

    full = runner.invoke(cli.app, ["leads", "report", "--db", str(db_path)])
    short = runner.invoke(cli.app, ["logs", "--db", str(db_path)])

    assert full.exit_code == 0, full.output
    assert short.exit_code == 0, short.output
    assert short.output == full.output
    assert "https://bsky.app/profile/buyer/post/1" in short.output


def test_leads_reclassify_updates_existing_foreign_false_positive(tmp_path, monkeypatch):
    db_path = tmp_path / "reclassify.db"
    query = "Shopify alternatīva"
    foreign = Mention(
        source="bluesky",
        query=query,
        author="mayonice.bsky.social",
        text="Bueno pues a buscar una alternativa a etsy 🙂",
        url="https://bsky.app/profile/mayonice.bsky.social/post/1",
        created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        lead_relevant=True,
        lead_score=85,
        lead_category="ecommerce",
        lead_reason="Vecā klasifikācija",
        suggested_reply="Vecais drafts",
    )
    with Store(db_path) as store:
        store.upsert([foreign])
        store.save_lead_analysis([foreign])

    class LatviaProvider:
        available = True

        def complete(self, prompt, system=None, max_tokens=1024):
            records = json.loads(prompt.split("\n\n")[-1])
            return json.dumps(
                {
                    record["id"]: {
                        "market_lv": False,
                        "relevant": True,
                        "score": 85,
                        "category": "ecommerce",
                        "reason_lv": "Ieraksts nav latviešu valodā.",
                        "reply_lv": "Nevajadzētu tikt nosūtītam.",
                    }
                    for record in records
                }
            )

    monkeypatch.setenv("HARKEN_LEAD_LLM_PROVIDER", "openai")
    monkeypatch.setattr(cli, "get_provider", lambda name: LatviaProvider())

    result = runner.invoke(
        cli.app,
        ["leads", "reclassify", "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert "reclassified 1 stored mention(s)" in result.output
    assert "0 qualified Latvian lead match(es)" in result.output

    with Store(db_path) as store:
        row = store.lead_analysis(query, foreign.id)
        assert row is not None
        assert row["relevant"] is False
        assert row["score"] == 49
        assert row["category"] == "other"
        assert row["suggested_reply"] == ""


def test_leads_report_ignores_legacy_non_ecommerce_profile_queries(tmp_path):
    db_path = tmp_path / "legacy-leads.db"
    with Store(db_path) as store:
        legacy = Mention(
            source="bluesky",
            query="meklēju mājaslapu",
            author="legacy.bsky.social",
            text="Meklēju mājaslapas izstrādātāju",
            url="https://bsky.app/profile/legacy/post/1",
            created_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
            lead_relevant=True,
            lead_score=99,
            lead_category="website",
            lead_reason="Vecā profila klasifikācija",
            suggested_reply="Vecs drafts",
        )
        store.upsert([legacy])
        store.save_lead_analysis([legacy])

    result = runner.invoke(cli.app, ["leads", "report", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "No unique leads found" in result.output
    assert "legacy.bsky.social" not in result.output


def test_alert_command_can_send_synthetic_lead_email(monkeypatch):
    monkeypatch.setenv("HARKEN_EMAIL_TO", "ops@example.test")
    monkeypatch.setenv("HARKEN_EMAIL_FROM", "harken@example.test")
    monkeypatch.setenv("HARKEN_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("HARKEN_SMTP_SECURITY", "none")
    delivered = []
    monkeypatch.setattr(
        cli,
        "send_lead_digest_email",
        lambda settings, mentions: delivered.append((settings, mentions)),
    )

    result = runner.invoke(
        cli.app,
        ["test-alert", "--transport", "email", "--kind", "lead"],
    )

    assert result.exit_code == 0, result.output
    settings, mentions = delivered[0]
    assert settings.recipients == ("ops@example.test",)
    assert mentions[0].query == "meklēju interneta veikalu"
    assert mentions[0].lead_score == 92
    assert mentions[0].lead_category == "ecommerce"
    assert "e-komercijas platformu" in mentions[0].text


def test_alert_command_can_send_synthetic_lead_with_resend(monkeypatch):
    monkeypatch.setenv("HARKEN_RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("HARKEN_RESEND_FROM", "noreply@primovezo.com")
    monkeypatch.setenv("HARKEN_RESEND_TO", "owner@example.test")
    delivered = []
    monkeypatch.setattr(
        cli,
        "send_lead_digest_resend",
        lambda settings, mentions: delivered.append((settings, mentions)),
    )

    result = runner.invoke(
        cli.app,
        ["test-alert", "--transport", "resend", "--kind", "lead"],
    )

    assert result.exit_code == 0, result.output
    settings, mentions = delivered[0]
    assert settings.sender == "noreply@primovezo.com"
    assert settings.recipients == ("owner@example.test",)
    assert mentions[0].lead_score == 92
    assert "resend test delivered" in result.output


def test_alert_command_can_send_synthetic_operational_warning_with_resend(monkeypatch):
    monkeypatch.setenv("HARKEN_RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("HARKEN_RESEND_FROM", "noreply@primovezo.com")
    monkeypatch.setenv("HARKEN_RESEND_TO", "owner@example.test")
    delivered = []
    monkeypatch.setattr(
        cli,
        "send_operational_resend",
        lambda settings, *, issues, run_label: delivered.append(
            (settings, list(issues), run_label)
        ),
    )

    result = runner.invoke(
        cli.app,
        ["test-alert", "--transport", "resend", "--kind", "operational"],
    )

    assert result.exit_code == 0, result.output
    settings, issues, run_label = delivered[0]
    assert settings.recipients == ("owner@example.test",)
    assert run_label == "synthetic alert test"
    assert any("Synthetic operational warning" in issue for issue in issues)
    assert "resend test delivered" in result.output


def test_version_flag():
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0


def test_evaluate_command_supports_json_and_accuracy_gate():
    result = runner.invoke(cli.app, ["evaluate", "--format", "json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["accuracy"] == 0.9667
    assert report["dataset"]["examples"] == 60

    passing = runner.invoke(cli.app, ["evaluate", "--min-accuracy", "0.95"])
    assert passing.exit_code == 0
    failing = runner.invoke(cli.app, ["evaluate", "--min-accuracy", "0.99"])
    assert failing.exit_code == 1
    assert "below required" in failing.output


def test_export_json_and_csv_include_complete_records(tmp_path):
    db = str(tmp_path / "demo.db")
    assert runner.invoke(cli.app, ["demo", "--no-serve", "--db", db]).exit_code == 0

    json_path = tmp_path / "mentions.json"
    result = runner.invoke(
        cli.app,
        ["export", "Quill", "--format", "json", "--output", str(json_path), "--db", db],
    )
    assert result.exit_code == 0
    records = json.loads(json_path.read_text())
    assert len(records) == 32
    assert set(records[0]) >= {"id", "source", "query", "sentiment", "theme"}

    csv_path = tmp_path / "mentions.csv"
    result = runner.invoke(
        cli.app,
        ["export", "--format", "csv", "--output", str(csv_path), "--db", db],
    )
    assert result.exit_code == 0
    with csv_path.open() as exported:
        assert len(list(csv.DictReader(exported))) == 32


def test_backup_command_preserves_data_and_requires_force(tmp_path):
    db = str(tmp_path / "demo.db")
    assert runner.invoke(cli.app, ["demo", "--no-serve", "--db", db]).exit_code == 0
    destination = tmp_path / "backup.db"
    first = runner.invoke(cli.app, ["backup", str(destination), "--db", db])
    assert first.exit_code == 0
    with Store(destination) as backup_store:
        assert backup_store.summary("Quill")["total"] == 32
    assert runner.invoke(cli.app, ["backup", str(destination), "--db", db]).exit_code != 0
    forced = runner.invoke(cli.app, ["backup", str(destination), "--force", "--db", db])
    assert forced.exit_code == 0


def test_prune_is_preview_only_without_explicit_yes(tmp_path):
    db = str(tmp_path / "demo.db")
    assert runner.invoke(cli.app, ["demo", "--no-serve", "--db", db]).exit_code == 0
    preview = runner.invoke(cli.app, ["prune", "--older-than", "1", "--db", db])
    assert preview.exit_code == 0
    assert "Would remove" in preview.output
    with Store(db) as store:
        assert store.summary("Quill")["total"] == 32

    applied = runner.invoke(cli.app, ["prune", "--older-than", "1", "--yes", "--db", db])
    assert applied.exit_code == 0
    with Store(db) as store:
        assert store.summary("Quill")["total"] < 32


@respx.mock
def test_alert_command_sends_synthetic_notification():
    route = respx.post("https://alerts.example.test/harken").mock(return_value=httpx.Response(204))
    result = runner.invoke(
        cli.app,
        ["test-alert", "--webhook-url", "https://alerts.example.test/harken"],
    )
    assert result.exit_code == 0
    assert route.called
    assert b"synthetic negative-mention alert" in route.calls[0].request.content


@respx.mock
def test_alert_command_can_send_synthetic_threshold_event():
    route = respx.post("https://alerts.example.test/harken").mock(return_value=httpx.Response(204))
    result = runner.invoke(
        cli.app,
        [
            "test-alert",
            "--kind",
            "volume",
            "--webhook-url",
            "https://alerts.example.test/harken",
        ],
    )
    assert result.exit_code == 0
    assert json.loads(route.calls[0].request.content)["event"] == "harken.volume_spike"


def test_alert_command_can_send_synthetic_email(monkeypatch):
    monkeypatch.setenv("HARKEN_EMAIL_TO", "ops@example.test")
    monkeypatch.setenv("HARKEN_EMAIL_FROM", "harken@example.test")
    monkeypatch.setenv("HARKEN_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("HARKEN_SMTP_SECURITY", "none")
    delivered = []
    monkeypatch.setattr(
        cli,
        "send_negative_email",
        lambda settings, query, mentions: delivered.append((settings, query, mentions)),
    )
    result = runner.invoke(cli.app, ["test-alert", "--transport", "email"])
    assert result.exit_code == 0, result.output
    settings, query, mentions = delivered[0]
    assert settings.host == "smtp.example.test"
    assert settings.recipients == ("ops@example.test",)
    assert query == "webhook test"
    assert "synthetic negative-mention alert" in mentions[0].text


def test_alert_command_requires_configured_email_transport():
    result = runner.invoke(cli.app, ["test-alert", "--transport", "email"])
    assert result.exit_code != 0
    assert "HARKEN_EMAIL_TO" in result.output


def test_track_rejects_blank_query():
    result = runner.invoke(cli.app, ["track", "   "])
    assert result.exit_code != 0
    assert "must not be empty" in result.output


def test_track_rejects_nonpositive_limit():
    result = runner.invoke(cli.app, ["track", "acme", "--limit", "0"])
    assert result.exit_code != 0


def test_report_rejects_unknown_query(tmp_path):
    db = str(tmp_path / "t.db")
    runner.invoke(cli.app, ["demo", "--no-serve", "--db", db])
    result = runner.invoke(cli.app, ["report", "missing", "--db", db])
    assert result.exit_code == 1
    assert "No data found" in result.output


def test_serve_warns_when_exposed_without_authentication(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_serve", lambda *args: None)
    result = runner.invoke(
        cli.app,
        ["serve", "--host", "0.0.0.0", "--db", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 0
    assert "no authentication" in result.output


def test_serve_reports_enabled_auth_and_https_requirement(tmp_path, monkeypatch):
    monkeypatch.setenv("HARKEN_AUTH_USERNAME", "admin")
    monkeypatch.setenv("HARKEN_AUTH_PASSWORD", "secret")
    monkeypatch.setattr(cli, "_serve", lambda *args: None)
    result = runner.invoke(
        cli.app,
        ["serve", "--host", "0.0.0.0", "--db", str(tmp_path / "t.db")],
    )
    assert result.exit_code == 0
    assert "authentication is enabled" in result.output
    assert "HTTPS" in result.output


@respx.mock
def test_watch_can_run_a_bounded_scan(tmp_path):
    route = respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(200, json={"hits": []})
    )
    result = runner.invoke(
        cli.app,
        [
            "watch",
            "acme",
            "--sources",
            "hackernews",
            "--runs",
            "1",
            "--db",
            str(tmp_path / "watch.db"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert route.call_count == 1
    assert "scan 1" in result.output


def test_watch_survives_a_failing_scan(tmp_path, monkeypatch):
    # A store error inside a scan must be logged and the watcher must keep
    # going / exit cleanly on the run cap, not crash the long-running process.
    class BoomPipe:
        def __init__(self, cfg):
            pass

        def track(self, query, pages=3):
            raise RuntimeError("database is locked")

        def close(self):
            pass

    monkeypatch.setattr(cli, "Pipeline", BoomPipe)
    result = runner.invoke(
        cli.app,
        ["watch", "acme", "--sources", "hackernews", "--runs", "1", "--db", str(tmp_path / "w.db")],
    )
    assert result.exit_code == 0, result.output
    assert "failed" in result.output


def test_export_to_a_directory_exits_cleanly(tmp_path):
    db = str(tmp_path / "e.db")
    assert runner.invoke(cli.app, ["demo", "--no-serve", "--db", db]).exit_code == 0
    result = runner.invoke(cli.app, ["export", "--db", db, "-o", str(tmp_path)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
