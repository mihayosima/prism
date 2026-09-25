"""agent.sys.mcp_client —— 通用 MCP stdio 客户端（子进程 JSON-RPC，仅 stdlib）。

把一个 MCP 服务器（stdio 子进程）封装成可用的工具目录 + 调用能力：
    start() → initialize + notifications/initialized → list_tools() → call_tool(name, args)
进程断连/超时 → 诚实抛错（不静默）。零第三方依赖、零业务词。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

_MCP_VERSION = "2024-11-05"

logger = logging.getLogger("agent.mcp")


class McpStdioClient:
    """单个 MCP 服务器子进程的 stdio 客户端。

    S2/S3 加固：
    - stderr 由独立任务持续消费（默认降级为 debug 日志，可传 ``on_stderr`` 回调），
      话痨子进程不会再把 OS 管道缓冲区写满而整体卡死；
    - 子进程退出（stdout EOF）或 ``close()`` 时，在途 ``_pending`` 请求**立即**
      收到 ``ConnectionError``，而非悬挂到超时；
    - 请求方取消 / 超时后，future 从 ``_pending`` 移除（迟到的响应不再泄漏）。
    """

    def __init__(self, command: str, args: list[str] | None = None,
                 env: dict[str, str] | None = None, timeout_s: float = 120.0,
                 on_stderr=None):
        self._cmd = command
        self._args = list(args or [])
        self._env = env or {}
        self._timeout = timeout_s
        self._on_stderr = on_stderr
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._rid = 0
        self._pending: dict[int, asyncio.Future] = {}

    async def start(self) -> None:
        full_env = {**os.environ, **self._env}
        self._proc = await asyncio.create_subprocess_exec(
            self._cmd, *self._args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=full_env)
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
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
        for t in (self._reader, self._stderr_task):
            if t is not None:
                t.cancel()
        for t in (self._reader, self._stderr_task):
            if t is not None:
                try:
                    await t
                except BaseException:  # noqa: BLE001 —— 取消/收尾异常均吞掉
                    pass
        self._reader = None
        self._stderr_task = None
        self._fail_pending("MCP 客户端已关闭")
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

    def _fail_pending(self, reason: str) -> None:
        """在途请求立即报错（子进程退出 / 客户端关闭），不留悬挂 future。"""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._proc is not None
        try:
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
        finally:
            self._fail_pending("MCP 子进程已退出（stdout EOF）")

    async def _stderr_loop(self) -> None:
        """持续消费 stderr（S2）：防止话痨子进程写满管道缓冲区而卡死。"""
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if self._on_stderr is not None:
                try:
                    self._on_stderr(text)
                except Exception:  # noqa: BLE001 —— 回调异常绝不影响主链路
                    pass
            else:
                logger.debug("mcp stderr: %s", text)

    async def _request(self, method: str, params: dict) -> Any:
        if self._proc is None or self._proc.stdin is None:
            raise ConnectionError("MCP 客户端未启动或已关闭")
        self._rid += 1
        rid = self._rid
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            self._proc.stdin.write((payload + "\n").encode("utf-8"))
            await self._proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)       # 迟到的响应不得悬挂在表里
            raise
        except asyncio.CancelledError:
            self._pending.pop(rid, None)
            raise

    def _notify(self, method: str, params: dict) -> None:
        if self._proc is not None and self._proc.stdin is not None:
            payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
            self._proc.stdin.write((payload + "\n").encode("utf-8"))


__all__ = ["McpStdioClient"]