"""agent.plugins.skills.adapter —— 技能 MCP 服务器。

把本仓 ``Services.SKILLS`` 的技能目录以 MCP 工具形式对外暴露，供子 agent / 外部 MCP
客户端消费。设计要点：

- 只读 + 可选执行：常驻暴露 ``skill_list`` / ``skill_view`` 两个只读工具；配置
  ``SKILLS_ALLOW_EXEC=1``，桥默认开，策略 A时额外暴露 Follow 三件套，在宿主环境
  直接操作电脑。执行不经内核 ``Skill.run``。
- 零第三方依赖：``core/mcp_client.py`` 是 stdlib 的 JSON-RPC stdio 客户端，本模块是
  它的对端——同样用 stdlib 手写服务端。实测本机 ``fastmcp`` / ``mcp`` 均未安装且无 uv，
  故不走框架路线。
- 子进程无 ctx：以 ``python -m agent.plugins.skills.adapter`` 独立进程运行，
  拿不到 ``ctx``；改为从环境变量 ``SKILLS_DATA_DIR`` 取数据目录，用 ``load_from_disk()``
  从磁盘重建目录——与内核读同一份 ``<data_dir>/skills/*.md``，口径一致。
- 零耦合：经包内相对导入取 ``SkillsCatalog``；不 import ``sys.tasks`` /
  ``plugins.react``。

两连迁，终局落在本包：
  ① 从 ``agent/run`` 先迁 ``plugins/skills_mcp``——随即被 ``test_plugin_decoupling``
     的插件横向耦合守卫拦下；
  ② 改迁本包——适配器消费技能目录，与目录同包 ⇒
     intra-package 相对导入合法；``plugins/skills_mcp`` 桥只经 spawn 路径字符串
     引用它，字符串不是 import，不构成耦合。
  根因回顾：适配器栖身 run/ 本是为了 import ``agent.api`` 导出的 SkillsCatalog，该类当年被误沉 core——放置错误引发迁移，归位后 api 不再导出任何技能符号。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .catalog import SkillsCatalog

logger = logging.getLogger("agent.plugins.skills.adapter")

# : 与 ``core/mcp_client.py`` 的 ``_MCP_VERSION`` 一致。
_PROTOCOL = "2024-11-05"

# : 数据目录环境变量。
_ENV_DATA_DIR = "SKILLS_DATA_DIR"


# --------------------------------------------------------------------------- #
# 目录重建
# --------------------------------------------------------------------------- #

def _build_catalog() -> SkillsCatalog:
    """从磁盘重建技能目录。

     每次调用都重建：技能体量小，读盘便宜，且免去长驻缓存的
     失效问题——管理面改了磁盘文件，下一次调用即可见。
     """
    data_dir = Path(os.environ.get(_ENV_DATA_DIR) or "data")
    cat = SkillsCatalog(data_dir)
    cat.load_from_disk()
    return cat


def _slim_card(row: dict) -> dict:
    """把 ``catalog()`` 的卡片压成索引条目：剔除 content。

     ``SkillsCatalog._card`` 实际含 ``content``，若直接透传，一次 ``skill_list``
     就把全部技能正文灌进上下文——正是要避免的 token 膨胀。
     """
    return {"name": row.get("name"), "description": row.get("description"),
            "risk": row.get("risk"), "source": row.get("source"),
            "enabled": row.get("enabled")}


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #

def _tool_list(args: dict) -> tuple[str, bool]:
    """``skill_list``：列出技能索引。"""
    q = str(args.get("q") or "").strip()
    cat = _build_catalog()
    rows = cat.search(q) if q else cat.catalog()
    items = [_slim_card(r) for r in rows]
    text = json.dumps({"skills": items, "count": len(items)},
                      ensure_ascii=False, indent=2)
    return text, False


def _tool_view(args: dict) -> tuple[str, bool]:
    """``skill_view``：读取单个技能的完整正文；可选 ``view_range`` 行切片。"""
    name = str(args.get("name") or "").strip()
    if not name:
        return "参数 name 必填", True
    cat = _build_catalog()
    s = cat.load(name)
    if s is None:
        return f"未找到技能 {name!r}", True
    text = cat.render_content(s)
    vr = args.get("view_range")
    if vr:
        try:
            if isinstance(vr, str):
                vr = [int(x) for x in vr.split(",")]
            start, end = int(vr[0]), int(vr[1])
            lines = text.split("\n")
            if start < 1:
                start = 1
            if end > len(lines):
                end = len(lines)
            if start > end:
                return "view_range 非法：start > end", True
            text = "\n".join(lines[start - 1:end])
        except Exception as exc:                       # noqa: BLE001
            return f"view_range 解析失败：{exc}", True
    resources = getattr(s, "resources", None) or []
    if resources:
        text += "\n\n## 捆绑资源（B3）\n" + "\n".join(f"- {r}" for r in resources)
    return text, False


# --------------------------------------------------------------------------- #
# 执行类工具
# --------------------------------------------------------------------------- #

_TRUNCATE_CHARS = 20000


def _allow_exec() -> bool:
    """门控：allow_exec=false 时执行类工具整体不可见。"""
    return os.environ.get("SKILLS_ALLOW_EXEC", "0") == "1"


def _exec_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get("SKILLS_EXEC_TIMEOUT_S", "120")))
    except ValueError:
        return 120.0


def _bash_deny() -> list[str]:
    """命令黑名单。可经 SKILLS_BASH_DENY 配置。"""
    raw = os.environ.get("SKILLS_BASH_DENY", "") or ""
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _truncate(text: str) -> str:
    if len(text) > _TRUNCATE_CHARS:
        return text[:_TRUNCATE_CHARS] + f"\n…[输出已截断至 {_TRUNCATE_CHARS} 字符]"
    return text


def _tool_bash(args: dict) -> tuple[str, bool]:
    """``skill_bash``：在宿主环境执行 shell 命令。"""
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return "参数 command 必填", True
    low = cmd.lower()
    for den in _bash_deny():
        if den and den in low:
            return f"命令被黑名单拦截：命中 {den!r}", True
    cwd = os.environ.get("SKILLS_WORKDIR") or os.getcwd()
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                              text=True, timeout=_exec_timeout(),
                              encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        return _truncate(f"[超时 >{_exec_timeout():.0f}s 已中断]\n" + out), True
    except Exception as exc:                       # noqa: BLE001
        return f"{type(exc).__name__}: {exc}", True
    return _truncate(f"$ {cmd}\n\n" + ((proc.stdout or "") + (proc.stderr or ""))), False


def _tool_file_create(args: dict) -> tuple[str, bool]:
    """``skill_file_create``：写/创建文件。"""
    path = str(args.get("path") or "").strip()
    content = str(args.get("content") or "")
    force = bool(args.get("force", False))
    if not path:
        return "参数 path 必填", True
    p = Path(path)
    if p.exists() and not force:
        return f"文件已存在（如需覆盖请传 force=true）：{path}", True
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as exc:                       # noqa: BLE001
        return f"{type(exc).__name__}: {exc}", True
    return f"已写入 {p}（{len(content)} 字符）", False


def _tool_str_replace(args: dict) -> tuple[str, bool]:
    """``skill_str_replace``：文件内唯一匹配替换。"""
    path = str(args.get("path") or "").strip()
    old = str(args.get("old_string") or "")
    new = str(args.get("new_string") or "")
    if not path:
        return "参数 path 必填", True
    if not old:
        return "参数 old_string 必填", True
    p = Path(path)
    if not p.exists():
        return f"文件不存在：{path}", True
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as exc:                       # noqa: BLE001
        return f"{type(exc).__name__}: {exc}", True
    count = text.count(old)
    if count == 0:
        return "old_string 未在文件中出现", True
    if count > 1:
        return f"old_string 出现 {count} 次，不唯一；请提供更长上下文", True
    try:
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
    except Exception as exc:                       # noqa: BLE001
        return f"{type(exc).__name__}: {exc}", True
    return f"已在 {p} 替换 1 处", False


_HANDLERS = {
    "skill_list": _tool_list,
    "skill_view": _tool_view,
    "skill_bash": _tool_bash,
    "skill_file_create": _tool_file_create,
    "skill_str_replace": _tool_str_replace,
}


def _tools() -> list[dict]:
    """MCP ``tools/list`` 返回的工具清单。

     B2：执行类工具受 ``SKILLS_ALLOW_EXEC`` 门控——
     关闭时根本不返回，fail-closed。
     """
    tools = [
        {
            "name": "skill_list",
            "description": ("列出可用技能（只读）：返回名称/描述/风险/来源，**不含正文**。"
                            "传 q 可按名称、描述、标签过滤。要看某技能的完整说明，"
                            "再用 skill_view 按 name 读取。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "q": {"type": "string",
                          "description": "可选关键词（匹配名称/描述/标签）"},
                },
                "required": [],
            },
        },
        {
            "name": "skill_view",
            "description": ("读取指定技能的完整说明正文（只读）。正文里的 {baseDir} 占位"
                            "已替换成该技能文件所在目录。可传 view_range 按行切片。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名，如 docker-build / skill.docker（不存在的名字会报错）"},
                    "view_range": {"type": "string",
                                   "description": "可选，行切片 'start,end'（1-based，含 end），如 '1,50'"},
                },
                "required": ["name"],
            },
        },
    ]
    if _allow_exec():
        tools += [
            {
                "name": "skill_bash",
                "description": ("在宿主环境执行 shell 命令（完全权限，直接操作电脑）。"
                                "返回合并的 stdout/stderr，超时与超长自动截断。"),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的 shell 命令"},
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "skill_file_create",
                "description": ("写/创建文件（不静默覆盖：已存在且未传 force=true 会报错）。"
                                "直接操作电脑，路径可为绝对或相对 SKILLS_WORKDIR。"),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件写入路径"},
                        "content": {"type": "string", "description": "文件内容（默认空）"},
                        "force": {"type": "boolean", "description": "已存在时是否覆盖，默认 false"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "skill_str_replace",
                "description": ("文件内唯一匹配替换：old_string 必须精确出现且仅出现一次，"
                                "否则报错（避免误改）。直接操作电脑。"),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "目标文件路径"},
                        "old_string": {"type": "string", "description": "待替换的原文（须唯一）"},
                        "new_string": {"type": "string", "description": "替换后的新文（默认空=删除）"},
                    },
                    "required": ["path", "old_string"],
                },
            },
        ]
    return tools


# --------------------------------------------------------------------------- #
# JSON-RPC 分派
# --------------------------------------------------------------------------- #

def _dispatch(req: dict) -> dict | None:
    """处理单条 JSON-RPC 请求。

     通知类返回 ``None`` ⇒ 不回包。
     """
    mid = req.get("id")
    method = str(req.get("method") or "")
    params = req.get("params") if isinstance(req.get("params"), dict) else {}

    def _ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _err(message: str, code: int = -32601) -> dict:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}

    if mid is None or method.startswith("notifications/"):
        return None
    if method == "initialize":
        return _ok({"protocolVersion": _PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "skills", "version": "0.1.0"}})
    if method == "tools/list":
        return _ok({"tools": _tools()})
    if method == "tools/call":
        name = str(params.get("name") or "")
        raw_args = params.get("arguments")
        args = raw_args if isinstance(raw_args, dict) else {}
        handler = _HANDLERS.get(name)
        if handler is None:
            return _ok({"content": [{"type": "text", "text": f"未知工具 {name!r}"}],
                        "isError": True})
        try:
            text, is_error = handler(args)
        except Exception as exc:                       # noqa: BLE001
            # 目录重建失败也要以 isError 回给调用方，不能让进程静默崩
            text, is_error = f"{type(exc).__name__}: {exc}", True
        return _ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return _err(f"未知方法 {method!r}")


def main() -> int:
    """stdio 主循环：逐行读请求、逐行写回。

     用二进制流而非文本流：避免 Windows 下 stdin/stdout 的默认编码
     把 UTF-8 的技能正文解错。
     """
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        if not isinstance(req, dict):
            continue
        resp = _dispatch(req)
        if resp is None:
            continue
        out.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        out.flush()
    return 0


__all__ = ["main", "_dispatch", "_tools", "_build_catalog", "_slim_card"]


if __name__ == "__main__":
    raise SystemExit(main())
