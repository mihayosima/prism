"""agent.core.skills_loader —— 文件型技能发现与持久化（data/skills/*.md）。

frontmatter 为极简 YAML（``key: value`` + ``tags: [a, b]``），避免引入 PyYAML 依赖。
仅扫描 ``data/skills/``（用户决策：不过度扫各插件子目录，省启动耗时）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent.core.skill import Skill

logger = logging.getLogger(__name__)


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """拆分 ``---\\n...\\n---\\n...`` → (frontmatter, body)。无 frontmatter 则 ("", raw)。"""
    if not raw.startswith("---"):
        return "", raw
    lines = raw.split("\n")
    if lines[0].strip() != "---":
        return "", raw
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return "", raw


def _parse_meta(meta: str) -> dict:
    """解析极简 YAML frontmatter（**支持多行块**，无 PyYAML 依赖）。

    支持三种形态：

    - ``key: value``            单行标量
    - ``key: [a, b]``           行内列表（仅 ``tags`` 走此解析）
    - ``key: |`` / ``key: >``   YAML 块标量：其后**缩进更深**的行整体作为值
      （``|`` 保留换行、``>`` 折叠为空格连接）

    ★ 块标量是为 Agent Skills 标准补的：标准里 ``description`` 常写成多行触发说明
    （``Use when… / Triggers include…``），是模型做 Match 的**唯一依据**；若被截断成
    首行，命中率会明显下降。
    """
    out: dict = {}
    lines = meta.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        i += 1
        if not line.strip() or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v in ("|", ">"):
            fold = (v == ">")
            base_indent = len(raw) - len(raw.lstrip())
            block: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip():
                    block.append("")
                    i += 1
                    continue
                if (len(nxt) - len(nxt.lstrip())) <= base_indent:
                    break                       # 缩进回退 ⇒ 块结束
                block.append(nxt.strip())
                i += 1
            while block and not block[-1]:
                block.pop()                     # 去尾部空行
            out[k] = (" " if fold else "\n").join(block)
            continue
        if k == "tags":
            v = [x.strip() for x in v.strip("[]").split(",") if x.strip()]
        out[k] = v
    return out


def _safe_name(name: str) -> str:
    """名称 → 安全文件名（防路径穿越；目录分隔符一律转 ``_``）。"""
    return str(name).replace("/", "_").replace("\\", "_")


def _render(skill: Skill) -> str:
    """Skill → ``SKILL.md`` 全文（description 含换行时改用 ``|`` 块写回，避免丢行）。"""
    desc = skill.description or ""
    if "\n" in desc:
        block = "\n".join("  " + ln for ln in desc.split("\n"))
        desc_line = "description: |\n" + block
    else:
        desc_line = f"description: {desc}"
    tags = "[" + ", ".join(skill.tags) + "]" if skill.tags else "[]"
    return (f"---\nname: {skill.name}\n{desc_line}\n"
            f"risk: {skill.risk}\nversion: {skill.version}\ntags: {tags}\n---\n\n"
            f"{skill.content}\n")


def parse_skill_file(path: str | Path, default_name: str | None = None) -> Skill:
    """解析单个技能文件为 Skill（source="file"）。

    ★ Agent Skills 标准对齐（名称）：名称 = frontmatter 的 ``name``，缺省则取
    **所在目录名**（目录式 ``<dir>/SKILL.md``）或文件名（扁平式 ``*.md``）。
    不再强制加 ``skill.`` 前缀——社区/Claude Code 技能用的是文件夹名
    （如 ``docker-build``、``duckduckgo-websearch``），加前缀会让它们改名、破坏
    技能正文里对自身名称的引用。显式写了 ``skill.xxx`` 的既有技能**照原样保留**。

    缺少 ``---`` 起始的 frontmatter 视为非法，抛 ``ValueError``——``discover_skills``
    会捕获并跳过，避免把随手丢进 ``data/skills/`` 的普通 markdown 当成技能加载。
    """
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    if not raw.lstrip().startswith("---"):
        raise ValueError(f"技能文件 {p.name} 缺少 --- 起始的 frontmatter")
    meta, body = _split_frontmatter(raw)
    if not meta.strip():
        raise ValueError(f"技能文件 {p.name} 的 frontmatter 为空或缺少闭合 ---")
    fm = _parse_meta(meta)
    name = fm.get("name") or default_name or p.stem
    return Skill(
        name=name,
        description=fm.get("description", ""),
        content=body.strip(),
        owner=f"file:{p}",
        risk=str(fm.get("risk", "read")),
        tags=list(fm.get("tags", [])),
        version=str(fm.get("version", "0.1.0")),
        run=None,
        enabled=True,
        source="file",
        basedir=str(p.parent),
    )


def discover_skills(data_dir: str | Path) -> list[Skill]:
    """扫描技能（解析失败告警跳过，不拖垮启动）。

    支持两种布局，**目录式优先**：

    - 目录式（Agent Skills 标准）：``<data_dir>/skills/<name>/SKILL.md``
      ——可同目录捆绑 ``scripts/ reference/ templates/ examples/ assets/``，
      名称取目录名，``basedir`` 指向该目录（供正文 ``{baseDir}`` 占位替换）。
    - 扁平式（本仓既有）：``<data_dir>/skills/*.md``——名称取文件名。

    ★ 重名处理：目录式先扫、同名者**告警跳过**（保留先到的）。刻意不让重名把容器
    装配拖崩——两种布局并存时同名是很容易发生的运维失误，比起服务起不来，
    告警+跳过更可取；``SkillsCatalog.register`` 的**显式**同名冲突仍照旧抛
    ``ServiceConflictError``（"不静默覆盖"的决策未变）。
    """
    d = Path(data_dir) / "skills"
    if not d.is_dir():
        return []
    out: list[Skill] = []
    seen: set[str] = set()

    def _try(p: Path, default_name: str | None) -> Skill | None:
        try:
            s = parse_skill_file(p, default_name=default_name)
        except Exception as exc:                      # noqa: BLE001
            logger.warning("技能文件解析失败 %s: %s", p, exc)
            return None
        if s.name in seen:
            logger.warning("技能重名 %r（已被同名技能占用），跳过 %s", s.name, p)
            return None
        seen.add(s.name)
        return s

    for p in sorted(d.glob("*/SKILL.md")):            # 目录式（标准）
        s = _try(p, p.parent.name)
        if s is not None:
            out.append(s)
    for p in sorted(d.glob("*.md")):                  # 扁平式（兼容）
        s = _try(p, None)
        if s is not None:
            out.append(s)
    return out


def write_skill_file(data_dir: str | Path, skill: Skill) -> Path:
    """写入技能（persist）。

    ★ 目录式技能**写回原 ``SKILL.md``**：管理面编辑一个来源于目录的技能时，若按
    扁平路径另存，就会同时存在 ``<name>/SKILL.md`` 与 ``<name>.md`` 两份 ⇒ 下次扫描
    重名冲突（且用户改的其实是新那份）。故优先写回原目录。
    """
    d = Path(data_dir) / "skills"
    if skill.basedir:
        bd = Path(skill.basedir)
        if bd.is_dir() and (bd / "SKILL.md").exists():
            p = bd / "SKILL.md"
            p.write_text(_render(skill), encoding="utf-8")
            return p
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{_safe_name(skill.name)}.md"
    p.write_text(_render(skill), encoding="utf-8")
    return p


def delete_skill_file(data_dir: str | Path, name: str) -> bool:
    """删除技能文件（扁平 ``<name>.md`` 或目录式 ``<name>/SKILL.md``）；成功返回 True。

    只删入口文件，不动同目录捆绑的 ``scripts/`` 等资源（删除是高危操作，保留资源
    便于误删后恢复）。
    """
    d = Path(data_dir) / "skills"
    safe = _safe_name(name)
    for p in (d / f"{safe}.md", d / safe / "SKILL.md"):
        if p.exists():
            p.unlink()
            return True
    return False


def _state_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "skills_state.json"


def load_state(data_dir: str | Path) -> set[str]:
    """读 disabled 集合（缺文件/损坏回落空集）。"""
    p = _state_path(data_dir)
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        return set(data.get("disabled", []))
    except Exception:                                  # noqa: BLE001
        return set()


def save_state(data_dir: str | Path, disabled: set[str]) -> None:
    """写 disabled 集合。"""
    p = _state_path(data_dir)
    p.write_text(json.dumps({"disabled": sorted(disabled)}, ensure_ascii=False, indent=2),
                 encoding="utf-8")


__all__ = ["discover_skills", "parse_skill_file", "write_skill_file",
           "delete_skill_file", "load_state", "save_state"]
