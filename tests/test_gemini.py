"""GeminiClient retry/validation logic with the network call mocked (no quota used)."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from google.genai import errors

from app.ai.gemini import AIInvalidResponse, AIUnavailable, GeminiClient
from app.ai.schemas import TransactionClassification

GOOD = {"transaction_type": "expense", "category": "Transport", "subcategory": "Taxi", "is_spending": True,
        "confidence": 0.9, "amount": "16213.00"}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    real_sleep = asyncio.sleep

    async def fast(_):
        await real_sleep(0)

    monkeypatch.setattr("app.ai.gemini.asyncio.sleep", fast)


def make_client(results):
    c = GeminiClient("fake-key", "gemini-test", min_interval=0, max_retries=3, timeout=5)
    calls = {"n": 0}

    async def generate_content(**kwargs):
        calls["n"] += 1
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(text=r)

    c._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    return c, calls


async def test_retries_transient_errors_then_succeeds():
    c, calls = make_client([errors.ServerError(503, {"error": {"message": "overloaded"}}), json.dumps(GOOD)])
    result, raw = await c.generate_json("p", TransactionClassification, system="s")
    assert result.category == "Transport" and calls["n"] == 2


async def test_rate_limit_is_retried():
    c, calls = make_client([errors.ClientError(429, {"error": {"message": "quota"}}), json.dumps(GOOD)])
    result, _ = await c.generate_json("p", TransactionClassification, system="s")
    assert calls["n"] == 2


async def test_malformed_json_retried_then_reported():
    c, calls = make_client(["{oops", "not json either", "```json\n{\"transaction_type\": \"nope\"}\n```"])
    with pytest.raises(AIInvalidResponse):
        await c.generate_json("p", TransactionClassification, system="s")
    assert calls["n"] == 3  # bounded


async def test_malformed_then_valid():
    c, _ = make_client(["{oops", "```json\n" + json.dumps(GOOD) + "\n```"])
    result, _ = await c.generate_json("p", TransactionClassification, system="s")
    assert str(result.amount) == "16213.00"


async def test_permanent_error_is_not_retried():
    c, calls = make_client([errors.ClientError(400, {"error": {"message": "bad request"}})])
    with pytest.raises(AIUnavailable):
        await c.generate_json("p", TransactionClassification, system="s")
    assert calls["n"] == 1


async def test_all_attempts_fail():
    c, calls = make_client([errors.ServerError(500, {}), errors.ServerError(502, {}), errors.ServerError(503, {})])
    with pytest.raises(AIUnavailable):
        await c.generate_json("p", TransactionClassification, system="s")
    assert calls["n"] == 3


def test_schema_rejects_out_of_range_and_unknown_type():
    from app.ai.gemini import parse_json_response

    with pytest.raises(AIInvalidResponse):
        parse_json_response(json.dumps({**GOOD, "confidence": 1.5}), TransactionClassification)
    with pytest.raises(AIInvalidResponse):
        parse_json_response(json.dumps({**GOOD, "transaction_type": "DROP TABLE"}), TransactionClassification)
    with pytest.raises(AIInvalidResponse):
        parse_json_response("[1, 2]", TransactionClassification)


async def test_daily_quota_switches_to_next_model_immediately():
    daily = errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20. Generate content requests per day limit exceeded. Please retry in 19h28m57s."}})
    c = GeminiClient("k", "model-a", fallback_models=["model-b"], min_interval=0, max_retries=3, timeout=5)
    used = []

    async def generate_content(**kw):
        used.append(kw["model"])
        if kw["model"] == "model-a":
            raise daily
        return SimpleNamespace(text=json.dumps(GOOD))

    c._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    await c.generate_json("p", TransactionClassification, system="s")
    assert used == ["model-a", "model-b"]  # no pointless retries on an exhausted model
    used.clear()
    await c.generate_json("p", TransactionClassification, system="s")
    assert used == ["model-b"]  # model-a is skipped until its reset


async def test_all_models_exhausted_raises_quota_error():
    from app.ai.gemini import AIQuotaExhausted

    daily = errors.ClientError(429, {"error": {"message": "requests per day limit exceeded. Please retry in 2h0m0s."}})
    c = GeminiClient("k", "a", fallback_models=["b"], min_interval=0, max_retries=2, timeout=5)

    async def generate_content(**kw):
        raise daily

    c._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    with pytest.raises(AIQuotaExhausted) as e:
        await c.generate_json("p", TransactionClassification, system="s")
    import time
    assert 3600 < e.value.retry_at - time.time() <= 7200
    with pytest.raises(AIQuotaExhausted):  # now fails fast without any request
        await c.generate_json("p", TransactionClassification, system="s")


async def test_background_and_interactive_use_separate_chains():
    c = GeminiClient("k", "best", fallback_models=["lite"], background_models=["lite"], min_interval=0)
    assert c.available_models(False) == ["best", "lite"]
    assert c.available_models(True) == ["lite"]


async def test_hanging_model_is_skipped_quickly():
    c = GeminiClient("k", "slow", fallback_models=["fast"], min_interval=0, max_retries=3, timeout=0.05)
    used = []

    async def generate_content(**kw):
        used.append(kw["model"])
        if kw["model"] == "slow":
            await asyncio.get_running_loop().create_future()  # never answers
        return SimpleNamespace(text=json.dumps(GOOD))

    c._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    await c.generate_json("p", TransactionClassification, system="s")
    assert used == ["slow", "fast"]
    await c.generate_json("p", TransactionClassification, system="s")
    assert used == ["slow", "fast", "fast"]


async def test_background_budget_protects_owner_quota():
    from app.ai.gemini import AIQuotaExhausted

    c = GeminiClient("k", "m", min_interval=0, background_daily_budget=2)

    async def generate_content(**kw):
        return SimpleNamespace(text=json.dumps(GOOD))

    c._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    await c.generate_json("p", TransactionClassification, system="s", background=True)
    await c.generate_json("p", TransactionClassification, system="s", background=True)
    with pytest.raises(AIQuotaExhausted):
        await c.generate_json("p", TransactionClassification, system="s", background=True)
    await c.generate_json("p", TransactionClassification, system="s")  # owner's request still works
