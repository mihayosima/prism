"""插件包：skills_mcp —— 只读技能 MCP 桥（把本仓技能目录以 MCP 工具对外暴露）。

与 ``playwright_mcp`` / ``windows_mcp`` **同形态**：起一个 stdio 子进程（本仓自家的
``agent.run.skills_mcp_adapter``），把它的工具注册进 agent 工具目录，供 chat tool_call 调用。

- **只读**：对端只暴露 ``skill_list`` / ``skill_view``，无任何写/执行动作（执行仍由内核
  ``Skill.run`` 承担）。
- **子进程无 ctx**：适配器拿不到 ``ctx``，故经环境变量 ``SKILLS_DATA_DIR`` 把数据目录
  传进去，让它从磁盘重建目录——与内核 ``plugins/skills`` 读同一份 ``<data_dir>/skills/*.md``。
  数据目录取法同 ``plugins/skills._resolve_data_dir``（= ``db_path`` 的父目录）。
- **默认未启用**：配置节 ``enabled`` 为真才起子进程；否则 ``apply`` 早退，
  **不起进程、不 import 对端模块、不注册工具**，零影响。
  配置节位置见 ``_section``：``agent.skills_mcp``（覆盖文件通路）优先，回退顶层 ``skills_mcp``。
- **零耦合**：本插件只 import ``agent.api``；不 import ``sys.skills`` / ``sys.tasks`` /
  ``plugins.react``（由 ``test_services_contract`` 的 AST 守卫守住）。
- 生命周期对称：``apply`` 起子进程 → ``ctx.effect`` 关子进程；工具调用照常受 guard 管辖。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from agent.api import (DEFAULT_DB_PATH, McpStdioClient, Services, StateKeys,
                       build_task_schema, current_session_key, plugin)

logger = logging.getLogger("agent.plugins.skills_mcp")

_BRIDGE = "skills_mcp"

#: ★ P3（2026-10-13）：本适配器里**确属只读**的工具名。
#: 依据：``run/skills_mcp_adapter.py`` 的两个工具只读 ``data/skills`` 后返回文本
#（``skill_list`` 列卡片、``skill_view`` 读正文），**无任何写/执行副作用**。
#: 未列入本集合的工具一律沿用 ``risk="write"``（保守：不因猜错而放行危险工具）。
#: ⚠️ 后续 Follow 三件套（skill_bash/file_create/str_replace）**绝不可**加进这里。
_READ_ONLY_TOOLS = frozenset({"skill_list", "skill_view"})

#: 适配器模块（以 ``python -m`` 方式拉起，无需额外安装任何依赖）。
_ADAPTER_MODULE = "agent.run.skills_mcp_adapter"


def _on_stderr(text: str) -> None:
    """子进程 stderr → warning（死因的唯一通路，绝不降级丢弃）。"""
    if text.strip():
        logger.warning("[%s] %s", _BRIDGE, text)


def resolve(cfg: dict, environ: dict | None = None) -> tuple[str, list[str], float]:
    """解析启动命令（纯函数，便于离线测试）：config > env > 默认「当前解释器 + -m 模块」。

    默认用 ``sys.executable``（绝对路径）而非裸 ``python``：Windows 下避免 PATH 解析差异，
    且子进程与宿主同解释器 ⇒ 一定能 import 到本仓的适配器模块。
    """
    environ = environ or {}
    command = cfg.get("command") or environ.get("SKILLS_MCP_COMMAND") or sys.executable
    args = list(cfg.get("args") or ["-m", _ADAPTER_MODULE])
    timeout = float(cfg.get("timeout_s", 60.0))
    return command, args, timeout


def _section(ctx) -> dict:
    """取本桥配置节：``agent.skills_mcp``（覆盖通道）优先，回退顶层 ``skills_mcp``。

    两条通路并存是有意为之，不是"猜节名"：

    - ``_merge_cfg`` 把容器级覆盖按 ``plugins.<名>`` → ``agent.<名>`` 映射 ⇒ 前端设置页 /
      ``data/agent_plugin_config.json`` 写进来的配置**落在 ``agent`` 节**；
    - 既有两个 MCP 桥（``playwright_mcp`` / ``windows_mcp``）读**顶层**同名节，且顶层在
      ``_SCHEMA`` 里已登记为 ``_any``。

    两处都认 ⇒ 覆盖文件与直接写基础 config 都能生效；顶层回退保证不劣化既有先例行为。
    """
    cfg = (getattr(ctx, "config", None) or {})
    return ((cfg.get("agent") or {}).get("skills_mcp") or cfg.get("skills_mcp") or {})


@plugin
class SkillsMcp:
    """把本仓技能目录以只读 MCP 工具挂进 agent 工具目录（可管理插件）。"""

    name = "plugins.skills_mcp"
    version = "0.1.0"
    layer = "business"
    provides = [Services.SKILLS_MCP]
    # apply() 里 ctx.tasks.register(...)：声明内核服务依赖，装配顺序才可机器校验
    inject = [Services.TASKS]
    description = "只读技能 MCP 桥（skill_list / skill_view；经 stdio 子进程暴露本仓技能）"

    def __init__(self):
        self._client: McpStdioClient | None = None
        self._cfg: dict = {}
        self._ctx = None
        self._tools: list[str] = []
        self._lock = asyncio.Lock()

    def _data_dir(self) -> str:
        """技能数据目录（传给子进程）：配置覆盖 > ``db_path`` 的父目录。"""
        if self._cfg.get("data_dir"):
            return str(self._cfg["data_dir"])
        cfg = (self._ctx.config if self._ctx is not None else None) or {}
        db_path = cfg.get("db_path") or DEFAULT_DB_PATH
        return str(Path(db_path).parent)

    def _workspace(self) -> dict | None:
        """取沙箱落点（``{"cwd","env"}``）；沙箱未装配 ⇒ ``None``（沿用宿主 cwd，不劣化）。

        ★ P2（2026-10-13）：此前本桥**没有接** ``CHILD_SPAWN_OPTIONS``，而另两个 MCP 桥
        （playwright / windows）早已接了 ⇒ 不一致：本桥子进程的 cwd 仍是**项目根**
        （那里有 ``local_secrets.json`` 明文 key 与 ``data/*.db`` 全量数据）。
        此处按另两桥**同款接法**补齐；取不到就退回无落点，**绝不把桥启动搞崩**。
        """
        if self._ctx is None:
            return None
        # ★ 用 getattr 取而不是直接 ``self._ctx.get(...)``：部分装配/测试桩的 ctx 未必
        # 实现 ``get``。沙箱是**增强维度**，缺了就退回无落点。
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
            return fn(current_session_key(self._ctx))
        except Exception:                       # noqa: BLE001 —— 沙箱故障 ⇒ 退回无落点
            logger.warning("skills_mcp 取沙箱落点失败 ⇒ 沿用宿主 cwd")
            return None

    def _spawn(self, cfg: dict) -> McpStdioClient:
        """按配置造一个客户端（注入 ``SKILLS_DATA_DIR``；装上沙箱时再叠落点）。"""
        command, args, timeout = resolve(cfg, {})
        env = {"SKILLS_DATA_DIR": self._data_dir()}
        env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
        ws = self._workspace()
        if ws:
            env.update(ws.get("env") or {})
            logger.info("skills_mcp 落点收拢：cwd=%s", ws.get("cwd"))
        # ★ P4：跨边界隔离（wsl2/容器）需要改写 argv 本身，光 cwd/env 挡不住命令。
        # ``wrap`` 是接缝新增键（既有桥不读 ⇒ 向后兼容）；本桥显式支持。
        wrap = (ws or {}).get("wrap")
        if wrap:
            logger.info("skills_mcp 命令包装：backend=%s mode=%s",
                        (ws or {}).get("backend"), wrap.get("mode"))
        return McpStdioClient(command, args, env, timeout_s=timeout,
                              on_stderr=_on_stderr, cwd=(ws or {}).get("cwd"),
                              wrap=wrap)

    async def apply(self, ctx) -> None:
        cfg = _section(ctx)
        self._cfg = cfg
        self._ctx = ctx
        if not cfg.get("enabled", False):
            # 未启用：只登记自身（供 status/插件页显示），不起进程、不注册工具 ⇒ 零影响
            ctx.provide(Services.SKILLS_MCP, self)
            return
        client = self._spawn(cfg)
        try:
            await client.start()
            tools = await client.list_tools()
        except Exception as exc:                      # noqa: BLE001 —— 起不来诚实报，不拖垮装配
            logger.warning("skills_mcp 启动失败: %s", exc)
            try:
                await client.close()
            except Exception:                         # noqa: BLE001
                pass
            ctx.provide(Services.SKILLS_MCP, self)
            ctx.state[StateKeys.MCP_ERROR.format(bridge=_BRIDGE)] = str(exc)
            return
        self._client = client
        self._tools = [t.get("name") for t in tools]
        ctx.state[StateKeys.MCP_TOOLS.format(bridge=_BRIDGE)] = self._tools
        ctx.state[StateKeys.MCP_ERROR.format(bridge=_BRIDGE)] = None
        for t in tools:
            name = str(t.get("name") or "")
            task_schema = build_task_schema(t.get("inputSchema"))
            # ★ P3：risk **不再吃 ``register`` 的默认值 "write"**。此前三个 MCP 桥注册时
            # 都没传 risk ⇒ 连 ``skill_list`` 这种纯读工具也是 write 级 ⇒ guard 在
            # risk.ask 下"读个技能也要问人"、plan 模式被裁 —— 只读适配器名不副实的漏洞。
            ctx.tasks.register(ctx, f"mcp.skills.{name}", schema=task_schema,
                               handler=self._make_handler(name),
                               owner=self.name,
                               risk="read" if name in _READ_ONLY_TOOLS else "write",
                               description=f"skills-mcp: {t.get('description') or name}")
        ctx.provide(Services.SKILLS_MCP, self)
        ctx.effect(self._close)

    def _make_handler(self, tool_name: str):
        async def _call(**args):
            client = await self._ensure_client()
            return await client.call_tool(tool_name, args)
        return _call

    async def _ensure_client(self) -> McpStdioClient:
        """探活 + 自愈：活着直接用；死了就地重启（免去重启整个后端）。"""
        c = self._client
        if c is not None and c.is_alive:
            return c
        async with self._lock:                        # 并发调用只重启一次
            c = self._client
            if c is not None and c.is_alive:
                return c
            dead = self._client.dead_reason if self._client is not None else "尚未就绪"
            logger.warning("skills_mcp 子进程不可用（%s），正在就地重启…", dead)
            client = self._spawn(self._cfg)
            await client.start()
            self._client = client
            return client

    async def _close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def status(self, ctx) -> dict:
        c = self._client
        return {"enabled": bool((self._cfg or {}).get("enabled", False)),
                "tools": ctx.state.get(StateKeys.MCP_TOOLS.format(bridge=_BRIDGE), []),
                "error": ctx.state.get(StateKeys.MCP_ERROR.format(bridge=_BRIDGE)),
                "alive": bool(c is not None and c.is_alive)}


__all__ = ["SkillsMcp", "resolve"]
