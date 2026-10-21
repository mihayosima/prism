"""agent.plugins.system_memory.loader —— 系统记忆内容加载。

设计：
- 存储：插件目录下的 Markdown 文件；
 支持配置 ``agent.system_memory.dir`` 指向外部目录，完全覆盖默认目录。
- 加载：仅启动加载，不做热重载；未来若需刷新，由显式命令触发。
- 排序：显式按文件名排序，扁平、不递归子目录。
- 语义：文件是「身份 / 边界」的声明层；越权强制仍在 guard，本模块不碰。
 文件不枚举具体工具——工具由 ``ctx.tasks.catalog()`` 实时注入，防漂移。

零特权：纯文件 IO，不依赖 ctx / db / llm。
"""

from __future__ import annotations

from pathlib import Path

# 默认目录 = 插件目录自身
DEFAULT_DIR = Path(__file__).resolve().parent


def load_system_memory(directory: str | None = None) -> str:
    """从目录加载系统记忆，拼接为单一文本。

 - 仅顶层 ``*.md``；
 - 按文件名显式排序；
 - 目录不存在 / 无 md 文件 → 返回空串。
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


__all__ = ["load_system_memory", "DEFAULT_DIR"]
