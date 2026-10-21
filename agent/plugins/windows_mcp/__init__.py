"""插件包：windows_mcp —— 把 windows-mcp-server 封装为 agent 工具插件。

管理：本包位于 agent/plugins/，经「插件」页可停用 / 启用 / 卸载。
默认未启用——在插件页点「启用」，或配置 env，工具即出现在 Agent 的工具目录里。

- 启动一个 MCP stdio 子进程，
 其工具自动注册到 agent 工具目录，供 chat tool_call 调用。
- 生命周期对称：apply 起子进程 → effect 关子进程；guard 矩阵照常管辖 tool 调用。
- 诚实边界：未配置 command → 装配时报告未就绪；进程启动失败 → 报告原因。

S4 加固：
- 子进程 stderr 经 ``on_stderr`` 打到 warning；
- handler 调用前探活，发现已死就就地重启子进程
 ⇒ 不必再重启整个后端；重启失败则如实抛错并写入 ``MCP_ERROR``；
- ``status()`` 增 ``alive`` 实时字段 ⇒ 插件页不再显示"假健康"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from agent.api import Services, StateKeys, current_session_key, plugin
from agent.api import McpStdioClient, build_task_schema, call_spawn_provider

logger = logging.getLogger("agent.plugins.windows_mcp")

_BRIDGE = "windows_mcp"


def _on_stderr(text: str) -> None:
    """子进程 stderr → warning。"""
    if text.strip():
        logger.warning("[%s] %s", _BRIDGE, text)


def _env_args(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else [str(v)]
    except Exception:  # noqa: BLE001
        return [x for x in raw.split(",") if x.strip()]


def resolve(cfg: dict, environ: dict | None = None) -> tuple[str, list[str], float]:
    """解析启动命令：config > env > 默认 uvx。"""
    environ = environ or {}
    command = cfg.get("command") or environ.get("WINDOWS_MCP_COMMAND") or "uvx"
    args = list(cfg.get("args") or _env_args(environ.get("WINDOWS_MCP_ARGS")))
    if not args:
        args = ["windows-mcp", "serve"]
    timeout = float(cfg.get("timeout_s", 120.0))
    return command, args, timeout


@plugin
class WindowsMcp:
    """把 windows-mcp-server 的工具挂进 agent 工具目录。"""

    name = "plugins.windows_mcp"
    version = "0.1.0"
    layer = "business"
    provides = [Services.WINDOWS_MCP]
    # apply() 里 ctx.tasks.register(...)：声明内核服务依赖，装配顺序才可机器校验
    inject = [Services.TASKS]
    description = "windows-mcp-server 工具桥（MCP stdio → agent tool_call）"

    def __init__(self):
        self._client: McpStdioClient | None = None
        self._cfg: dict = {}
        self._ctx = None
        self._tools: list[str] = []
        self._lock = asyncio.Lock()

    def _spawn(self, cfg: dict) -> McpStdioClient:
        """按配置造一个客户端。"""
        command, args, timeout = resolve(cfg, os.environ)
        env = dict(cfg.get("env") or {})
        cwd = self._workspace()
        if cwd:
            env.update(cwd.get("env") or {})
            logger.info("windows_mcp 落点收拢：cwd=%s", cwd.get("cwd"))
        return McpStdioClient(command, args, env, timeout_s=timeout,
                              on_stderr=_on_stderr, cwd=(cwd or {}).get("cwd"))

    def _workspace(self) -> dict | None:
        """取沙箱落点接缝：声明了档位 ⇒ 把子进程挪出项目根。

         沙箱未装配 / ``mode=none`` ⇒ ``None`` ⇒ 行为与今天完全一致。
         接缝故障不得拖垮桥启动，沙箱是增强维度，不是硬前提。
         """
        if self._ctx is None:
            return None
        # 用 getattr 取而不是直接 ``self._ctx.get(...)``：部分装配/测试桩的 ctx 未必
        # 实现 ``get``。沙箱是增强维度，缺了就退回无落点，绝不能把桥启动搞崩。
        getter = getattr(self._ctx, "get", None)
        if not callable(getter):
            return None
        try:
            fn = getter(Services.CHILD_SPAWN_OPTIONS)
        except Exception:                       # noqa: BLE001
            return None
        if not callable(fn):
            return None
        try:
            # 传桥 id —— windows_mcp 是不可移植目标，
            # 由沙箱侧据此把本桥降级为 off，而不是硬套包装把它弄崩。
            return call_spawn_provider(fn, current_session_key(self._ctx), _BRIDGE)
        except Exception:                       # noqa: BLE001 —— 沙箱故障 ⇒ 退回无落点
            logger.warning("windows_mcp 取沙箱落点失败 ⇒ 沿用宿主 cwd")
            return None

    async def apply(self, ctx) -> None:
        cfg = (ctx.config or {}).get("windows_mcp") or {}
        self._cfg = cfg
        self._ctx = ctx
        client = self._spawn(cfg)
        try:
            await client.start()
            tools = await client.list_tools()
        except Exception as exc:  # noqa: BLE001 —— 起不来诚实报，不拖垮装配
            logger.warning("windows_mcp 启动失败: %s", exc)
            # 起不来的子进程同样要收干净，否则 uvx→uv→python 会变孤儿长期驻留
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
            ctx.provide(Services.WINDOWS_MCP, self)
            ctx.state[StateKeys.MCP_ERROR.format(bridge=_BRIDGE)] = str(exc)
            return
        self._client = client
        self._tools = [t.get("name") for t in tools]
        # 用赋值而非 setdefault：运行期自愈重启后需要刷新这两项
        ctx.state[StateKeys.MCP_TOOLS.format(bridge=_BRIDGE)] = self._tools
        ctx.state[StateKeys.MCP_ERROR.format(bridge=_BRIDGE)] = None
        for t in tools:
            # S6/P2：保真转换，
            # 此前只抄 type ⇒ 模型的工具卡里看不到 mode 的合法取值
            task_schema = build_task_schema(t.get("inputSchema"))
            ctx.tasks.register(ctx, f"mcp.windows.{t.get('name')}", schema=task_schema,
                               handler=self._make_handler(t.get("name")),
                               owner=self.name,
                               description=f"windows-mcp: {t.get('description') or t.get('name')}")
        ctx.provide(Services.WINDOWS_MCP, self)
        ctx.effect(self._close)

    def _make_handler(self, tool_name: str):
        async def _call(**args):
            client = await self._ensure_client()
            return await client.call_tool(tool_name, args)
        return _call

    async def _ensure_client(self) -> McpStdioClient:
        """探活 + 自愈：活着直接用；死了就地重启。"""
        c = self._client
        if c is not None and c.is_alive:
            return c
        async with self._lock:                  # 并发调用只重启一次
            c = self._client
            if c is not None and c.is_alive:
                return c
            return await self._restart()

    async def _restart(self) -> McpStdioClient:
        ctx = self._ctx
        key_err = StateKeys.MCP_ERROR.format(bridge=_BRIDGE)
        dead = self._client.dead_reason if self._client is not None else "尚未就绪"
        logger.warning("windows_mcp 子进程不可用（%s），正在就地重启…", dead)
        old, self._client = self._client, None
        if old is not None:
            try:
                await old.close()
            except Exception:  # noqa: BLE001 —— 旧连接收尾失败不阻断重启
                pass
        try:
            client = self._spawn(self._cfg)
            await client.start()
            tools = await client.list_tools()
        except Exception as exc:  # noqa: BLE001
            logger.warning("windows_mcp 重启失败: %s", exc)
            if ctx is not None:
                ctx.state[key_err] = f"子进程已退出（{dead}）且重启失败: {exc}"
            raise RuntimeError(f"windows_mcp 不可用（重启失败: {exc}）") from exc
        self._client = client
        names = [t.get("name") for t in tools]
        if ctx is not None:
            ctx.state[StateKeys.MCP_TOOLS.format(bridge=_BRIDGE)] = names
            ctx.state[key_err] = None
        if sorted(names) != sorted(self._tools):
            logger.warning("windows_mcp 重启后工具集变化（%s → %s）；本次仅恢复连接，"
                           "新增/更名工具需重装插件才会出现在工具目录", self._tools, names)
        self._tools = names
        logger.warning("windows_mcp 已恢复（%d 个工具）", len(names))
        return client

    async def _close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def status(self, ctx) -> dict:
        c = self._client
        return {"tools": ctx.state.get(StateKeys.MCP_TOOLS.format(bridge=_BRIDGE), []),
                "error": ctx.state.get(StateKeys.MCP_ERROR.format(bridge=_BRIDGE)),
                # 实时存活，避免"假健康"
                "alive": bool(c is not None and c.is_alive)}


__all__ = ["WindowsMcp"]
