"""插件包：skills —— 技能目录（系统组件 · 目录插件形态 · opt-in）。

- 提供 ``Services.SKILLS``（``SkillsCatalog`` 实例），react 经 ``ctx.get`` 反向读取，单向解耦。
- 文件型技能从 ``<data_dir>/skills/*.md`` 发现（仅此一处，不过度扫描）。
- 本插件**不 import react**，react 也**不 import 本插件**——纯经 Service 契约相遇。
- 停用后 ``ctx.has(Services.SKILLS)`` 为 False，react 回落「无技能」分支，零劣化。
"""

from __future__ import annotations

import logging
from pathlib import Path

from agent.api import DEFAULT_DB_PATH, Services, SkillsCatalog, plugin

logger = logging.getLogger(__name__)


def _resolve_data_dir(ctx) -> str:
    """数据目录 = db_path 父目录（与 subagent 的 _data_dir 同口径）。"""
    db_path = (ctx.config or {}).get("db_path") or DEFAULT_DB_PATH
    return str(Path(db_path).parent)


@plugin
class Skills:
    """ctx.skills（经 Services.SKILLS） —— 技能目录。"""

    name = "plugins.skills"
    version = "0.1.0"
    layer = "system"
    provides = [Services.SKILLS]
    inject = []
    tables = ()
    description = "技能目录（说明型 + 可选代码入口；react 经 ctx.get 反向读取）"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("skills") or {}
        data_dir = _resolve_data_dir(ctx)
        catalog = SkillsCatalog(data_dir)
        try:
            n = catalog.load_from_disk()
            logger.info("skills：发现并注册 %d 个文件技能（data_dir=%s）", n, data_dir)
        except Exception as exc:                      # noqa: BLE001
            # 同名冲突（插件/文件同名）按决策报错；其余异常不拖垮容器，降级为无技能
            logger.error("skills：加载文件技能失败：%s", exc)
            raise
        ctx.provide(Services.SKILLS, catalog)

    async def status(self, ctx) -> dict:
        cat = ctx.get(Services.SKILLS)
        if cat is None:
            return {"skills": 0}
        return {"skills": len(cat.catalog())}


__all__ = ["Skills"]
