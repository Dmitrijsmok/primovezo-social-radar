"""LLM provider factory tests — no network, no keys."""

import httpx
import pytest

from harken.llm import NullProvider, get_provider


def test_default_is_null(monkeypatch):
    monkeypatch.delenv("HARKEN_LLM_PROVIDER", raising=False)
    p = get_provider()
    assert isinstance(p, NullProvider)
    assert p.available is False


def test_null_provider_raises_on_complete():
    p = get_provider("none")
    with pytest.raises(RuntimeError):
        p.complete("hello")


def test_unknown_provider_errors():
    with pytest.raises(ValueError):
        get_provider("definitely-not-a-provider")


def test_anthropic_provider_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = get_provider("anthropic")
    assert p.name == "anthropic"
    assert p.available is False  # no key -> not available, so pipeline skips it


def test_openai_provider_reads_model_env(monkeypatch):
    monkeypatch.setenv("HARKEN_LLM_MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    p = get_provider("openai")
    assert p.model == "gpt-test"
    assert p.available is True



def test_openai_provider_retries_transient_503(monkeypatch):
    calls = 0
    delays = []

    def post(*args, **kwargs):
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://llm.example.test/chat/completions")
        if calls < 3:
            return httpx.Response(503, request=request, json={"error": {"message": "busy"}})
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    monkeypatch.setattr("harken.llm.openai_provider.httpx.post", post)
    monkeypatch.setattr("harken.llm.openai_provider.time.sleep", delays.append)

    provider = get_provider(
        "openai",
        api_key="test-key",
        model="test-model",
        base_url="https://llm.example.test",
    )
    assert provider.complete("hello") == "OK"
    assert calls == 3
    assert delays == [1.0, 2.0]


def test_openai_provider_does_not_retry_non_transient_400(monkeypatch):
    calls = 0
    delays = []

    def post(*args, **kwargs):
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://llm.example.test/chat/completions")
        return httpx.Response(400, request=request, json={"error": {"message": "bad request"}})

    monkeypatch.setattr("harken.llm.openai_provider.httpx.post", post)
    monkeypatch.setattr("harken.llm.openai_provider.time.sleep", delays.append)

    provider = get_provider(
        "openai",
        api_key="test-key",
        model="test-model",
        base_url="https://llm.example.test",
    )
    with pytest.raises(httpx.HTTPStatusError):
        provider.complete("hello")

    assert calls == 1
    assert delays == []


def test_openai_provider_honors_retry_after(monkeypatch):
    calls = 0
    delays = []

    def post(*args, **kwargs):
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "https://llm.example.test/chat/completions")
        if calls == 1:
            return httpx.Response(
                429,
                request=request,
                headers={"Retry-After": "7"},
                json={"error": {"message": "rate limited"}},
            )
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "OK"}}]},
        )

    monkeypatch.setattr("harken.llm.openai_provider.httpx.post", post)
    monkeypatch.setattr("harken.llm.openai_provider.time.sleep", delays.append)

    provider = get_provider(
        "openai",
        api_key="test-key",
        model="test-model",
        base_url="https://llm.example.test",
    )
    assert provider.complete("hello") == "OK"
    assert calls == 2
    assert delays == [7.0]
