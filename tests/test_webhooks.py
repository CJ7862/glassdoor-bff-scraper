"""Webhook signing and delivery tests (httpx transport mocked)."""

from __future__ import annotations

import json

import httpx
import pytest

from api.security import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    hash_api_key,
    sign_webhook,
    verify_webhook,
)
from api.webhooks import deliver_webhook


def test_hash_api_key_is_deterministic():
    assert hash_api_key("secret") == hash_api_key("secret")
    assert hash_api_key("a") != hash_api_key("b")
    assert len(hash_api_key("x")) == 64


def test_sign_and_verify_roundtrip():
    sig = sign_webhook("shhh", "12345", b'{"ok": true}')
    assert verify_webhook("shhh", "12345", b'{"ok": true}', sig)
    assert not verify_webhook("shhh", "12345", b'{"ok": false}', sig)
    assert not verify_webhook("wrong", "12345", b'{"ok": true}', sig)


@pytest.mark.asyncio
async def test_deliver_webhook_signs_and_succeeds():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ok = await deliver_webhook(
            "https://consumer.test/hook",
            {"job_id": "j1", "results": []},
            secret="topsecret",
            client=client,
        )
    assert ok is True
    assert SIGNATURE_HEADER.lower() in captured["headers"]
    timestamp = captured["headers"][TIMESTAMP_HEADER.lower()]
    signature = captured["headers"][SIGNATURE_HEADER.lower()]
    assert verify_webhook("topsecret", timestamp, captured["body"], signature)


@pytest.mark.asyncio
async def test_deliver_webhook_retries_then_gives_up():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ok = await deliver_webhook(
            "https://consumer.test/hook",
            {"x": 1},
            secret="",
            max_attempts=3,
            client=client,
        )
    assert ok is False
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_deliver_webhook_body_is_valid_json():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await deliver_webhook(
            "https://consumer.test/hook",
            {"job_id": "j1", "results": [{"job_id": "abc"}]},
            secret="s",
            client=client,
        )
    payload = json.loads(captured["body"])
    assert payload["job_id"] == "j1"
    assert payload["results"][0]["job_id"] == "abc"
