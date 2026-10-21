"""子 Agent 角色定义。

设计稿 v0.3：角色是安全下界——``tools`` 白名单语义为「交集」，
只能比父可见集更窄。

MVP 不读 YAML，只内置 ``default`` / ``explorer`` / ``worker`` 三档；
三者差异化以 ``system_note`` 体现，真正的工具白名单收窄
在阶段 3 经 ``agent.react.subagents`` YAML 表达。
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
from typing import Iterable

logger = logging.getLogger("agent.subagent.roles")

# 阶段 3：子永远是叶子节点 —— 委派工具绝不下传给子循环。
# 用「排除项」表达而非枚举「其余全部」：后者会随工具目录变化静默失效。
LEAF_TOOL = "agent.delegate"

# 角色可覆盖的运行时护栏与上下文治理字段。
# 只允许收窄子循环，不允许超过父级默认 ⇒ 角色不能成为放大面。
MAX_ROLE_STEPS = 32
MAX_ROLE_DEADLINE_S = 600.0
# ``context`` 允许的字段 = ReactConfig / 治理器上真实存在的上下文治理旋钮。
# 未在治理器实例上找到同名字段 ⇒ 诚实跳过，绝不静默塞进对象。
CONTEXT_KEYS = ("react_keep_recent", "act_fold_tokens", "history_keep",
                "clip_max_tokens", "clip_head_tokens", "clip_tail_tokens")

# 角色文件名合法字符集：与会话键净化同款口径，防路径穿越/注入。
# 首尾必须落在 [A-Za-z0-9_]，中间才允许 . 与 - —— 否则 "..evil" / ".hidden"
# 这类以点开头的名字会被 {1,64} 的通配点号放行。
_ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}[A-Za-z0-9_]$|^[A-Za-z0-9_]$")

# 内置角色。``tools`` 为 ``None`` 时表示「不额外收窄」；
# 为具体名单时表示在该角色下只允许这些工具名/域。
BUILTIN_ROLES: dict[str, dict] = {
    "default": {
        "description": "通用子 Agent：继承父的全部可见工具（不额外收窄）；适合任何独立子任务。",
        "tools": None,
        "system_note": "",
    },
    "explorer": {
        "description": (
            "调研型子 Agent：面向检索/读取场景。MVP 仅以提示约束其只读倾向；"
            "真正的只读硬约束（仅放行 risk=read 工具）由父会话 plan 档位或"
            "阶段 3 角色白名单落实，本档不擅自放宽。"
        ),
        "tools": None,
        "system_note": (
            "你是调研子 Agent：只做检索与读取，归纳事实、给出依据；"
            "不要执行任何写操作，也不要臆造来源。"
        ),
    },
    "worker": {
        "description": (
            "执行/审查型子 Agent：在 default 基础上可承载写操作，并对结果做自检。"
        ),
        "tools": None,
        "system_note": (
            "你是执行子 Agent：按指令完成任务，完成后对结果做一遍自检"
            "（正确性/边界/明显错误），在答复中说明自检结论。"
        ),
    },
}


class Role:
    """解析后的角色。

     阶段 3 增补 ``max_steps`` / ``deadline_s`` / ``context``：前两者进 ``cfg_overrides``，
     ``context`` 供「子 Agent 独立上下文治理」使用。
     """

    __slots__ = ("name", "description", "tools", "system_note",
                 "max_steps", "deadline_s", "context")

    def __init__(self, name: str, description: str, tools, system_note: str,
                 max_steps: int | None = None, deadline_s: float | None = None,
                 context: dict | None = None) -> None:
        self.name = name
        self.description = description
        self.tools = tools            # list[str] | None
        self.system_note = system_note
        self.max_steps = max_steps
        self.deadline_s = deadline_s
        self.context = dict(context or {})


def get_role(name: str, extra: dict | None = None) -> Role:
    """按名取角色；未知名回落 ``default``，诚实降级，不抛异常，避免父循环崩溃。

     ``extra`` 是经 :func:`load_role_files` 校验过的目录式角色表；
     文件角色不覆盖内置角色。
     """
    name = (name or "default").strip() or "default"
    spec = BUILTIN_ROLES.get(name)
    if spec is None:
        spec = (extra or {}).get(name)
    if spec is None:
        spec = BUILTIN_ROLES["default"]
        name = "default"
    return Role(name=name, description=spec.get("description", ""),
                tools=spec.get("tools"), system_note=spec.get("system_note", "") or "",
                max_steps=spec.get("max_steps"), deadline_s=spec.get("deadline_s"),
                context=spec.get("context") or {})


def list_roles(extra: dict | None = None) -> list[str]:
    """已注册角色名。"""
    names = set(BUILTIN_ROLES) | set(extra or {})
    return sorted(names)


# 阶段 2：目录式角色文件

def roles_dir(data_dir: str | pathlib.Path) -> pathlib.Path:
    """角色目录 = ``<data_dir>/agent_roles``。"""
    return pathlib.Path(data_dir) / "agent_roles"


def validate_role_spec(name: str, spec: dict,
                       catalog_names: set[str], catalog_domains: set[str]) -> dict:
    """校验单个角色定义，非法 ⇒ 抛 ``ValueError``。

     安全边界：工具名/域名必须在当前工具目录内。
     为什么「未知工具」要整份拒绝而不是静默丢弃：白名单是安全下界，
     一处笔误若被静默忽略，角色就会静默变成「不收窄」——表面正常、实际越权。
     故宁可让该角色不可用，也不给一个语义被悄悄改写的角色。
     """
    if not isinstance(spec, dict):
        raise ValueError(f"角色 {name!r}：文件内容必须是对象")
    unknown_keys = set(spec) - {"description", "tools", "system_note",
                                "max_steps", "deadline_s", "context"}
    if unknown_keys:
        raise ValueError(f"角色 {name!r}：出现未知字段 {sorted(unknown_keys)}")
    tools = spec.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ValueError(f"角色 {name!r}：tools 必须是字符串数组或 null")
        bad = [t for t in tools if t != "*"
               and t not in catalog_names and t not in catalog_domains]
        if bad:
            raise ValueError(f"角色 {name!r}：引用了不存在的工具/域 {sorted(set(bad))}")
        tools = [t for t in tools if t] or None
    note = spec.get("system_note") or ""
    if not isinstance(note, str):
        raise ValueError(f"角色 {name!r}：system_note 必须是字符串")
    desc = spec.get("description") or ""
    if not isinstance(desc, str):
        raise ValueError(f"角色 {name!r}：description 必须是字符串")
    out = {"description": desc, "tools": tools, "system_note": note}
    # ---- 阶段 3：可覆盖的护栏 ----
    ms = spec.get("max_steps")
    if ms is not None:
        if not isinstance(ms, int) or isinstance(ms, bool) or not (1 <= ms <= MAX_ROLE_STEPS):
            raise ValueError(
                f"角色 {name!r}：max_steps 必须是 1..{MAX_ROLE_STEPS} 的整数")
        out["max_steps"] = ms
    dl = spec.get("deadline_s")
    if dl is not None:
        if not isinstance(dl, (int, float)) or isinstance(dl, bool) \
                or not (1.0 <= float(dl) <= MAX_ROLE_DEADLINE_S):
            raise ValueError(
                f"角色 {name!r}：deadline_s 必须是 1..{MAX_ROLE_DEADLINE_S} 的数字")
        out["deadline_s"] = float(dl)
    cx = spec.get("context")
    if cx is not None:
        if not isinstance(cx, dict):
            raise ValueError(f"角色 {name!r}：context 必须是对象")
        bad = sorted(set(cx) - set(CONTEXT_KEYS))
        if bad:
            raise ValueError(f"角色 {name!r}：context 出现未知字段 {bad}"
                             f"（允许：{list(CONTEXT_KEYS)}）")
        for k, v in cx.items():
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise ValueError(f"角色 {name!r}：context.{k} 必须是非负整数")
        # 空 dict ⇒ 不写入
        if cx:
            out["context"] = dict(cx)
    return out


def load_role_files(directory: str | pathlib.Path, catalog: list[dict]) -> dict:
    """扫描角色目录，返回校验通过的角色表；非法文件跳过并记账。

     - 目录不存在 ⇒ 返回空表；
     - 单个文件坏 ⇒ 跳过该文件，不影响其他角色与内置角色；
     - 角色名 = 文件名主干，须匹配 ``[A-Za-z0-9_.-]{1,64}``；
     - 内置角色优先，文件角色不覆盖内置。

     每次 ``delegate`` 调用时扫目录：读几个小文件的 IO 相对「起一个 LLM 子循环」
     可忽略，换来「加角色不用改代码」。
     """
    names = {str(t.get("name") or "") for t in catalog or []}
    domains = {str(t.get("owner") or "") for t in catalog or [] if t.get("owner")}
    out: dict[str, dict] = {}
    d = pathlib.Path(directory)
    if not d.is_dir():
        return out
    for fp in sorted(d.glob("*.json")):
        stem = fp.stem
        if not _ROLE_NAME_RE.match(stem):
            logger.warning("跳过角色文件（非法角色名）：%s", fp.name)
            continue
        try:
            spec = json.loads(fp.read_text(encoding="utf-8"))
            out[stem] = validate_role_spec(stem, spec, names, domains)
        except Exception as exc:                      # 单文件坏不影响其他
            logger.warning("跳过角色文件 %s：%s", fp.name, exc)
    return out


def apply_leaf_rule(tools: list[str] | None) -> list[str]:
    """在白名单上叠加「子永远是叶子节点」：排除委派工具本身。

     为什么是「排除」而不是「枚举允许项」：子要继承父的宽工具面，否则角色内的
     具体白名单会随目录变化失效，唯一要拿掉的只有委派工具 ⇒ 排除语义最贴合。
     该函数只产出标识符，真正的过滤由 react 的
     ``_apply_whitelist`` / ``_split_whitelist_denied`` 执行 —— 提示层与执行层同口径。

     - ``None``⇒ ``["*", "-agent.delegate"]``；
     - 具体名单 ⇒ 去掉委派工具 + 追加排除项。
     """
    if tools is None:
        return ["*", f"-{LEAF_TOOL}"]
    kept = [t for t in tools if str(t) != LEAF_TOOL]
    return [*kept, f"-{LEAF_TOOL}"]


def combine_tools(role_tools: Iterable[str] | None,
                  param_tools: Iterable[str] | None) -> list[str] | None:
    """角色白名单与请求白名单取交集。

     语义：两层任一都只能再收窄，不能放宽。
     - 任一层为 ``None``/空 ⇒ 不收窄该层；
     - 含 ``"*"`` 的层 ⇒ 不额外收窄；
     - 两层皆为具体名单 ⇒ 取同时出现在两层者；
     - 两层具体名单无交集 ⇒ 抛 ``ValueError``。

     返回 ``None`` 表示「不收窄」，交由工厂再做父权限裁剪；返回具体列表即收窄结果。
     """
    rt = list(role_tools or [])
    pt = list(param_tools or [])
    if not rt:
        return pt or None
    if not pt:
        return rt or None
    sa, sb = set(rt), set(pt)
    if "*" in sa:
        return pt or None
    if "*" in sb:
        return rt or None
    common = [x for x in rt if x in sb]
    if not common:
        raise ValueError(
            f"角色工具白名单 {sorted(rt)} 与请求工具 {sorted(pt)} 无交集")
    return common
