from __future__ import annotations

from typing import Final

from redis.asyncio import Redis

from erp_core.shared.enums import ChannelType

_META_RATE_LIMIT_CODES: Final[frozenset[int]] = frozenset({131056, 130429})
_META_PER_USER_MARKETING_CAP_CODE: Final[int] = 131049

_DUAL_TOKEN_BUCKET_LUA: Final[str] = """
local acct_key = KEYS[1]
local pair_key = KEYS[2]

local acct_cap = tonumber(ARGV[1])
local acct_rate = tonumber(ARGV[2])
local has_pair = tonumber(ARGV[3])
local pair_cap = tonumber(ARGV[4])
local pair_rate = tonumber(ARGV[5])
local ttl_ms = tonumber(ARGV[6])
local now_override = tonumber(ARGV[7])

local now_ms
if now_override ~= nil and now_override > 0 then
    now_ms = now_override
else
    local now_t = redis.call('TIME')
    now_ms = tonumber(now_t[1]) * 1000 + math.floor(tonumber(now_t[2]) / 1000)
end

local function eval_bucket(key, cap, rate)
    local data = redis.call('HMGET', key, 'tokens', 'last_ms')
    local tokens = tonumber(data[1])
    local last_ms = tonumber(data[2])
    if tokens == nil or last_ms == nil then
        tokens = cap
        last_ms = now_ms
    else
        local elapsed_sec = math.max(0.0, (now_ms - last_ms) / 1000.0)
        tokens = math.min(cap, tokens + elapsed_sec * rate)
        if (cap - tokens) < 0.000001 then
            tokens = cap
        end
        last_ms = now_ms
    end
    local allowed = tokens >= 1.0
    local wait_ms = 0
    if not allowed then
        wait_ms = math.ceil(((1.0 - tokens) / rate) * 1000.0)
    end
    return allowed, tokens, last_ms, wait_ms
end

local ok1, tok1, ts1, wait1 = eval_bucket(acct_key, acct_cap, acct_rate)
local ok2, tok2, ts2, wait2 = true, 0.0, now_ms, 0
if has_pair == 1 then
    ok2, tok2, ts2, wait2 = eval_bucket(pair_key, pair_cap, pair_rate)
end

if ok1 and ok2 then
    tok1 = tok1 - 1.0
    redis.call('HSET', acct_key, 'tokens', string.format('%.6f', tok1), 'last_ms', tostring(ts1))
    redis.call('PEXPIRE', acct_key, ttl_ms)
    if has_pair == 1 then
        tok2 = tok2 - 1.0
        redis.call(
            'HSET', pair_key, 'tokens', string.format('%.6f', tok2),
            'last_ms', tostring(ts2)
        )
        redis.call('PEXPIRE', pair_key, ttl_ms)
    end
    return {1, 0}
end

local wait_ms = math.max(wait1, wait2)
return {0, wait_ms}
"""


class DualTokenBucketLimiter:
    """Atomic Redis Lua dual token bucket enforcing account and WhatsApp pair rate limits."""

    def __init__(
        self,
        redis: Redis,
        *,
        pair_burst_capacity: int = 1,
        pair_window_seconds: float = 6.0,
        cloud_mps: int = 80,
        coexistence_mps: int = 20,
        default_mps: int = 50,
        key_ttl_ms: int = 120_000,
    ) -> None:
        self._redis = redis
        self._pair_burst_capacity = max(1, pair_burst_capacity)
        self._pair_refill_rate = 1.0 / max(0.1, pair_window_seconds)
        self._cloud_mps = max(1, cloud_mps)
        self._coexistence_mps = max(1, coexistence_mps)
        self._default_mps = max(1, default_mps)
        self._key_ttl_ms = max(10_000, key_ttl_ms)
        self._script = self._redis.register_script(_DUAL_TOKEN_BUCKET_LUA)

    def _resolve_account_rate(
        self, channel_type: ChannelType, is_coexistence: bool
    ) -> tuple[int, float]:
        if channel_type == ChannelType.WHATSAPP_COEXISTENCE or is_coexistence:
            return self._coexistence_mps, float(self._coexistence_mps)
        if channel_type == ChannelType.WHATSAPP_CLOUD:
            return self._cloud_mps, float(self._cloud_mps)
        return self._default_mps, float(self._default_mps)

    async def acquire(
        self,
        channel_type: ChannelType,
        account_id: str,
        recipient_id: str | None = None,
        is_coexistence: bool = False,
        now_ms: int | None = None,
    ) -> tuple[bool, int]:
        """Atomically acquire account and (for WhatsApp) recipient pair tokens."""
        acct_cap, acct_rate = self._resolve_account_rate(channel_type, is_coexistence)
        acct_key = f"bucket:acct:{channel_type.value}:{account_id}"

        is_whatsapp = channel_type in (
            ChannelType.WHATSAPP_CLOUD,
            ChannelType.WHATSAPP_COEXISTENCE,
        )
        has_pair = 1 if (is_whatsapp and recipient_id) else 0
        pair_key = (
            f"bucket:pair:{channel_type.value}:{account_id}:{recipient_id}"
            if has_pair == 1
            else f"{acct_key}:nopair"
        )

        result = await self._script(
            keys=[acct_key, pair_key],
            args=[
                str(acct_cap),
                f"{acct_rate:.6f}",
                str(has_pair),
                str(self._pair_burst_capacity),
                f"{self._pair_refill_rate:.6f}",
                str(self._key_ttl_ms),
                "" if now_ms is None else str(int(now_ms)),
            ],
        )
        allowed = int(result[0]) == 1
        wait_ms = int(result[1])
        return allowed, wait_ms

    def compute_backoff_seconds(self, attempt: int, error_code: int | None = None) -> int:
        """Compute retry delay seconds using Meta 4^X backoff on 131056/130429."""
        bounded_attempt = max(1, attempt)
        if error_code == _META_PER_USER_MARKETING_CAP_CODE:
            return 86_400
        if error_code in _META_RATE_LIMIT_CODES:
            return int(min(4**bounded_attempt, 4096))
        return int(min(2**bounded_attempt, 256))
