"""插件包：system_memory —— 系统记忆。

目录插件，与短期记忆 ``plugins.short_term``、
长期记忆 ``plugins.memory`` 平级；经「插件」页可启用 / 停用 / 卸载。

- 语义：内容层——独占「身份 / 边界」叙事；
- 加载时机：仅插件 apply 时读入一次并缓存，不热重载；
- 外部目录配置 ``agent.system_memory.dir`` 完全覆盖默认目录。

解耦纪律：
- 消费面一律 ``ctx.get(Services.SYSTEM_MEMORY) or ""`` 软依赖——停用即空底座；
- 无表、无 db/llm 依赖；零特权：只做文件 IO。
"""

from __future__ import annotations

from agent.api import Services, plugin

from .loader import DEFAULT_DIR, load_system_memory


@plugin
class SystemMemory:
    """ctx.system_memory —— 启动从文件全量加载身份/边界叙事，作各阶段 system 提示静态底座。"""

    name = "plugins.system_memory"
    version = "0.1.0"
    layer = "system"
    provides = [Services.SYSTEM_MEMORY]
    inject = []
    description = "系统记忆（第1层）：启动时从文件全量加载身份/边界叙事，作各阶段 system 提示静态底座"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("system_memory") or {}
        # 外部目录配置 → 完全覆盖默认目录
        text = load_system_memory(cfg.get("dir"))
        self._text = text
        self._dir = cfg.get("dir")
        ctx.provide(Services.SYSTEM_MEMORY, text)

    async def status(self, ctx) -> dict:
        """运行态：加载了多少叙事、来自哪个目录。"""
        text = getattr(self, "_text", "")
        return {
            "chars": len(text or ""),
            "empty": not (text or "").strip(),
            "dir": getattr(self, "_dir", None) or "(默认目录)",
        }


__all__ = ["SystemMemory", "load_system_memory", "DEFAULT_DIR"]
