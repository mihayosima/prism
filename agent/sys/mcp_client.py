"""agent.sys.mcp_client —— 通用 MCP stdio 客户端（子进程 JSON-RPC，仅 stdlib）。

把一个 MCP 服务器（stdio 子进程）封装成可用的工具目录 + 调用能力：
    start() → initialize + notifications/initialized → list_tools() → call_tool(name, args)
进程断连/超时 → 诚实抛错（不静默）。零第三方依赖、零业务词。
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

_MCP_VERSION = "2024-11-05"


class McpStdioClient:
    """单个 MCP 服务器子进程的 stdio 客户端。"""

    def __init__(self, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, timeout_s: float = 120.0):
        self._cmd = command
        self._args = list(args or [])
        self._env = env or {}
        self._timeout = timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._rid = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def start(self) -> None:
        full_env = {**os.environ, **self._env}
        self._proc = await asyncio.create_subprocess_exec(
            self._cmd, *self._args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=full_env)
        self._reader = asyncio.create_task(self._read_loop())
        await self._request("initialize", {
            "protocolVersion": _MCP_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agent", "version": "0.1.0"},
        })
        self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict]:
        result = await self._request("tools/list", {})
        return list(result.get("tools") or [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        content = list(result.get("content") or [])
        text = "\n".join(str(c.get("text") or "") for c in content if c.get("type") == "text")
        if result.get("isError"):
            raise RuntimeError(text or f"工具 {name!r} 返回错误")
        return {"content": content, "text": text,
                "structured": result.get("structuredContent")}

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self._proc = None

    # ---------- 内部 ----------

    async def _read_loop(self) -> None:
        assert self._proc is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:  # noqa: BLE001 —— 非 JSON 行忽略
                continue
            mid = msg.get("id")
            if mid is not None and mid in self._pending:
                fut = self._pending.pop(mid)
                if "error" in msg:
                    fut.set_exception(RuntimeError(json.dumps(msg["error"], ensure_ascii=False)))
                else:
                    fut.set_result(msg.get("result"))

    async def _request(self, method: str, params: dict) -> Any:
        assert self._proc is not None
        self._rid += 1
        rid = self._rid
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        self._proc.stdin.write((payload + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout=self._timeout)

    def _notify(self, method: str, params: dict) -> None:
        if self._proc is not None and self._proc.stdin is not None:
            payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
            self._proc.stdin.write((payload + "\n").encode("utf-8"))


__all__ = ["McpStdioClient"]