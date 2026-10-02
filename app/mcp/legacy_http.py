"""POST-only compatibility for FieldKit and ImageLab; not Streamable HTTP."""
from typing import Any

import httpx


class LegacyHTTPClient:
    def __init__(self, url: str, timeout: float, headers: dict[str, str]):
        self.url, self.timeout, self.headers = url, timeout, headers
        self.client: httpx.AsyncClient | None = None
        self.request_id = 0

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("Legacy HTTP client is not connected")
        self.request_id += 1
        response = await self.client.post(self.url, json={
            "jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params,
        })
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(str(payload["error"].get("message", "MCP request failed")))
        return payload.get("result", payload)

    async def connect(self) -> dict[str, Any]:
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"Connection": "close", **self.headers},
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=10),
        )
        try:
            return await self.request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "clientInfo": {"name": "FruitcakeAI", "version": "5.0"},
            })
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
