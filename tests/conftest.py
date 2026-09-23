"""Pytest isolation from developer-local Harken configuration."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_harken_environment(monkeypatch):
    """Keep a developer's .env from changing test semantics.

    harken.config loads the project-local .env at import time. Config defaults
    read os.environ when instances are created, so an enabled local lead mode,
    SMTP target, source list, or API key must not leak into otherwise isolated
    tests. Individual tests can still opt in with monkeypatch.setenv().
    """
    secret_names = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    for name in list(os.environ):
        if name.startswith("HARKEN_") or name in secret_names:
            monkeypatch.delenv(name, raising=False)
