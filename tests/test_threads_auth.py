"""Threads token maintenance tests."""

import os
import stat
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from harken.threads_auth import (
    ThreadsAuthError,
    inspect_threads_token,
    maintain_threads_token,
)

DEBUG_URL = "https://graph.threads.net/debug_token"
REFRESH_URL = "https://graph.threads.net/refresh_access_token"


def _debug_payload(expires_at, *, scopes=None, valid=True):
    return {
        "data": {
            "is_valid": valid,
            "scopes": scopes or ["threads_basic", "threads_keyword_search"],
            "expires_at": int(expires_at.timestamp()),
        }
    }


@respx.mock
def test_maintain_threads_token_skips_refresh_when_expiry_is_far_away(tmp_path):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    debug = respx.get(DEBUG_URL).mock(
        return_value=httpx.Response(
            200,
            json=_debug_payload(now + timedelta(days=60)),
        )
    )

    result = maintain_threads_token(
        "long-token",
        env_path=tmp_path / ".env",
        now=now,
    )

    assert result.token == "long-token"
    assert result.refreshed is False
    assert result.refresh_error is None
    assert debug.call_count == 1
    assert not (tmp_path / ".env").exists()


@respx.mock
def test_maintain_threads_token_refreshes_near_expiry_and_persists_atomically(
    tmp_path, monkeypatch
):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    debug = respx.get(DEBUG_URL).mock(
        side_effect=[
            httpx.Response(200, json=_debug_payload(now + timedelta(days=7))),
            httpx.Response(200, json=_debug_payload(now + timedelta(days=60))),
        ]
    )
    refresh = respx.get(REFRESH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed-token",
                "token_type": "bearer",
                "expires_in": 5_184_000,
            },
        )
    )
    env_path = tmp_path / ".env"
    env_path.write_text("KEEP=value\nHARKEN_THREADS_ACCESS_TOKEN=old-token\n")
    env_path.chmod(0o600)
    monkeypatch.delenv("HARKEN_THREADS_ACCESS_TOKEN", raising=False)

    result = maintain_threads_token(
        "old-token",
        env_path=env_path,
        now=now,
    )

    assert result.refreshed is True
    assert result.token == "refreshed-token"
    assert result.info.expires_at is not None
    assert debug.call_count == 2
    assert refresh.call_count == 1
    params = refresh.calls[0].request.url.params
    assert params["grant_type"] == "th_refresh_token"
    assert params["access_token"] == "old-token"
    assert env_path.read_text() == (
        "KEEP=value\nHARKEN_THREADS_ACCESS_TOKEN=refreshed-token\n"
    )
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert os.environ["HARKEN_THREADS_ACCESS_TOKEN"] == "refreshed-token"


@respx.mock
def test_refresh_failure_keeps_current_valid_token_for_next_daily_retry(tmp_path):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    respx.get(DEBUG_URL).mock(
        return_value=httpx.Response(
            200,
            json=_debug_payload(now + timedelta(days=7)),
        )
    )
    respx.get(REFRESH_URL).mock(return_value=httpx.Response(500, json={"error": {}}))

    result = maintain_threads_token(
        "still-valid-token",
        env_path=tmp_path / ".env",
        now=now,
    )

    assert result.token == "still-valid-token"
    assert result.refreshed is False
    assert result.refresh_error == "Threads token refresh failed with HTTP 500"
    assert not (tmp_path / ".env").exists()


@respx.mock
def test_token_inspection_error_never_exposes_token():
    secret = "very-secret-threads-token"
    respx.get(DEBUG_URL).mock(return_value=httpx.Response(401, json={"error": {}}))

    with pytest.raises(ThreadsAuthError) as exc:
        inspect_threads_token(secret)

    assert secret not in str(exc.value)
    assert "HTTP 401" in str(exc.value)


@respx.mock
def test_missing_keyword_scope_is_rejected(tmp_path):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    respx.get(DEBUG_URL).mock(
        return_value=httpx.Response(
            200,
            json=_debug_payload(
                now + timedelta(days=60),
                scopes=["threads_basic"],
            ),
        )
    )

    with pytest.raises(ThreadsAuthError, match="threads_keyword_search"):
        maintain_threads_token(
            "basic-only-token",
            env_path=tmp_path / ".env",
            now=now,
        )
