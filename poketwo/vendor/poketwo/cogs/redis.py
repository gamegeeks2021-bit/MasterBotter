from __future__ import annotations

import os
import ssl
from typing import Any, AsyncIterator, Optional

import redis.asyncio as redis
from discord.ext import commands


class RedisCompat:
    """Compatibility wrapper for upstream Poketwo code.

    Upstream cogs use patterns like:
      - with await bot.redis as r:
      - async for k, v in bot.redis.ihscan("trade")

    redis-py asyncio clients don't implement those helpers directly.
    This wrapper keeps upstream code working while still exposing the full
    redis-py API via attribute proxying.
    """

    def __init__(self, client: redis.Redis):
        self._client = client

    async def _as_ctx(self) -> "RedisCompat":
        return self

    def __await__(self):
        return self._as_ctx().__await__()

    def __enter__(self) -> redis.Redis:
        return self._client

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    async def ihscan(
        self, name: str, match: Optional[str] = None, count: Optional[int] = None
    ) -> AsyncIterator[tuple[Any, Any]]:
        async for item in self._client.hscan_iter(name, match=match, count=count):
            # redis-py yields (field, value)
            if isinstance(item, (tuple, list)) and len(item) == 2:
                yield item[0], item[1]

    def __getattr__(self, name: str):
        return getattr(self._client, name)


class Redis(commands.Cog):
    """For redis."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pool: redis.Redis | None = None
        self._connect_task = self.bot.loop.create_task(self.connect())

    async def connect(self):
        # Poketwo upstream used aioredis 1.x pools. For Python 3.11, we use redis-py asyncio.
        # Prefer REDIS_URL, otherwise fall back to REDIS_CONF fields.
        url = getattr(self.bot.config, "REDIS_URL", None) or os.getenv("REDIS_URL")
        if url:
            if not (url.startswith("redis://") or url.startswith("rediss://") or url.startswith("unix://")):
                raise ValueError(
                    "Redis URL must start with redis:// or rediss:// (example: rediss://user:pass@host:port)"
                )
            # NOTE:
            # redis-py asyncio does NOT accept ssl_context in the connection kwargs.
            # Use rediss:// to enable TLS and relax cert checks only if needed.
            kwargs = {"decode_responses": False}
            if url.startswith("rediss://"):
                # Many managed Redis providers require TLS and use certs that may not validate
                # in minimal containers. These options mirror "--insecure" behavior.
                kwargs.update({
                    "ssl_cert_reqs": None,
                    "ssl_check_hostname": False,
                })
            self.pool = redis.from_url(url, **kwargs)
            # Expose as bot.redis for other cogs (compat wrapper)
            self.bot.redis = RedisCompat(self.pool)
            return

        conf = getattr(self.bot.config, "REDIS_CONF", {}) or {}
        addr = conf.get("address") or (conf.get("host", "localhost"), conf.get("port", 6379))
        host, port = addr[0], addr[1]
        db = conf.get("db", 0)
        password = conf.get("password")
        self.pool = redis.Redis(host=host, port=int(port), db=int(db), password=password, decode_responses=False)
        # Expose as bot.redis for other cogs (compat wrapper)
        self.bot.redis = RedisCompat(self.pool)

    async def close(self):
        if self.pool is None:
            return
        await self.pool.close()
        if getattr(self.bot, "redis", None) is not None:
            self.bot.redis = None

    async def wait_until_ready(self):
        await self._connect_task

    def cog_unload(self):
        self.bot.loop.create_task(self.close())


async def setup(bot: commands.Bot):
    await bot.add_cog(Redis(bot))
