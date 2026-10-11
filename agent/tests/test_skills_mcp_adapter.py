"""agent.tests.test_skills_mcp_adapter —— 只读技能 MCP 适配器单测。

覆盖：JSON-RPC 分派（initialize / 通知 / tools/list / tools/call / 未知方法）、
``skill_list`` **剔除 content** 控 token、``skill_view`` 返回正文且 ``{baseDir}`` 已替换、
未找到技能报错、以及 S2 回填的 ``basedir`` / ``render_content`` 与配置节登记。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.api import Services, SkillsCatalog, parse_skill_file
from agent.core.config_schema import _SCHEMA
from agent.core.skill import Skill, skill
from agent.plugins.skills_mcp import SkillsMcp
from agent.run.skills_mcp_adapter import _dispatch, _tools


def _write_skill(tmp_path, stem: str, description: str, body: str) -> str:
    """在 ``<tmp>/skills/<stem>.md`` 写一个带 frontmatter 的技能文件，返回其路径。"""
    d = tmp_path / "skills"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stem}.md"
    p.write_text("---\n"
                 f"name: {stem}\n"
                 f"description: {description}\n"
                 "---\n\n"
                 f"{body}\n", encoding="utf-8")
    return str(p)


def _call(tmp_path, monkeypatch, name: str, args: dict) -> dict:
    """发一条 ``tools/call`` 并返回完整 JSON-RPC 响应。"""
    monkeypatch.setenv("SKILLS_DATA_DIR", str(tmp_path))
    return _dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": name, "arguments": args}})


def _text(resp: dict) -> str:
    return resp["result"]["content"][0]["text"]


# ---------------------------- JSON-RPC 分派 ---------------------------- #

def test_initialize_returns_protocol_version():
    resp = _dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2024-11-05"


def test_notification_does_not_respond():
    """``notifications/initialized`` 无 id ⇒ 不回包（否则客户端会收到错位响应）。"""
    assert _dispatch({"jsonrpc": "2.0", "method": "notifications/initialized",
                      "params": {}}) is None


def test_tools_list_exposes_two_readonly_tools():
    names = [t["name"] for t in _tools()]
    assert names == ["skill_list", "skill_view"]
    for t in _tools():
        assert "inputSchema" in t, f"{t['name']} 缺 inputSchema（build_task_schema 需要）"


def test_unknown_method_returns_error():
    resp = _dispatch({"jsonrpc": "2.0", "id": 7, "method": "nope", "params": {}})
    assert "error" in resp


def test_unknown_tool_call_returns_is_error():
    resp = _dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "skill_delete", "arguments": {}}})
    assert resp["result"]["isError"] is True


# ---------------------------- 工具行为 ---------------------------- #

def test_skill_list_excludes_content(tmp_path, monkeypatch):
    """``catalog()`` 实际含 content；skill_list 必须剔除，否则一次调用灌满上下文。"""
    _write_skill(tmp_path, "skill.docker", "构建 docker 镜像", "步骤：docker build -t x .")
    resp = _call(tmp_path, monkeypatch, "skill_list", {})
    payload = json.loads(_text(resp))
    assert payload["count"] == 1
    row = payload["skills"][0]
    assert row["name"] == "skill.docker"
    assert "content" not in row, "skill_list 不得返回正文（token 膨胀）"
    assert "docker build" not in _text(resp), "正文不应出现在索引结果里"


def test_skill_list_filters_by_query(tmp_path, monkeypatch):
    _write_skill(tmp_path, "skill.docker", "构建 docker 镜像", "body-a")
    _write_skill(tmp_path, "skill.sql", "写 SQL 查询", "body-b")
    resp = _call(tmp_path, monkeypatch, "skill_list", {"q": "docker"})
    payload = json.loads(_text(resp))
    assert [r["name"] for r in payload["skills"]] == ["skill.docker"]


def test_skill_view_returns_content(tmp_path, monkeypatch):
    _write_skill(tmp_path, "skill.docker", "构建 docker 镜像", "步骤：docker build -t x .")
    resp = _call(tmp_path, monkeypatch, "skill_view", {"name": "skill.docker"})
    assert resp["result"]["isError"] is False
    assert "docker build" in _text(resp)


def test_skill_view_unknown_name_is_error(tmp_path, monkeypatch):
    resp = _call(tmp_path, monkeypatch, "skill_view", {"name": "skill.nope"})
    assert resp["result"]["isError"] is True
    assert "未找到技能" in _text(resp)


def test_skill_view_requires_name(tmp_path, monkeypatch):
    resp = _call(tmp_path, monkeypatch, "skill_view", {})
    assert resp["result"]["isError"] is True
    assert "name 必填" in _text(resp)


def test_skill_view_replaces_basedir(tmp_path, monkeypatch):
    """S2：``{baseDir}`` 必须替换成技能文件所在目录，否则调用方拿到无效路径。"""
    _write_skill(tmp_path, "skill.docker", "构建镜像",
                 "运行：{baseDir}/scripts/build.sh")
    resp = _call(tmp_path, monkeypatch, "skill_view", {"name": "skill.docker"})
    text = _text(resp)
    assert "{baseDir}" not in text, "占位符未替换"
    assert str(tmp_path / "skills") in text


# ---------------------------- S2 回填项 ---------------------------- #

def test_parse_skill_file_records_basedir(tmp_path):
    p = _write_skill(tmp_path, "skill.docker", "构建镜像", "body")
    s = parse_skill_file(p)
    assert s.basedir == str(tmp_path / "skills")


def test_render_content_replaces_basedir(tmp_path):
    p = _write_skill(tmp_path, "skill.docker", "构建镜像", "run {baseDir}/s.sh")
    cat = SkillsCatalog(tmp_path)
    cat.load_from_disk()
    s = cat.load("skill.docker")
    assert cat.render_content(s) == f"run {tmp_path / 'skills'}/s.sh"


def test_render_content_noop_without_placeholder():
    s = skill("skill.plain", "无占位", content="纯文本")
    cat = SkillsCatalog("data")
    assert cat.render_content(s) == "纯文本"


def test_skill_dataclass_has_basedir():
    assert "basedir" in Skill.__dataclass_fields__
    assert Skill("skill.x", "d").basedir == ""
    assert skill("skill.y", "d", basedir="/tmp/skills").basedir == "/tmp/skills"


# ---------------------------- 配置登记 ---------------------------- #

def test_config_schema_registers_skills_mcp_as_any():
    """``skills_mcp`` 必须是 ``_any`` 宽松节：严格节漏登记会让设置页保存 fail-fast。"""
    assert _SCHEMA.get("skills_mcp") == "_any"


# ---------------------------- 桥插件端到端 ---------------------------- #

class _FakeTasks:
    def __init__(self):
        self.registered: dict[str, dict] = {}

    def register(self, ctx, name, schema=None, handler=None, owner=None, description=None):
        self.registered[name] = {"schema": schema, "handler": handler,
                                 "owner": owner, "description": description}


class _FakeCtx:
    def __init__(self, config):
        self.config = config
        self.state: dict = {}
        self.tasks = _FakeTasks()
        self.provided: dict = {}
        self.effect_fn = None

    def provide(self, key, value):
        self.provided[key] = value

    def get(self, key, default=None):
        return self.provided.get(key, default)

    def effect(self, fn):
        self.effect_fn = fn


def test_bridge_disabled_is_noop(tmp_path):
    """未启用：不起进程、不注册工具，只登记自身 ⇒ 零影响。"""
    ctx = _FakeCtx({"skills_mcp": {"enabled": False},
                    "db_path": str(tmp_path / "agent.db")})
    asyncio.run(SkillsMcp.apply(ctx))
    assert ctx.tasks.registered == {}, "未启用却注册了工具"
    assert ctx.provided.get(Services.SKILLS_MCP) is not None, "未启用也应 provide 自身"


def test_bridge_enabled_spawns_and_registers(tmp_path):
    """启用态端到端：真起子进程 → 注册 ``mcp.skills.*`` → 调用真的拿到正文。"""
    _write_skill(tmp_path, "skill.docker", "构建 docker 镜像", "步骤：docker build -t x .")
    ctx = _FakeCtx({"skills_mcp": {"enabled": True, "timeout_s": 30.0},
                    "db_path": str(tmp_path / "agent.db")})
    async def _flow():
        await SkillsMcp.apply(ctx)
        assert sorted(ctx.tasks.registered) == ["mcp.skills.skill_list",
                                                "mcp.skills.skill_view"]
        # 真调一次：经 McpStdioClient 打到子进程
        handler = ctx.tasks.registered["mcp.skills.skill_view"]["handler"]
        res = await handler(name="skill.docker")
        assert "docker build" in res["text"], f"调用未拿到正文: {res}"
        # skill_list 同样走通，且不含正文
        lres = await ctx.tasks.registered["mcp.skills.skill_list"]["handler"]()
        assert "content" not in lres["text"], "skill_list 不应返回正文"
        assert "skill.docker" in lres["text"]
        # 收尾关子进程（同一事件循环内，避免跨 loop 的 transport 告警）
        if ctx.effect_fn is not None:
            await ctx.effect_fn()

    asyncio.run(_flow())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
