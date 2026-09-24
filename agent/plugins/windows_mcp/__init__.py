"""插件包：windows_mcp —— 把 windows-mcp-server（MCP stdio）封装为 agent 工具插件。

管理：本包位于 agent/plugins/，经「插件」页可停用 / 启用 / 卸载（引擎式 desired/actual/delta）。
默认未启用——在插件页点「启用」，或配置 env，工具即出现在 Agent 的工具目录里。

- 启动一个 MCP stdio 子进程（windows-mcp-server，如 node/npx/npx.cmd 入口），
  其工具（process/screenshot/window 等）自动注册到 agent 工具目录，供 chat tool_call 调用。
- 生命周期对称：apply 起子进程 → effect 关子进程；guard 矩阵照常管辖 tool 调用。
- 诚实边界：未配置 command → 装配时报告未就绪（不假挂）；进程启动失败 → 报告原因。
"""

from __future__ import annotations

import json
import logging
import os

from agent.api import plugin
from agent.sys.mcp_client import McpStdioClient

logger = logging.getLogger("agent.plugins.windows_mcp")


def _env_args(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else [str(v)]
    except Exception:  # noqa: BLE001
        return [x for x in raw.split(",") if x.strip()]


def resolve(cfg: dict, environ: dict | None = None) -> tuple[str, list[str], float]:
    """解析启动命令（纯函数，便于离线测试）：config > env > 默认 uvx。"""
    environ = environ or {}
    command = cfg.get("command") or environ.get("WINDOWS_MCP_COMMAND") or "uvx"
    args = list(cfg.get("args") or _env_args(environ.get("WINDOWS_MCP_ARGS")))
    if not args:
        args = ["windows-mcp", "serve"]
    timeout = float(cfg.get("timeout_s", 120.0))
    return command, args, timeout


@plugin
class WindowsMcp:
    """把 windows-mcp-server 的工具挂进 agent 工具目录（可管理插件）。"""

    name = "plugins.windows_mcp"
    version = "0.1.0"
    layer = "business"
    provides = ["windows_mcp"]
    description = "windows-mcp-server 工具桥（MCP stdio → agent tool_call）"

    def __init__(self):
        self._client: McpStdioClient | None = None

    async def apply(self, ctx) -> None:
        cfg = (ctx.config or {}).get("windows_mcp") or {}
        command, args, timeout = resolve(cfg, os.environ)
        client = McpStdioClient(command, args, dict(cfg.get("env") or {}), timeout_s=timeout)
        try:
            await client.start()
            tools = await client.list_tools()
        except Exception as exc:  # noqa: BLE001 —— 起不来诚实报，不拖垮装配
            logger.warning("windows_mcp 启动失败: %s", exc)
            ctx.provide("windows_mcp", self)
            ctx.state.setdefault("windows_mcp_error", str(exc))
            return
        self._client = client
        ctx.state.setdefault("windows_mcp_tools", [t.get("name") for t in tools])
        _TYPE = {"string": "str", "number": "float", "integer": "int",
                 "boolean": "bool", "array": "array", "object": "object"}
        for t in tools:
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            task_schema = {name: {"type": _TYPE.get((props[name] or {}).get("type", "object"), "object"),
                                  "required": name in required}
                           for name in props}
            ctx.tasks.register(ctx, f"mcp.windows.{t.get('name')}", schema=task_schema,
                               handler=self._make_handler(t.get("name")),
                               owner=self.name,
                               description=f"windows-mcp: {t.get('description') or t.get('name')}")
        ctx.provide("windows_mcp", self)
        ctx.effect(self._close)

    def _make_handler(self, tool_name: str):
        async def _call(**args):
            if self._client is None:
                raise RuntimeError("windows_mcp 未就绪")
            return await self._client.call_tool(tool_name, args)
        return _call

    async def _close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def status(self, ctx) -> dict:
        return {"tools": ctx.state.get("windows_mcp_tools", []),
                "error": ctx.state.get("windows_mcp_error")}


__all__ = ["WindowsMcp"]