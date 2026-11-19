from __future__ import annotations

import os
import uuid

import pytest
import redis.asyncio as aioredis

from erp_core.channels.rate_limiter import DualTokenBucketLimiter
from erp_core.shared.enums import ChannelType


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_whatsapp_pair_limit_throttling_and_isolation(redis_client: aioredis.Redis) -> None:
    limiter = DualTokenBucketLimiter(
        redis_client,
        pair_burst_capacity=3,
        pair_window_seconds=6.0,
    )
    acct_id = f"pn-{uuid.uuid4().hex[:8]}"
    buyer_a = "+14155550101"
    buyer_b = "+14155550102"

    # First 3 burst messages to buyer_a pass immediately
    for _ in range(3):
        allowed, wait_ms = await limiter.acquire(
            ChannelType.WHATSAPP_CLOUD, acct_id, recipient_id=buyer_a
        )
        assert allowed is True
        assert wait_ms == 0

    # 4th immediate message to buyer_a is throttled by pair bucket (~6s / token)
    allowed_4, wait_ms_4 = await limiter.acquire(
        ChannelType.WHATSAPP_CLOUD, acct_id, recipient_id=buyer_a
    )
    assert allowed_4 is False
    assert wait_ms_4 > 0

    # Independent recipient buyer_b is unaffected
    allowed_b, wait_ms_b = await limiter.acquire(
        ChannelType.WHATSAPP_CLOUD, acct_id, recipient_id=buyer_b
    )
    assert allowed_b is True
    assert wait_ms_b == 0


@pytest.mark.asyncio
async def test_coexistence_20mps_vs_cloud_80mps_and_atomic_no_leak(
    redis_client: aioredis.Redis,
) -> None:
    limiter = DualTokenBucketLimiter(redis_client, pair_burst_capacity=1)

    coex_acct = f"coex-{uuid.uuid4().hex[:8]}"
    # 20 distinct recipients consume all 20 mps tokens on coexistence account
    for idx in range(20):
        allowed, wait_ms = await limiter.acquire(
            ChannelType.WHATSAPP_COEXISTENCE,
            coex_acct,
            recipient_id=f"+49170000{idx:04d}",
            is_coexistence=True,
        )
        assert allowed is True
        assert wait_ms == 0

    # 21st distinct recipient in same second hits Coexistence 20 mps ceiling
    allowed_21, wait_ms_21 = await limiter.acquire(
        ChannelType.WHATSAPP_COEXISTENCE,
        coex_acct,
        recipient_id="+491700009999",
        is_coexistence=True,
    )
    assert allowed_21 is False
    assert wait_ms_21 > 0

    # Standard Cloud API account allows 80 mps before throttling on 81st
    cloud_acct = f"cloud-{uuid.uuid4().hex[:8]}"
    for idx in range(80):
        allowed, wait_ms = await limiter.acquire(
            ChannelType.WHATSAPP_CLOUD,
            cloud_acct,
            recipient_id=f"+44770090{idx:04d}",
            is_coexistence=False,
        )
        assert allowed is True
        assert wait_ms == 0

    allowed_81, wait_ms_81 = await limiter.acquire(
        ChannelType.WHATSAPP_CLOUD,
        cloud_acct,
        recipient_id="+447700909999",
        is_coexistence=False,
    )
    assert allowed_81 is False
    assert wait_ms_81 > 0

    # Atomicity: pair rejection must NOT consume an account token
    atomic_acct = f"atomic-{uuid.uuid4().hex[:8]}"
    ok1, _ = await limiter.acquire(
        ChannelType.WHATSAPP_COEXISTENCE,
        atomic_acct,
        recipient_id="+33612345678",
        is_coexistence=True,
    )
    assert ok1 is True
    # Rejected twice by pair limit
    for _ in range(2):
        ok_rej, wait_rej = await limiter.acquire(
            ChannelType.WHATSAPP_COEXISTENCE,
            atomic_acct,
            recipient_id="+33612345678",
            is_coexistence=True,
        )
        assert ok_rej is False
        assert wait_rej > 0
    acct_tokens_raw = await redis_client.hget(
        f"bucket:acct:{ChannelType.WHATSAPP_COEXISTENCE.value}:{atomic_acct}",
        "tokens",
    )
    assert float(acct_tokens_raw) >= 18.9


def test_meta_4_power_x_backoff_computation(redis_client: aioredis.Redis) -> None:
    limiter = DualTokenBucketLimiter(redis_client)

    # Meta 131056 (pair limit) and 130429 (throughput limit) strictly follow 4^X backoff
    assert limiter.compute_backoff_seconds(1, 131056) == 4
    assert limiter.compute_backoff_seconds(2, 131056) == 16
    assert limiter.compute_backoff_seconds(3, 130429) == 64
    assert limiter.compute_backoff_seconds(4, 130429) == 256
    assert limiter.compute_backoff_seconds(6, 131056) == 4096
    assert limiter.compute_backoff_seconds(8, 131056) == 4096

    # Meta 131049 per-user marketing frequency cap requires 24h cooldown
    assert limiter.compute_backoff_seconds(1, 131049) == 86400

    # Generic transient error uses 2^X backoff
    assert limiter.compute_backoff_seconds(1, 500) == 2
    assert limiter.compute_backoff_seconds(3, None) == 8


@pytest.mark.asyncio
async def test_default_peer_bucket_allows_one_message_per_six_seconds(
    redis_client: aioredis.Redis,
) -> None:
    import asyncio

    client = aioredis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        max_connections=50,
    )
    limiter = DualTokenBucketLimiter(client)
    account_id = f"peer-{uuid.uuid4().hex[:8]}"
    recipient = "+14155552671"
    start_ms = 1_763_500_000_000

    async def once() -> bool:
        allowed, _wait = await limiter.acquire(
            ChannelType.WHATSAPP_CLOUD,
            account_id,
            recipient_id=recipient,
            now_ms=start_ms,
        )
        return allowed

    try:
        assert limiter._pair_burst_capacity == 1
        results = await asyncio.gather(*(once() for _ in range(8)))
        assert sum(1 for ok in results if ok) == 1
        early, early_wait = await limiter.acquire(
            ChannelType.WHATSAPP_CLOUD,
            account_id,
            recipient_id=recipient,
            now_ms=start_ms + 5_999,
        )
        assert early is False
        assert early_wait > 0
        opened, opened_wait = await limiter.acquire(
            ChannelType.WHATSAPP_CLOUD,
            account_id,
            recipient_id=recipient,
            now_ms=start_ms + 6_000,
        )
        assert opened is True
        assert opened_wait == 0
        other, other_wait = await limiter.acquire(
            ChannelType.WHATSAPP_CLOUD,
            account_id,
            recipient_id="+14155550000",
            now_ms=start_ms,
        )
        assert other is True
        assert other_wait == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_account_bucket_is_atomic(redis_client: aioredis.Redis) -> None:
    import asyncio

    client = aioredis.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        max_connections=200,
    )
    limiter = DualTokenBucketLimiter(client, pair_burst_capacity=5)
    account_id = f"race-{uuid.uuid4().hex[:8]}"

    async def one(idx: int) -> bool:
        allowed, _wait = await limiter.acquire(
            ChannelType.WHATSAPP_CLOUD,
            account_id,
            recipient_id=f"+1555000{idx:04d}",
            is_coexistence=False,
        )
        return allowed

    try:
        results = await asyncio.gather(*(one(i) for i in range(120)))
        assert sum(1 for ok in results if ok) == 80
    finally:
        await client.aclose()
