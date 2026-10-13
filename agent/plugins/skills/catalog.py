"""agent.plugins.skills.catalog —— 技能目录（SkillsCatalog）。

react 经 ``ctx.get(Services.SKILLS)`` **反向读取**本目录，二者单向解耦：本模块
**不 import react / plugins.react**。

★ 2026-10-14 归位：本模块从 ``agent/core`` 移入 ``plugins/skills``。设计文档三层语义
（``agent内部架构-开发文档.md``）：``core/`` 零业务词，业务词只允许出现在 ``plugins/``
业务插件、``fixtures/`` 与领域文档；``plugins.memory``（layer=system）住插件层即先例。
此前沉入 core 的理由（"适配器子进程要构造 ⇒ 只能落 core"）是把插件层问题推给内核：
适配器已随桥搬进 ``plugins/skills_mcp/adapter.py``（包内相对导入本目录），run/ 传输层
改经目录自身的管理面 API（``add_fields`` / ``persist`` / ``reload_from_disk``）操作，
两端都不再需要 ``agent.api`` 导出技能类。内核机制（errors / permissions / tokens）
仍经 ``agent.api`` facade 取用，不碰 ``agent.core.*`` 私有路径。

暴露给 react 的契约面（react 只依赖这些签名）：
- ``meta_tools() -> list[dict]``        单 ``skill`` 工具定义（常驻，独立于 lazy_tools）
- ``handle_meta(task, call, ctx) -> (text, is_error)``
  - action="load"   ⇒ 取 content 写入 ``task.skill_docs``
  - action="invoke" ⇒ 调 ``Skill.run(ctx, **params)``
- ``index_block(max_chars) -> str``      紧凑索引（注入系统提示免费常驻）
- ``render_content(s) -> str``           正文（``{baseDir}`` 已替换为技能文件所在目录）

管理面（决策：增/删/查/改/启停；run/skills_routes 只经这些方法，不碰 Skill 类）：
- ``add_fields(...) -> dict``            字段式新增（内部构造 Skill，返回卡片）
- ``persist(s)``                         把内存中的技能写回磁盘
- ``reload_from_disk(name) -> Skill``    从磁盘重载（目录式/扁平式均可）
- ``add / remove / set_enabled`` 支持 ``persist`` 写盘（``data/skills/*.md`` + ``skills_state.json``）
- 同名冲突抛 ``ServiceConflictError``（不静默覆盖）
- 插件来源技能不可经管理面移除（只可停用插件）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from agent.api import AgentError, ServiceConflictError, clip_text, current_permission_mode
from .loader import (delete_skill_file, discover_skills, load_state,
                     parse_skill_file, save_state, write_skill_file)
from .model import Skill

logger = logging.getLogger(__name__)


def _slim(text: str, limit: int = 120) -> str:
    """索引摘要：截断首行（仿 react 的 _slim_description）。"""
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


class SkillsCatalog:
    """技能注册表 + 文件发现 + 管理面（持久化到 data/skills/）。"""

    def __init__(self, data_dir: str | Path, *,
                 skill_docs_max_tokens: int = 6000):
        self._data_dir = Path(data_dir)
        self._skills: dict[str, Skill] = {}
        self._disabled: set[str] = set()
        self._skill_docs_max_tokens = skill_docs_max_tokens

    # ---- 发现 / 装配 ----
    def load_from_disk(self) -> int:
        """扫描 data/skills/*.md 并注册；与已注册（插件）技能同名 ⇒ 抛 ServiceConflictError。"""
        self._disabled = load_state(self._data_dir)
        n = 0
        for s in discover_skills(self._data_dir):
            if s.name in self._disabled:
                s.enabled = False
            self.register(s)          # 同名 ⇒ ServiceConflictError（决策 4：报错不覆盖）
            n += 1
        return n

    def register(self, s: Skill) -> None:
        if s.name in self._skills:
            raise ServiceConflictError(s.name, "skills.catalog")
        self._skills[s.name] = s

    def unregister(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(name)
        return self._skills.pop(name)

    # ---- 查询 ----
    def catalog(self) -> list[dict]:
        return [self._card(s) for s in self._skills.values()]

    def _card(self, s: Skill) -> dict:
        return {"name": s.name, "description": s.description, "risk": s.risk,
                "owner": s.owner, "tags": list(s.tags), "version": s.version,
                "has_run": s.has_run, "enabled": s.enabled, "source": s.source,
                "content": s.content}

    def search(self, keyword: str) -> list[dict]:
        kw = (keyword or "").strip().lower()
        if not kw:
            return self.catalog()
        return [self._card(s) for s in self._skills.values()
                if kw in f"{s.name} {s.description} {' '.join(s.tags)}".lower()]

    def load(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def render_content(self, s: Skill) -> str:
        """返回技能正文，并把 ``{baseDir}`` 替换为该技能文件所在目录。

        Agent Skills 标准约定：SKILL.md 用 ``{baseDir}`` 指代自身目录（常用于指向捆绑的
        ``scripts/``），加载时**必须**替换为真实路径，否则模型/调用方拿到的是无效占位。
        无占位符则原样返回（对既有技能是纯 no-op）。
        """
        text = s.content or ""
        if "{baseDir}" in text:
            text = text.replace("{baseDir}", s.basedir or "")
        return text

    def invoke(self, name: str, ctx, **params) -> dict:
        s = self._skills.get(name)
        if s is None:
            return {"ok": False, "error": f"未找到技能 {name!r}"}
        if not s.has_run:
            return {"ok": False, "error": f"技能 {name!r} 无代码入口（仅说明型）"}
        try:
            return {"ok": True, "result": s.run(ctx, **params)}
        except Exception as exc:                         # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---- react 反向读取契约 ----
    def meta_tools(self) -> list[dict]:
        """单 ``skill`` 工具定义（常驻可见，独立于 lazy_tools 开关）。"""
        return [{
            "name": "skill",
            "description": ("技能工具：action=load 把技能说明注入上下文（之后模型按说明执行）；"
                           "action=invoke 执行带代码入口的技能。参数 action/name/params。"
                           "勿把技能当普通函数直接调用——先用 skill(action=load) 读说明。"),
            "params": {
                "action": {"type": "str"},
                "name": {"type": "str"},
                "params": {"type": "str"},
            },
            "required": ["action", "name"],
            "risk": "read",
            "owner": "plugins.skills",
        }]

    def handle_meta(self, task, call, ctx) -> tuple[str, bool]:
        """处理发往 ``skill`` 工具的调用；返回 (文本, 是否错误)。

        react 侧据此包成 ToolResult（避免 sys 层反向依赖 plugins 层）。
        """
        args = getattr(call, "arguments", None)
        if not isinstance(args, dict):
            args = {}
        action = str(args.get("action") or "").strip().lower()
        name = str(args.get("name") or "").strip()
        if action == "load":
            s = self.load(name)
            if s is None:
                return (f"未找到技能 {name!r}", True)
            doc = self.render_content(s) or f"（技能 {name!r} 暂无说明内容）"
            doc, _ = clip_text(doc, self._skill_docs_max_tokens)
            task.skill_docs.append(doc)
            return (f"已加载技能 {name!r} 的说明，将在下一步注入上下文。", False)
        if action == "invoke":
            s = self.load(name)
            if s is None:
                return (f"未找到技能 {name!r}", True)
            if s.risk != "read" and current_permission_mode(ctx, "") == "plan":
                return (f"plan 模式下不允许调用写类技能 {name!r}", True)
            if not s.has_run:
                return (f"技能 {name!r} 无代码入口（仅说明型）", True)
            raw = args.get("params") or {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw) or {}
                except Exception:                          # noqa: BLE001
                    raw = {}
            try:
                res = s.run(ctx, **raw)
            except Exception as exc:                      # noqa: BLE001
                return (f"技能执行失败：{type(exc).__name__}: {exc}", True)
            text = json.dumps(res, ensure_ascii=False)
            text, _ = clip_text(text, self._skill_docs_max_tokens)
            return (text, False)
        return (f"未知 action={action!r}（支持 load/invoke）", True)

    def index_block(self, max_chars: int = 8000) -> str:
        """紧凑索引块（注入系统提示，免费常驻；超预算压缩/省略）。"""
        lines = []
        for s in self._skills.values():
            if not s.enabled:
                continue
            tag = "写" if s.risk != "read" else "读"
            lines.append(f"- {s.name}（{tag}）：{_slim(s.description)}")
        if not lines:
            return ""
        body = "\n".join(lines)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n…(技能过多已截断，请用 skill 工具按需加载)"
        return ("## 可用技能（按需调用 skill 工具加载说明，勿当普通函数直接调用）\n" + body)

    # ---- 管理面（持久化） ----
    def add_fields(self, *, name: str, description: str = "", content: str = "",
                   risk: str = "read", tags: list[str] | None = None,
                   owner: str = "admin", source: str = "file",
                   persist: bool = False) -> dict:
        """字段式新增（传输层管理面用）：内部构造 Skill、注册、按需写盘，返回卡片。

        ★ 归位（2026-10-14）后 ``run/skills_routes`` 不再 import Skill 类——run/ 层
        只准 import ``agent.api``，而 api 不再导出技能类 ⇒ 新增改经本方法落库。
        """
        s = Skill(name=name, description=description, content=content, risk=risk,
                  tags=list(tags or []), owner=owner, source=source)
        self.add(s, persist=persist)
        return self._card(s)

    def persist(self, s: Skill) -> Path:
        """把内存中的技能写回磁盘（管理面编辑后调用；目录式优先写回原 ``SKILL.md``）。"""
        return write_skill_file(self._data_dir, s)

    def reload_from_disk(self, name: str) -> Skill:
        """从磁盘重载指定技能（扁平式/目录式均可），返回重载后的 Skill。

        ★ 归位（2026-10-14）后解析/重注册逻辑内聚到目录自身，传输层不再碰
        ``parse_skill_file``。布局兼容：``<name>.md`` 与 ``<name>/SKILL.md`` 都认
        （旧传输层实现只查扁平路径，目录式技能实为 404——本次顺带修正）。
        找不到入口文件抛 ``FileNotFoundError``、解析失败抛 ``ValueError``，
        由传输层映射为 4xx：重载是显式管理动作，失败必须可见，不吞。
        """
        d = Path(self._data_dir) / "skills"
        safe = str(name).replace("/", "_").replace("\\", "_")
        for p, default in ((d / f"{safe}.md", None), (d / safe / "SKILL.md", name)):
            if p.exists():
                ns = parse_skill_file(p, default_name=default)
                ns.enabled = name not in self._disabled
                self.unregister(name)
                self.register(ns)
                return ns
        raise FileNotFoundError(
            f"技能文件不存在：{d / f'{safe}.md'} 或 {d / safe / 'SKILL.md'}")

    def add(self, skill: Skill, *, persist: bool = False) -> None:
        if skill.name in self._skills:
            raise ServiceConflictError(skill.name, "skills.catalog")
        self.register(skill)
        if persist:
            write_skill_file(self._data_dir, skill)

    def remove(self, name: str, *, persist: bool = False) -> None:
        s = self._skills.get(name)
        if s is None:
            raise KeyError(name)
        if s.source == "plugin":
            raise AgentError(
                f"技能 {name!r} 来自插件，不可经管理面移除（请停用对应插件）")
        self.unregister(name)
        if persist:
            delete_skill_file(self._data_dir, name)

    def set_enabled(self, name: str, enabled: bool, *, persist: bool = False) -> None:
        s = self._skills.get(name)
        if s is None:
            raise KeyError(name)
        s.enabled = bool(enabled)
        if not enabled:
            self._disabled.add(name)
        else:
            self._disabled.discard(name)
        if persist:
            save_state(self._data_dir, self._disabled)


__all__ = ["SkillsCatalog"]
