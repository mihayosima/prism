"""插件包：skills_mcp —— 技能 MCP 桥。

与 ``playwright_mcp`` / ``windows_mcp`` 同形态：起一个 stdio 子进程，把它的工具注册进 agent 工具目录，供 chat tool_call 调用。

- 只读 + 可选执行：对端常驻 ``skill_list`` / ``skill_view``；配置 ``allow_exec``
 时额外暴露 Follow 三件套，
 在宿主环境直接操作电脑。执行能力不经内核 ``Skill.run``，由对端 adapter 子进程直接落地。
- 子进程无 ctx：适配器拿不到 ``ctx``，故经环境变量 ``SKILLS_DATA_DIR`` 把数据目录
 传进去，让它从磁盘重建目录——与内核 ``plugins/skills`` 读同一份 ``<data_dir>/skills/*.md``。
 数据目录取法同 ``plugins/skills._resolve_data_dir``。
- 默认启用：``enabled`` 默认真，除非显式 ``false`` 即起子进程；
 若需关闭，配置节写 ``enabled: false`` 即 ``apply`` 早退——
 不起进程、不 import 对端模块、不注册工具，零影响。
 配置节位置见 ``_section``：``agent.skills_mcp`` 优先，回退顶层 ``skills_mcp``。
- 零耦合：本插件只 import ``agent.api``；不 import ``sys.skills`` / ``sys.tasks`` /
 ``plugins.react``。
- 生命周期对称：``apply`` 起子进程 → ``ctx.effect`` 关子进程；工具调用照常受 guard 管辖。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from agent.api import (DEFAULT_DB_PATH, McpStdioClient, Services, StateKeys,
                       build_task_schema, call_spawn_provider, current_session_key,
                       plugin)

logger = logging.getLogger("agent.plugins.skills_mcp")

_BRIDGE = "skills_mcp"

# : P3：本适配器里确属只读的工具名。
# : 依据：``plugins/skills/adapter.py`` 的两个工具只读 ``data/skills`` 后返回文本
# 无任何写/执行副作用。
# : 未列入本集合的工具一律沿用 ``risk="write"``。
# : B2：``skill_bash`` 是高危执行工具，单独列 ``_DANGER_TOOLS``，
# 注册时标 ``risk="danger"``。
_READ_ONLY_TOOLS = frozenset({"skill_list", "skill_view"})
_DANGER_TOOLS = frozenset({"skill_bash"})

# : 适配器模块。
#: 归位：适配器从 ``agent.run`` 迁入 ``plugins/skills`` 包内
# : ——它消费技能目录，与目录同包才能用包内相对导入；
# : 插件间禁止横向 import，故不能放本包再 import
# : skills 包。本桥只经这行 spawn 路径字符串引用它：字符串不是 import，不构成耦合。
_ADAPTER_MODULE = "agent.plugins.skills.adapter"


def _on_stderr(text: str) -> None:
    """子进程 stderr → warning。"""
    if text.strip():
        logger.warning("[%s] %s", _BRIDGE, text)


def resolve(cfg: dict, environ: dict | None = None) -> tuple[str, list[str], float]:
    """解析启动命令：config > env > 默认「当前解释器 + -m 模块」。

     默认用 ``sys.executable`` 而非裸 ``python``：Windows 下避免 PATH 解析差异，
     且子进程与宿主同解释器 ⇒ 一定能 import 到本仓的适配器模块。
     """
    environ = environ or {}
    command = cfg.get("command") or environ.get("SKILLS_MCP_COMMAND") or sys.executable
    args = list(cfg.get("args") or ["-m", _ADAPTER_MODULE])
    timeout = float(cfg.get("timeout_s", 60.0))
    return command, args, timeout


def _section(ctx) -> dict:
    """取本桥配置节：``agent.skills_mcp`` 优先，回退顶层 ``skills_mcp``。

     两条通路并存是有意为之，不是"猜节名"：

     - ``_merge_cfg`` 把容器级覆盖按 ``plugins.<名>`` → ``agent.<名>`` 映射 ⇒ 前端设置页 /
     ``data/agent_plugin_config.json`` 写进来的配置落在 ``agent`` 节；
     - 既有两个 MCP 桥读顶层同名节，且顶层在
     ``_SCHEMA`` 里已登记为 ``_any``。

     两处都认 ⇒ 覆盖文件与直接写基础 config 都能生效；顶层回退保证不劣化既有先例行为。
     """
    cfg = (getattr(ctx, "config", None) or {})
    return ((cfg.get("agent") or {}).get("skills_mcp") or cfg.get("skills_mcp") or {})


@plugin
class SkillsMcp:
    """把本仓技能目录以只读 MCP 工具挂进 agent 工具目录。"""

    name = "plugins.skills_mcp"
    version = "0.1.0"
    layer = "business"
    provides = [Services.SKILLS_MCP]
    # apply() 里 ctx.tasks.register(...)：声明内核服务依赖，装配顺序才可机器校验
    inject = [Services.TASKS]
    description = "技能 MCP 桥（skill_list / skill_view 只读；allow_exec 开时加 skill_bash / skill_file_create / skill_str_replace 直接操作电脑）"

    def __init__(self):
        self._client: McpStdioClient | None = None
        self._cfg: dict = {}
        self._ctx = None
        self._tools: list[str] = []
        self._lock = asyncio.Lock()

    def _data_dir(self) -> str:
        """技能数据目录：配置覆盖 > ``db_path`` 的父目录。"""
        if self._cfg.get("data_dir"):
            return str(self._cfg["data_dir"])
        cfg = (self._ctx.config if self._ctx is not None else None) or {}
        db_path = cfg.get("db_path") or DEFAULT_DB_PATH
        return str(Path(db_path).parent)

    def _workspace(self) -> dict | None:
        """取沙箱落点；沙箱未装配 ⇒ ``None``。

                 P2：此前本桥没有接 ``CHILD_SPAWN_OPTIONS``，而另两个 MCP 桥
                 早已接了 ⇒ 不一致：本桥子进程的 cwd 仍是项目根。
                 此处按另两桥同款接法补齐；取不到就退回无落点，绝不把桥启动搞崩。
                 """
        if self._ctx is None:
            return None
        # 用 getattr 取而不是直接 ``self._ctx.get(...)``：部分装配/测试桩的 ctx 未必
        # 实现 ``get``。沙箱是增强维度，缺了就退回无落点。
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
            # 把桥 id 一起传进去 —— 可移植性是按桥判定的，
            # 提供方据此决定本桥要不要降级。
            return call_spawn_provider(fn, current_session_key(self._ctx), _BRIDGE)
        except Exception:                       # noqa: BLE001 —— 沙箱故障 ⇒ 退回无落点
            logger.warning("skills_mcp 取沙箱落点失败 ⇒ 沿用宿主 cwd")
            return None

    def _spawn(self, cfg: dict) -> McpStdioClient:
        """按配置造一个客户端。"""
        command, args, timeout = resolve(cfg, {})
        # 默认完全权限——allow_exec 默认开、workdir 默认用户主目录
        allow_exec = cfg.get("allow_exec", True)
        workdir = str(cfg.get("workdir") or Path.home())
        deny = cfg.get("bash_deny") or []
        deny_str = ",".join(deny) if isinstance(deny, list) else str(deny)
        env = {
            "SKILLS_DATA_DIR": self._data_dir(),
            "SKILLS_ALLOW_EXEC": "1" if allow_exec else "0",
            "SKILLS_WORKDIR": workdir,
            "SKILLS_EXEC_TIMEOUT_S": str(float(cfg.get("timeout_s", 120.0))),
            "SKILLS_BASH_DENY": deny_str,
        }
        env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
        ws = self._workspace()
        if ws:
            env.update(ws.get("env") or {})
            logger.info("skills_mcp 落点收拢：cwd=%s", ws.get("cwd"))
        # 跨边界隔离需要改写 argv 本身，光 cwd/env 挡不住命令。
        # ``wrap`` 是接缝新增键；本桥显式支持。
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
        if not cfg.get("enabled", True):
            # 未启用：只登记自身，不起进程、不注册工具 ⇒ 零影响
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
            # risk 不再吃 ``register`` 的默认值 "write"。此前三个 MCP 桥注册时
            # 都没传 risk ⇒ 连 ``skill_list`` 这种纯读工具也是 write 级 ⇒ guard 在
            # risk.ask 下"读个技能也要问人"、plan 模式被裁 —— 只读适配器名不副实的漏洞。
            ctx.tasks.register(ctx, f"mcp.skills.{name}", schema=task_schema,
                               handler=self._make_handler(name),
                               owner=self.name,
                               risk=("danger" if name in _DANGER_TOOLS
                                     else "read" if name in _READ_ONLY_TOOLS
                                     else "write"),
                               description=f"skills-mcp: {t.get('description') or name}")
        ctx.provide(Services.SKILLS_MCP, self)
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
