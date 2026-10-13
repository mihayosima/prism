"""agent.plugins.skills.model —— Skill 数据模型（技能插件层）。

Skill = 一份「遇到 X 类任务时按 Y 步骤做」的说明（``content``，给模型看）+ 可选代码
入口（``run``，给编排器调）。与 tool/MCP 正交分工：skill 描述「怎么做」，tool 提供
「能做的具体动作」。

★ 2026-10-14 归位：本模块随 Skill 插件系统从 ``agent/core`` 移入 ``plugins/skills``。
设计文档三层语义——``core/`` 零业务词，业务词只允许出现在 ``plugins/`` 业务插件、
``fixtures/`` 与领域文档（``plugins.memory`` 即 system 层组件住插件层的先例）。
此前的反向推理（"适配器子进程要构造 ⇒ 只能落 core"）让内核迁就了特性，已纠正：
适配器随桥驻插件层（``plugins/skills_mcp/adapter.py``），不再需要 api 导出本类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Skill:
    """单个技能定义（文件型只有 ``content``；插件型可再带 ``run``）。"""

    name: str                        # 唯一键，如 "skill.docker_build"
    description: str                 # 触发摘要（索引块 / 列表展示，≤120 字首行）
    content: str = ""                # markdown 说明（注入上下文；纯文本 ⇒ 400 安全）
    owner: str = ""                  # 来源域（插件 id 或 "file:<path>"）
    risk: str = "read"               # "read" | "write"；影响 plan 模式裁剪
    tags: list[str] = field(default_factory=list)
    version: str = "0.1.0"
    run: Callable[..., Any] | None = None   # 可选执行入口 run(ctx, **params) -> dict
    enabled: bool = True
    source: str = "file"             # "file" | "plugin"
    #: 技能文件所在目录（Agent Skills 标准的 ``{baseDir}`` 占位替换用）。
    #: 文件型由 ``parse_skill_file`` 填 ``str(p.parent)``；插件型（无磁盘落点）为空串。
    basedir: str = ""

    @property
    def has_run(self) -> bool:
        return self.run is not None


def skill(name: str, description: str, content: str = "", *,
          risk: str = "read", tags: list[str] | None = None,
          version: str = "0.1.0", run: Callable[..., Any] | None = None,
          owner: str = "", source: str = "file", basedir: str = "") -> Skill:
    """构造一个技能。

    默认 ``source="file"``：绝大多数经工厂创建的技能是文件型；插件来源技能由调用方
    显式传 ``source="plugin"``（如本仓测试与未来插件作者）。否则文件型技能会被
    误判为插件来源、进而无法经管理面增删（见 SkillsCatalog.remove 守卫）。
    """
    return Skill(name=name, description=description, content=content,
                 owner=owner, risk=risk, tags=tags or [], version=version,
                 run=run, source=source, basedir=basedir)


__all__ = ["Skill", "skill"]
