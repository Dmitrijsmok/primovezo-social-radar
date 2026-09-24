"""Threads access-token inspection, refresh, and local persistence."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

_DEBUG_URL = "https://graph.threads.net/debug_token"
_REFRESH_URL = "https://graph.threads.net/refresh_access_token"
_KEYWORD_SCOPE = "threads_keyword_search"


class ThreadsAuthError(RuntimeError):
    """A sanitized Threads token-management failure."""


@dataclass(frozen=True)
class ThreadsTokenInfo:
    valid: bool
    scopes: tuple[str, ...]
    expires_at: datetime | None

    def remaining(self, now: datetime | None = None) -> timedelta | None:
        if self.expires_at is None:
            return None
        current = now or datetime.now(timezone.utc)
        return self.expires_at - current


@dataclass(frozen=True)
class ThreadsTokenMaintenance:
    token: str
    info: ThreadsTokenInfo
    refreshed: bool = False
    refresh_error: str | None = None


def inspect_threads_token(
    token: str,
    *,
    timeout: float = 15.0,
) -> ThreadsTokenInfo:
    """Validate a Threads user token without exposing it in errors."""
    if not token:
        raise ThreadsAuthError("Threads access token is missing")

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                _DEBUG_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={"input_token": token},
            )
    except httpx.HTTPError as exc:
        raise ThreadsAuthError(f"Threads token inspection failed: {type(exc).__name__}") from None

    if response.status_code != 200:
        raise ThreadsAuthError(f"Threads token inspection failed with HTTP {response.status_code}")

    try:
        payload = response.json()
        data = payload["data"]
    except (ValueError, KeyError, TypeError):
        raise ThreadsAuthError("Threads token inspection returned invalid JSON") from None

    scopes = tuple(sorted(str(scope) for scope in data.get("scopes", [])))
    expires_at_raw = data.get("expires_at")
    expires_at = None
    if isinstance(expires_at_raw, (int, float)) and expires_at_raw > 0:
        expires_at = datetime.fromtimestamp(expires_at_raw, tz=timezone.utc)

    return ThreadsTokenInfo(
        valid=bool(data.get("is_valid")),
        scopes=scopes,
        expires_at=expires_at,
    )


def refresh_threads_token(token: str, *, timeout: float = 20.0) -> str:
    """Refresh a valid long-lived Threads token and return the replacement."""
    if not token:
        raise ThreadsAuthError("Threads access token is missing")

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                _REFRESH_URL,
                params={
                    "grant_type": "th_refresh_token",
                    "access_token": token,
                },
            )
    except httpx.HTTPError as exc:
        raise ThreadsAuthError(f"Threads token refresh failed: {type(exc).__name__}") from None

    if response.status_code != 200:
        raise ThreadsAuthError(f"Threads token refresh failed with HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError:
        raise ThreadsAuthError("Threads token refresh returned invalid JSON") from None

    refreshed = payload.get("access_token")
    if not isinstance(refreshed, str) or not refreshed.strip():
        raise ThreadsAuthError("Threads token refresh returned no access token")
    return refreshed.strip()


def maintain_threads_token(
    token: str,
    *,
    env_path: str | Path = ".env",
    refresh_before_days: int = 14,
    now: datetime | None = None,
) -> ThreadsTokenMaintenance:
    """Inspect and, near expiry, automatically refresh a Threads token.

    A refresh failure does not discard a still-valid token. The caller can keep
    scanning and retry maintenance on the next scheduled run.
    """
    info = inspect_threads_token(token)
    if not info.valid:
        raise ThreadsAuthError("Threads access token is invalid")
    if _KEYWORD_SCOPE not in info.scopes:
        raise ThreadsAuthError("Threads access token is missing threads_keyword_search permission")

    remaining = info.remaining(now)
    if remaining is None or remaining > timedelta(days=refresh_before_days):
        return ThreadsTokenMaintenance(token=token, info=info)

    if remaining <= timedelta(0):
        raise ThreadsAuthError("Threads access token has expired")

    try:
        refreshed = refresh_threads_token(token)
        refreshed_info = inspect_threads_token(refreshed)
        if not refreshed_info.valid or _KEYWORD_SCOPE not in refreshed_info.scopes:
            raise ThreadsAuthError(
                "refreshed Threads token failed validation or lost keyword-search permission"
            )
        persist_threads_token(refreshed, env_path=env_path)
    except (ThreadsAuthError, OSError) as exc:
        return ThreadsTokenMaintenance(
            token=token,
            info=info,
            refresh_error=str(exc),
        )

    return ThreadsTokenMaintenance(
        token=refreshed,
        info=refreshed_info,
        refreshed=True,
    )


def persist_threads_token(token: str, *, env_path: str | Path = ".env") -> None:
    """Atomically update only HARKEN_THREADS_ACCESS_TOKEN in a dotenv file."""
    path = Path(env_path)
    key = "HARKEN_THREADS_ACCESS_TOKEN="
    existing = path.read_text() if path.exists() else ""
    lines = existing.splitlines()

    for index, line in enumerate(lines):
        if line.startswith(key):
            lines[index] = key + token
            break
    else:
        lines.append(key + token)

    payload = "\n".join(lines) + "\n"
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)

    # Config reads dotenv into os.environ at import time. Keep this process in
    # sync so a scan immediately uses the refreshed value.
    os.environ["HARKEN_THREADS_ACCESS_TOKEN"] = token
