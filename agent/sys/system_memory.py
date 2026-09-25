"""agent.sys.system_memory —— 系统记忆（第 1 层：永久、文件、启动全量注入）。

设计（用户敲定）：
- 存储：目录下的 Markdown 文件（默认 ``agent/system_memory/``，随代码 git 跟踪）；
  支持配置 ``agent.system_memory.dir`` 指向外部目录，**完全覆盖**默认目录（非合并）。
- 加载：**仅启动加载**（boot 时读入并缓存），不做热重载；未来若需刷新，由显式命令触发。
- 排序：**显式按文件名排序**（不依赖文件系统枚举顺序），**扁平**、不递归子目录。
- 语义：文件是「身份 / 边界」的**声明层**（人读 + LLM 读）；越权强制仍在 guard，本模块不碰。
  文件不枚举具体工具——工具由 ``ctx.tasks.catalog()`` 实时注入，防漂移。

零特权：纯文件 IO，不依赖 ctx / db / llm。
"""

from __future__ import annotations

from pathlib import Path

from agent.api import Services, plugin

# 默认目录：agent/system_memory/（本文件在 agent/sys/，parent.parent = agent/）
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "system_memory"


def load_system_memory(directory: str | None = None) -> str:
    """从目录加载系统记忆，拼接为单一文本。

    - 仅顶层 ``*.md``（扁平、不递归）；
    - 按文件名**显式排序**（确定性，跨平台一致）；
    - 目录不存在 / 无 md 文件 → 返回空串（优雅降级）。
    """
    d = Path(directory) if directory else DEFAULT_DIR
    if not d.is_dir():
        return ""
    parts: list[str] = []
    for fp in sorted(d.glob("*.md")):        # 显式排序，扁平
        if fp.is_file():
            text = fp.read_text(encoding="utf-8").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts)


@plugin
class SystemMemory:
    """ctx 服务 ``system_memory``：启动时把系统记忆文本全量注入，作为各阶段 system 提示静态底座。"""

    name = "agent.sys.system_memory"
    version = "0.1.0"
    layer = "system"
    provides = [Services.SYSTEM_MEMORY]
    inject = []
    description = "系统记忆：启动从文件全量加载身份/边界叙事，作各阶段 system 提示静态底座"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("system_memory") or {}
        # 外部目录配置 → 完全覆盖默认目录（非合并）
        text = load_system_memory(cfg.get("dir"))
        ctx.provide(Services.SYSTEM_MEMORY, text)


__all__ = ["SystemMemory", "load_system_memory", "DEFAULT_DIR"]
