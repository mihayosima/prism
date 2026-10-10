"""P2-1 TodoWrite 插件 —— 落地测试（零触网，fixture LLM）。

四用例：
- A（Store 纯逻辑）：增删改、完成度 percent、严格状态机（多 in_progress 自动归一）、会话分桶隔离。
- B（plan 可见性）：catalog 中 todo.write/todo.list 的 risk=="read"，且复刻 loop.py:245 的
  plan 过滤放行它们 —— 固化 risk="read" 生效，顺带落实验证「plan 软可见性过滤过粗」批判。
- C（事件发射）：todo.write 后 emit TODO_PROGRESS，payload 含 percent。
- D（端到端）：fixture「待办」脚本 round0 调 todo.write 列 3 项（1 completed/1 in_progress/1 pending），
  round1 收尾；react.run 后 TodoStore 命中对应状态。

手法与 test_no_progress.py 同构：build_agent（默认 fixture provider）+ pm.set_enabled("plugins.todo", True)
+ 驱动 c.root.react.run。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.api import Events, Services, make_identity
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

from agent.plugins.todo.store import TodoStore


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path):
    PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.react", True)
    pm.set_enabled("plugins.work_memory", True)
    pm.set_enabled("plugins.todo", True)
    return pm


# ---- A：TodoStore 纯逻辑 ----

def test_todo_store_unit():
    s = TodoStore()
    sk = "sessA"
    s.write(sk, [
        {"content": "读需求", "status": "completed"},
        {"content": "实现", "status": "in_progress"},
        {"content": "测试", "status": "pending"},
    ])
    sm = s.summary(sk)
    assert sm["total"] == 3
    assert sm["completed"] == 1
    assert sm["in_progress"] == 1
    assert sm["percent"] == 33

    # 整表替换：再写即覆盖旧状态
    s.write(sk, [{"content": "仅一项"}])
    assert s.summary(sk)["total"] == 1

    # 严格状态机：多 in_progress 自动归一（保留列表中最后一个）
    s.write(sk, [
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
        {"content": "c", "status": "in_progress"},
    ])
    assert s.summary(sk)["in_progress"] == 1
    in_prog = [t for t in s.list(sk) if t["status"] == "in_progress"]
    assert in_prog[0]["content"] == "c"            # 最后一个保留

    # 非法 status 归一为 pending（不拒绝调用）
    s.write(sk, [{"content": "x", "status": "bogus"}])
    assert s.list(sk)[0]["status"] == "pending"

    # 会话分桶隔离：另一会话不被影响
    s.write("sessB", [{"content": "别的会话"}])
    assert s.summary("sessB")["total"] == 1
    assert s.summary(sk)["total"] == 1


# ---- B：plan 模式可见性（固化 risk="read"） ----

def test_todo_visible_in_plan_mode(tmp_path):
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")},
                    plugin_mgr=_pm(tmp_path))

    async def s():
        await c.assemble()
        catalog = c.root.tasks.catalog()
        await c.teardown()
        return catalog

    catalog = _run(s())
    by_name = {t["name"]: t for t in catalog}
    assert "todo.write" in by_name and "todo.list" in by_name
    # risk=="read" ⇒ plan 过滤（loop.py:245 的 catalog=[t for t in catalog if t.get("risk")=="read"]）放行
    assert by_name["todo.write"]["risk"] == "read"
    assert by_name["todo.list"]["risk"] == "read"
    # 复刻 loop.py:245 的 plan 过滤，确认 todo.* 在 plan 下确实可见
    visible_in_plan = {t["name"] for t in catalog if t["risk"] == "read"}
    assert "todo.write" in visible_in_plan and "todo.list" in visible_in_plan


# ---- C：事件发射 ----

def test_todo_emits_progress(tmp_path):
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")},
                    plugin_mgr=_pm(tmp_path))
    captured: list[dict] = []

    def collector(summary):
        captured.append(summary)

    async def s():
        await c.assemble()
        c.root.events.on(c.root, Events.TODO_PROGRESS, collector)
        plugin = c.root.get(Services.TODO)
        res = await plugin._write(todos=[
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "in_progress"},
        ])
        await c.teardown()
        return res

    res = _run(s())
    assert res["total"] == 2 and res["percent"] == 50
    assert captured, "TODO_PROGRESS 未被 emit"
    assert captured[-1]["percent"] == 50


# ---- D：端到端（经 react.run，fixture 脚本化调 todo.write） ----

def test_todo_e2e_via_react(tmp_path):
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")},
                    plugin_mgr=_pm(tmp_path))

    async def s():
        await c.assemble()
        ident = make_identity(tenant="t1", user="u1", session="sessTodo")
        res = await c.root.react.run(
            c.root, "请用 TodoWrite 建立待办清单规划这个多步任务",
            session_id=ident.session_key, identity=ident)
        plugin = c.root.get(Services.TODO)
        snap = await plugin.list_tasks(c.root)
        await c.teardown()
        return res, snap

    res, snap = _run(s())
    assert res.ok is True
    assert snap["total"] == 3
    assert snap["completed"] == 1
    assert snap["in_progress"] == 1
    assert snap["pending"] == 1
    assert snap["percent"] == 33
