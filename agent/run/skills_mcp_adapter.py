"""agent.run.skills_mcp_adapter —— 只读技能 MCP 服务器（stdlib stdio JSON-RPC）。

把本仓 ``Services.SKILLS`` 的技能目录以 MCP 工具形式对外暴露，供子 agent / 外部 MCP
客户端消费。设计要点（均经真实代码核实）：

- **只读**：仅暴露 ``skill_list`` / ``skill_view`` 两个读工具，不提供任何写/执行动作。
  执行能力由内核的 ``Skill.run`` 承担，**不经**本适配器。
- **零第三方依赖**：``core/mcp_client.py`` 是 stdlib 的 JSON-RPC stdio 客户端，本模块是
  它的**对端**——同样用 stdlib 手写服务端。实测本机 ``fastmcp`` / ``mcp`` 均未安装且无 uv，
  故不走框架路线（方案 §7-5 的「手写 stdio 桥」选项）。
- **子进程无 ctx**：以 ``python -m agent.run.skills_mcp_adapter`` 独立进程运行，拿不到
  ``ctx``；改为从环境变量 ``SKILLS_DATA_DIR`` 取数据目录，用 ``load_from_disk()`` 从磁盘
  **重建**目录——与内核读同一份 ``<data_dir>/skills/*.md``，口径一致（参照
  ``plugins/skills/_resolve_data_dir``：数据目录 = ``db_path`` 的父目录）。
- **零耦合**：经 ``agent.api`` 取 ``SkillsCatalog``（api 不得反向依赖 sys，故技能目录落在
  core 并由 api 导出）；不 import ``sys.tasks`` / ``plugins.react``
  （由 ``test_services_contract`` 的 AST 守卫）。

线协议（与 ``core/mcp_client.py`` 严格对齐）：换行分隔的 JSON-RPC 2.0，
``initialize`` → ``notifications/initialized`` → ``tools/list`` → ``tools/call``。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from agent.api import SkillsCatalog

logger = logging.getLogger("agent.run.skills_mcp_adapter")

#: 与 ``core/mcp_client.py`` 的 ``_MCP_VERSION`` 一致。
_PROTOCOL = "2024-11-05"

#: 数据目录环境变量（由 ``plugins/skills_mcp`` 桥插件注入）。
_ENV_DATA_DIR = "SKILLS_DATA_DIR"


# --------------------------------------------------------------------------- #
# 目录重建（子进程侧，无 ctx）
# --------------------------------------------------------------------------- #

def _build_catalog() -> SkillsCatalog:
    """从磁盘重建技能目录。

    **每次调用都重建**：技能体量小（几十个 markdown），读盘便宜，且免去长驻缓存的
    失效问题——管理面（``/api/skills``）改了磁盘文件，下一次调用即可见。
    """
    data_dir = Path(os.environ.get(_ENV_DATA_DIR) or "data")
    cat = SkillsCatalog(data_dir)
    cat.load_from_disk()
    return cat


def _slim_card(row: dict) -> dict:
    """把 ``catalog()`` 的卡片压成索引条目：**剔除 content**（正文由 skill_view 给）。

    ``SkillsCatalog._card`` 实际含 ``content``（见 ``sys/skills.py``），若直接透传，
    一次 ``skill_list`` 就把全部技能正文灌进上下文——正是要避免的 token 膨胀。
    """
    return {"name": row.get("name"), "description": row.get("description"),
            "risk": row.get("risk"), "source": row.get("source"),
            "enabled": row.get("enabled")}


# --------------------------------------------------------------------------- #
# 工具实现（只读）
# --------------------------------------------------------------------------- #

def _tool_list(args: dict) -> tuple[str, bool]:
    """``skill_list``：列出技能索引（不含正文）。"""
    q = str(args.get("q") or "").strip()
    cat = _build_catalog()
    rows = cat.search(q) if q else cat.catalog()
    items = [_slim_card(r) for r in rows]
    text = json.dumps({"skills": items, "count": len(items)},
                      ensure_ascii=False, indent=2)
    return text, False


def _tool_view(args: dict) -> tuple[str, bool]:
    """``skill_view``：读取单个技能的完整正文（``{baseDir}`` 已替换）。"""
    name = str(args.get("name") or "").strip()
    if not name:
        return "参数 name 必填", True
    cat = _build_catalog()
    s = cat.load(name)
    if s is None:
        return f"未找到技能 {name!r}", True
    return cat.render_content(s), False


_HANDLERS = {"skill_list": _tool_list, "skill_view": _tool_view}


def _tools() -> list[dict]:
    """MCP ``tools/list`` 返回的工具清单（含 inputSchema，供 ``build_task_schema`` 转换）。"""
    return [
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
                            "已替换成该技能文件所在目录。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名，如 docker-build / skill.docker（不存在的名字会报错）"},
                },
                "required": ["name"],
            },
        },
    ]


# --------------------------------------------------------------------------- #
# JSON-RPC 分派
# --------------------------------------------------------------------------- #

def _dispatch(req: dict) -> dict | None:
    """处理单条 JSON-RPC 请求。

    通知类（无 ``id``，或 method 以 ``notifications/`` 开头）返回 ``None`` ⇒ 不回包。
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
            # 目录重建失败（如同名冲突）也要以 isError 回给调用方，不能让进程静默崩
            text, is_error = f"{type(exc).__name__}: {exc}", True
        return _ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return _err(f"未知方法 {method!r}")


def main() -> int:
    """stdio 主循环：逐行读请求、逐行写回（写完立即 flush）。

    用**二进制**流而非文本流：避免 Windows 下 stdin/stdout 的默认编码（GBK/CP1252）
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
