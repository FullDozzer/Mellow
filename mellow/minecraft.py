from __future__ import annotations

import aiohttp


class MinecraftUnavailable(Exception):
    pass


class MinecraftClient:
    def __init__(self, base_url: str | None, token: str | None, timeout: float = 8):
        self.base_url = base_url.rstrip("/") if base_url else None
        self.token = token
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def add_to_whitelist(self, username: str, operation_key: str) -> str:
        if not self.base_url or not self.token:
            raise MinecraftUnavailable("Minecraft API is not configured")
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as client:
                async with client.post(
                    f"{self.base_url}/v1/whitelist/add",
                    json={"username": username},
                    headers={"Authorization": f"Bearer {self.token}", "Idempotency-Key": operation_key},
                ) as response:
                    if response.status != 200:
                        raise MinecraftUnavailable(f"Minecraft API returned HTTP {response.status}")
                    data = await response.json(content_type=None)
                    if not isinstance(data, dict):
                        raise MinecraftUnavailable("Minecraft API returned an invalid response")
                    status = data.get("status")
                    if status not in {"SUCCESS", "ALREADY_WHITELISTED"}:
                        raise MinecraftUnavailable("Minecraft server did not confirm whitelist operation")
                    return status
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise MinecraftUnavailable("Minecraft API is unavailable") from exc
