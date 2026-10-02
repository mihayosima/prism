"""R1 补：权限档位（``PERMISSION_MODE``）按会话分桶 —— 档位不再跨会话互相覆盖。

**要治的缺陷**：档位是**会话级语义**（"本会话多谨慎"），此前却存**容器级单值** ⇒
``decide()`` 拿到的是"最后一次有人改的档位"。最严重后果：A 会话切 ``bypass`` 会**同时
打开 B 会话在跑 run 的审批闸**（``_pre_task`` 命中 bypass 直接 ``next()``）——放行本应
问人的**写操作**。这比"预算记错桶"严重：那是记错账，这是放行。

**修法**：值改为 ``dict[会话键, mode]``（与 ``BUDGET_SPENT`` 同款分桶），读写一律经内核
助手 ``current_permission_mode()`` / ``set_permission_mode()``——形状判断 SSOT，且**容错
识别遗留的 ``str`` 单值形态**（既有直接写 state 的调用点与测试因此零改动）。

三个读取方均按会话取值：``approval.mode()``（判定闸门）、``react.visible_tools``（工具
软裁剪）、``react.status``（状态上报）。

**残留（诚实登记）**：终态工具 ``submit_plan`` 的**可达性**仍是容器级（注册表无"按会话
可见"维度）——只要有会话在 plan，其它会话也能看见它。越权面已被 ``_submit_plan`` 的硬
兜底关死（非 plan 会话调用即自拒），用例 6 锁定这一点。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from agent.api import StateKeys, set_current_session_key
from agent.plugins.approval import Approval
from agent.plugins.react.loop import visible_tools
from agent.profiles import build_agent

AGENT = Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


def _build(tmp_path, approval=None):
    agent: dict = {}
    if approval is not None:
        agent["approval"] = approval
    cfg: dict = {"db_path": str(tmp_path / "agent.db")}
    if agent:
        cfg["agent"] = agent
    return build_agent(config=cfg, extra_plugins=(Approval,))


def _register(c):
    async def h():
        return {"ok": True}

    for name, risk in (("t.read", "read"), ("t.write", "write"), ("t.danger", "danger")):
        c.root.tasks.register(c.root, name, schema={}, handler=h, risk=risk, owner=None)


# ---------------------------------------------------------------------------
# ① 档位本身：会话隔离
# ---------------------------------------------------------------------------

def test_mode_is_per_session(tmp_path):
    """A 设 plan 不影响 B；A 自己的档位稳定可读。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        set_current_session_key("u:A")
        await c.root.approval.set_mode(c.root, "plan", by="test")
        assert c.root.approval.mode() == "plan"

        set_current_session_key("u:B")
        assert c.root.approval.mode() == "default", "B 继承了 A 的档位（串会话）"

        set_current_session_key("u:A")
        assert c.root.approval.mode() == "plan", "A 的档位被 B 的读取/写入改写"
        await c.teardown()

    _run(s())


def test_bypass_does_not_open_other_sessions_gate(tmp_path):
    """★ 安全主用例：A 切 bypass 不得放行 B 的在跑 run。

    修前：``mode`` 存容器级单值 ⇒ B 的 ``_pre_task`` 读到 A 写的 ``bypass`` ⇒
    直接 ``next()`` 放行写操作（本用例第二个 invoke 会"成功"，断言即红）。
    """
    c = _build(tmp_path, approval={"ask": ["*"], "provider": "auto-deny"})

    async def s():
        await c.assemble()
        _register(c)

        set_current_session_key("u:A")
        await c.root.approval.set_mode(c.root, "bypass", by="test")
        # A 自己：bypass 生效，写类不打扰
        assert (await c.root.tasks.invoke(c.root, "t.write", caller="t")) == {"ok": True}

        set_current_session_key("u:B")
        assert c.root.approval.mode() == "default"
        try:
            await c.root.tasks.invoke(c.root, "t.write", caller="t")
            raise AssertionError("B 的写类调用被 A 的 bypass 放行了（档位跨会话）")
        except Exception as exc:                        # noqa: BLE001
            assert "未获批准" in str(exc), f"B 应被 ask+auto-deny 拒绝，实际：{exc}"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ② 读取方：工具软裁剪与状态上报也按会话
# ---------------------------------------------------------------------------

def test_tool_trim_is_per_session(tmp_path):
    """A 在 plan ⇒ 只有 A 的工具被裁到只读；B 仍看得见写类工具。

    断言**内容**而非计数：终态工具 ``submit_plan`` 的注册是**容器级**（有会话进 plan 即
    全局注册），故 B 的目录会多出这一项——按计数断言会把"注册表容器级"误报成"档位串会话"。
    该残留已显式锁定（见下方最后一条断言）。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register(c)

        def vis() -> set[str]:
            return {t["name"] for t in visible_tools(c.root)}

        set_current_session_key("u:B")
        assert {"t.read", "t.write", "t.danger"} <= vis()

        set_current_session_key("u:A")
        await c.root.approval.set_mode(c.root, "plan", by="test")
        assert "t.read" in vis()
        assert "t.write" not in vis() and "t.danger" not in vis(), \
            f"plan 会话应只剩只读，实际：{sorted(vis())}"

        set_current_session_key("u:B")
        assert {"t.write", "t.danger"} <= vis(), "B 被 A 的 plan 档位连带裁剪了"
        # 残留（已登记）：可达性容器级 ⇒ A 进 plan 后 B 也看得见终态工具；
        # 越权面由 _submit_plan 硬兜底关死（见 test_submit_plan_tool_self_rejects...）。
        # 若将来把可见性也做成按会话，本断言会红 —— 那时请同步更新文档中的残留说明。
        assert "approval.submit_plan" in vis()
        await c.teardown()

    _run(s())


def test_legacy_single_value_shape_still_honored(tmp_path):
    """遗留的单值形态（直接写 ``state``）仍生效——既有调用点与测试写法零改动。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register(c)
        c.root.state[StateKeys.PERMISSION_MODE] = "plan"        # 旧形状（str 单值）
        assert c.root.approval.mode() == "plan"
        assert visible_tools(c.root) and all(
            t["risk"] == "read" for t in visible_tools(c.root))
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ③ 终态工具的注册状态与硬兜底
# ---------------------------------------------------------------------------

def test_submit_plan_registration_is_refcounted(tmp_path):
    """注册状态按会话计数：B 离开 plan 不得注销 A 的终态工具。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        assert not c.root.tasks.has("approval.submit_plan")

        set_current_session_key("u:A")
        await c.root.approval.set_mode(c.root, "plan", by="test")
        assert c.root.tasks.has("approval.submit_plan"), "进 plan 应注册终态工具"

        set_current_session_key("u:B")
        await c.root.approval.set_mode(c.root, "default", by="test")
        assert c.root.tasks.has("approval.submit_plan"), \
            "B 切回 default 把正在 plan 的 A 的终态工具注销了"

        set_current_session_key("u:A")
        await c.root.approval.set_mode(c.root, "default", by="test")
        assert not c.root.tasks.has("approval.submit_plan"), "全都离开 plan 才应注销"
        await c.teardown()

    _run(s())


def test_submit_plan_tool_self_rejects_outside_plan(tmp_path):
    """注册表是容器级 ⇒ 真正的越权防线是工具**自拒**（软可见 + 硬兜底成对）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        set_current_session_key("u:A")
        await c.root.approval.set_mode(c.root, "plan", by="test")

        set_current_session_key("u:B")                  # B 非 plan，但工具存在（容器级注册）
        assert c.root.tasks.has("approval.submit_plan")
        out = await c.root.tasks.invoke(c.root, "approval.submit_plan",
                                        caller="t", plan="x")
        assert out.get("approved") is False
        assert "plan" in str(out.get("hint"))
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ④ 守卫（源码级）：入口必须解析身份 / 不得绕过内核助手直读
# ---------------------------------------------------------------------------

def test_approval_mode_endpoint_parses_identity():
    """``/api/approval/mode`` 必须解析身份——否则 ``set_mode`` 落进容器桶（改到全局）。"""
    src = (AGENT / "run" / "chat.py").read_text(encoding="utf-8")
    start = src.index("async def _approval_mode(")
    end = src.index("\n    @app.", start + 1)
    body = src[start:end]
    assert "_identity(request, body)" in body, \
        "档位端点是会话级的 ⇒ 必须先解析身份（否则改的是全局单值）"


# 会话级端点登记表：路径 → 为什么它是会话级的。
# 漏掉一个不会报错，只会**静默读到不确定的会话**（ContextVar 未绑定 ⇒ 回退分支读到
# 上一次聊天请求留下的值）。故用登记表锁死；反向也断言登记项仍存在，防表腐烂。
_SESSION_SCOPED = {
    "/api/chat": "本会话的转写/预算/续接",
    "/api/chat/stream": "同上（SSE）",
    "/api/chat/cancel": "取消要校验 run 归属",
    "/api/approval/answer": "只能应答本会话的在途请求",
    "/api/approval/pending": "只返回本会话的在途申请，并回报本会话档位",
    "/api/approval/mode": "档位按会话分桶",
    "/api/status": "预算与身份按会话回显",
    "/api/perms": "档位按会话分桶（前端档位展示，R1 补时发现漏解析）",
    "/api/session": "返回当前会话身份",
    "/api/session/reset": "只重置本会话",
    "/api/memory": "各层记忆按会话统计",
    "/api/settings": "会话级覆盖的读写",
}


def _route_blocks() -> dict[str, str]:
    src = (AGENT / "run" / "chat.py").read_text(encoding="utf-8")
    starts = [m.start() for m in re.finditer(r"\n    @app\.(get|post|delete)\(", src)]
    out: dict[str, str] = {}
    for i, pos in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(src)
        chunk = src[pos:end]
        path = re.search(r'"(/[^"]*)"', chunk)
        if path:
            out.setdefault(path.group(1), chunk)
    return out


def test_session_scoped_endpoints_parse_identity():
    """会话级端点必须经 ``_identity`` 解析身份（唯一漏斗，同时绑定请求作用域）。"""
    blocks = _route_blocks()
    stale = [p for p in _SESSION_SCOPED if p not in blocks]
    assert not stale, f"登记表指向的路由已不存在（请同步更新登记表）：{stale}"
    offenders = [p for p in _SESSION_SCOPED if "_identity(" not in blocks[p]]
    assert not offenders, f"会话级端点未解析身份（会读到不确定的会话）：{offenders}"


def test_permission_mode_reads_go_through_kernel_helper():
    """除内核助手外，生产代码不得再直接 ``state.get(PERMISSION_MODE)``（防形状判断分叉）。"""
    offenders: list[str] = []
    for sub in ("sys", "plugins", "run"):
        for path in sorted((AGENT / sub).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for i, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if "StateKeys.PERMISSION_MODE" in code and "state.get(" in code:
                    offenders.append(f"{path.relative_to(AGENT)}:{i}: {code.strip()}")
    assert not offenders, (
        "档位存在绕过内核助手的直读点（应改用 current_permission_mode(ctx)）："
        f"{offenders}")
