"""agent.plugins.skills.model —— Skill 数据模型。

Skill = 一份「遇到 X 类任务时按 Y 步骤做」的说明+ 可选代码
入口。与 tool/MCP 正交分工：skill 描述「怎么做」，tool 提供
「能做的具体动作」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Skill:
    """单个技能定义。"""

    name: str                        # 唯一键，如 "skill.docker_build"
    description: str                 # 触发摘要
    content: str = ""                # markdown 说明
    owner: str = ""                  # 来源域
    risk: str = "read"               # "read" | "write"；影响 plan 模式裁剪
    tags: list[str] = field(default_factory=list)
    version: str = "0.1.0"
    # : B3：目录式技能捆绑的资源相对路径，
    # : 由 loader 扫描填入，供 skill_view 一并返回，深度 2、最多 50，避免膨胀。
    resources: list[str] = field(default_factory=list)
    run: Callable[..., Any] | None = None   # 可选执行入口 run(ctx, **params) -> dict
    enabled: bool = True
    source: str = "file"             # "file" | "plugin"
    # : 技能文件所在目录。
    # : 文件型由 ``parse_skill_file`` 填 ``str(p.parent)``；插件型为空串。
    basedir: str = ""

    @property
    def has_run(self) -> bool:
        return self.run is not None


def skill(name: str, description: str, content: str = "", *,
          risk: str = "read", tags: list[str] | None = None,
          version: str = "0.1.0", run: Callable[..., Any] | None = None,
          owner: str = "", source: str = "file", basedir: str = "",
          resources: list[str] | None = None) -> Skill:
    """构造一个技能。

 默认 ``source="file"``：绝大多数经工厂创建的技能是文件型；插件来源技能由调用方
 显式传 ``source="plugin"``。否则文件型技能会被
 误判为插件来源、进而无法经管理面增删。
 """
    return Skill(name=name, description=description, content=content,
                 owner=owner, risk=risk, tags=tags or [], version=version,
                 run=run, source=source, basedir=basedir,
                 resources=resources or [])


__all__ = ["Skill", "skill"]
