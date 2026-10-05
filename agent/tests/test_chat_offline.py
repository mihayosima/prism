"""对话驱动测试 —— 离线确定性（fixture）：规划→执行→解说闭环 · guard 拒绝 · 无 key 诚实。"""

from __future__ import annotations

import asyncio

from agent.profiles import TIGHT_PATCH, build_agent


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path):
    return {"db_path": str(tmp_path / "agent.db")}


def test_chat_ping_round_trip(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        reply = await c.root.chat.send(c.root, "ping 健康检查")
        assert reply.ok
        assert reply.plan["intent"] == "ping"
        assert reply.text.strip()
        assert reply.evidence["ok"] is True
        assert reply.evidence["intent"] == "ping"
        assert reply.tokens_out > 0
        # durable：chat/in + chat/done 落 audit 脊柱
        n = await c.root.db.query(
            c.root, "SELECT COUNT(*) AS n FROM agent_events WHERE event LIKE 'agent/chat/%'")
        assert n[0]["n"] >= 2
        await c.teardown()

    _run(s())


def test_chat_plain_chat_intent(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        reply = await c.root.chat.send(c.root, "你好呀")
        assert reply.plan["intent"] == "chat"
        assert reply.evidence["ok"] is True
        await c.teardown()

    _run(s())


def test_chat_ping_denied_by_tight_guard(tmp_path):
    """矩阵收窄（deny agent.ping）→ chat 内的 ping 执行被 guard 拒绝 → 如实上报，不假成功。"""
    c = build_agent(config=_cfg(tmp_path), patch=TIGHT_PATCH)

    async def s():
        await c.assemble()
        reply = await c.root.chat.send(c.root, "ping 健康检查")
        assert not reply.ok
        assert reply.evidence["ok"] is False
        assert "未授权" in (reply.error or "") or "guard" in (reply.error or "")
        await c.teardown()

    _run(s())


def test_chat_honest_when_no_key(monkeypatch, tmp_path):
    """deepseek 无 key → 规划/解说被占位 Provider 诚实拒绝 → 降级告知，不碰网络。"""
    monkeypatch.setattr("agent.sys.llm._resolve_key", lambda ctx: None)
    c = build_agent(config={**_cfg(tmp_path),
                            "agent": {"llm": {"provider": "deepseek", "model": "deepseek_v4_flash"}}})

    async def s():
        await c.assemble()
        reply = await c.root.chat.send(c.root, "你好")
        assert not reply.ok
        assert "key" in (reply.error or "").lower()
        await c.teardown()

    _run(s())

def test_chat_background_tasks_are_tracked_and_drained(tmp_path):
    """★ 后台任务必须**被持有并追踪**：裸 ``create_task`` 不持引用 ⇒ 可能被 GC 中途
    回收、teardown 也不等它 ⇒ 压缩静默丢失（表现为"历史有时没压缩"）。

    与 ``core/events.py`` 的 ``_tasks`` + ``aclose()`` 同一纪律；此处对称落地到 chat。
    """
    from agent.sys.chat import ChatDriver

    d = ChatDriver()
    started = False

    async def slow():
        nonlocal started
        started = True
        await asyncio.sleep(10)

    async def s():
        d._spawn_bg(slow())
        await asyncio.sleep(0)                 # 让任务真正启动
        assert started, "后台任务没跑起来"
        assert len(d._bg) == 1, "在途任务未被登记 ⇒ 等于没追踪"
        await d.drain()
        assert not d._bg, "drain 后仍残留登记 ⇒ 没等到/没取消"

    _run(s())


def test_chat_teardown_drains_background_tasks(tmp_path):
    """端到端：容器 teardown 走 effect ⇒ 后台任务被收干净（不留悬挂写库）。"""
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        await c.root.chat.send(c.root, "ping 健康检查")
        await c.teardown()                     # 收尾必须不抛、不挂

    _run(s())
