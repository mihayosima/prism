"""插件包：playwright_mcp —— 把 microsoft/playwright-mcp（MCP stdio）封装为 agent 工具插件。

管理：本包位于 agent/plugins/，经「插件」页可停用 / 启用 / 卸载（引擎式 desired/actual/delta）。
默认未启用——在插件页点「启用」（或 state 文件 enabled 加入本插件 id），工具即出现在 agent 工具目录。

- 启动一个 MCP stdio 子进程（@playwright/mcp，如 npx 入口），
  其工具（browser_navigate / browser_click / …）自动注册到 agent 工具目录，供 chat tool_call 调用。
- 默认 headless + chromium（后台服务无显示器）；仅核心自动化（不挂 --caps，走无障碍树，无需视觉模型）。
- 生命周期对称：apply 起子进程 → effect 关子进程；guard 矩阵照常管辖 tool 调用。
- 诚实边界：未配置 command → 装配时报告未就绪（不假挂）；进程启动失败 → 报告原因。
"""

from __future__ import annotations

import json
import logging
import os
import shutil

from agent.api import Services, StateKeys, plugin
from agent.sys.mcp_client import McpStdioClient

logger = logging.getLogger("agent.plugins.playwright_mcp")


def _env_args(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else [str(v)]
    except Exception:  # noqa: BLE001
        return [x for x in raw.split(",") if x.strip()]


def resolve(cfg: dict, environ: dict | None = None) -> tuple[str, list[str], float]:
    """解析启动命令（纯函数，便于离线测试）：config > env > 默认 npx + headless chromium。"""
    environ = environ or {}
    command = cfg.get("command") or environ.get("PLAYWRIGHT_MCP_COMMAND") or "npx"
    # Windows 上 npx 是 .cmd 包装；asyncio.create_subprocess_exec 直接 exec 时不会自动补
    # .cmd 扩展名，需要在 PATH 中解析为绝对路径以保证子进程能可靠启动。
    if os.name == "nt" and not os.path.isabs(command) \
            and not command.lower().endswith((".cmd", ".exe", ".bat", ".ps1")):
        alt = shutil.which(command + ".cmd") or shutil.which(command + ".exe") or shutil.which(command)
        if alt:
            command = alt
    args = list(cfg.get("args") or _env_args(environ.get("PLAYWRIGHT_MCP_ARGS")))
    if not args:
        args = ["-y", "@playwright/mcp@latest"]
    # 默认 headless（后台服务无显示器）；可配置 headless=false 走有头（需 DISPLAY）
    if cfg.get("headless", True):
        args.append("--headless")
    browser = cfg.get("browser") or environ.get("PLAYWRIGHT_MCP_BROWSER") or "chromium"
    args += ["--browser", browser]
    if cfg.get("isolated"):
        args.append("--isolated")
    caps = cfg.get("caps")  # 默认不开：仅核心自动化（无障碍树，无需视觉模型）
    if caps:
        args += ["--caps", caps if isinstance(caps, str) else ",".join(caps)]
    timeout = float(cfg.get("timeout_s", 120.0))
    return command, args, timeout


@plugin
class PlaywrightMcp:
    """把 playwright-mcp 的工具挂进 agent 工具目录（可管理插件）。"""

    name = "plugins.playwright_mcp"
    version = "0.1.0"
    layer = "business"
    provides = [Services.PLAYWRIGHT_MCP]
    description = "playwright-mcp 工具桥（MCP stdio → agent tool_call，headless chromium）"

    def __init__(self):
        self._client: McpStdioClient | None = None

    async def apply(self, ctx) -> None:
        cfg = (ctx.config or {}).get("playwright_mcp") or {}
        command, args, timeout = resolve(cfg, os.environ)
        client = McpStdioClient(command, args, dict(cfg.get("env") or {}), timeout_s=timeout)
        try:
            await client.start()
            tools = await client.list_tools()
        except Exception as exc:  # noqa: BLE001 —— 起不来诚实报，不拖垮装配
            logger.warning("playwright_mcp 启动失败: %s", exc)
            ctx.provide(Services.PLAYWRIGHT_MCP, self)
            ctx.state.setdefault(StateKeys.MCP_ERROR.format(bridge="playwright_mcp"), str(exc))
            return
        self._client = client
        ctx.state.setdefault(StateKeys.MCP_TOOLS.format(bridge="playwright_mcp"),
                             [t.get("name") for t in tools])
        _TYPE = {"string": "str", "number": "float", "integer": "int",
                 "boolean": "bool", "array": "array", "object": "object"}
        for t in tools:
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            task_schema = {name: {"type": _TYPE.get((props[name] or {}).get("type", "object"), "object"),
                                  "required": name in required}
                           for name in props}
            ctx.tasks.register(ctx, f"mcp.playwright.{t.get('name')}", schema=task_schema,
                               handler=self._make_handler(t.get("name")),
                               owner=self.name,
                               description=f"playwright-mcp: {t.get('description') or t.get('name')}")
        ctx.provide(Services.PLAYWRIGHT_MCP, self)
        ctx.effect(self._close)

    def _make_handler(self, tool_name: str):
        async def _call(**args):
            if self._client is None:
                raise RuntimeError("playwright_mcp 未就绪")
            return await self._client.call_tool(tool_name, args)
        return _call

    async def _close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def status(self, ctx) -> dict:
        return {"tools": ctx.state.get(StateKeys.MCP_TOOLS.format(bridge="playwright_mcp"), []),
                "error": ctx.state.get(StateKeys.MCP_ERROR.format(bridge="playwright_mcp"))}


__all__ = ["PlaywrightMcp"]
