"""Token-bucket rate limiter tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from glassdoor_scraper.ratelimit import TokenBucket


def test_burst_is_immediate():
    bucket = TokenBucket(rate=1.0, capacity=3.0)
    start = time.monotonic()
    for _ in range(3):
        bucket.acquire()
    # The initial burst of `capacity` tokens should not block.
    assert time.monotonic() - start < 0.05


def test_sustained_rate_is_throttled():
    bucket = TokenBucket(rate=50.0, capacity=1.0)
    bucket.acquire()  # consume the single burst token
    start = time.monotonic()
    bucket.acquire()  # must wait ~1/50s for a refill
    elapsed = time.monotonic() - start
    assert elapsed >= 0.015


def test_invalid_parameters():
    with pytest.raises(ValueError):
        TokenBucket(rate=0, capacity=1)
    with pytest.raises(ValueError):
        TokenBucket(rate=1, capacity=0)


@pytest.mark.asyncio
async def test_async_acquire_shares_state():
    bucket = TokenBucket(rate=100.0, capacity=1.0)
    await bucket.acquire_async()
    start = time.monotonic()
    await bucket.acquire_async()
    assert time.monotonic() - start >= 0.005


@pytest.mark.asyncio
async def test_async_acquire_does_not_block_loop():
    bucket = TokenBucket(rate=20.0, capacity=1.0)
    bucket.acquire()

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(5):
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    await bucket.acquire_async()  # yields to the loop while waiting
    await task
    assert ticks == 5
