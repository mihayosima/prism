"""R1：旁路会话键/归组键的**请求级强隔离**（ContextVar）—— 并发两会话不得串账。

背景（详见 `docs/reports/engineering/R1方案选型分析-2026-10-02.md`）：

``SESSION_KEY`` / ``REACT_RUN_ID`` 是「**当前请求是谁**」的选择器，此前统一读写**容器级**
``ctx.state``。容器级 state 被并发请求共享 ⇒ 交错时后写覆盖先写：A 的预算记进 B 的桶、
A 的会话档位放行 B 的审批。R1 把**读取通道**改为 ContextVar（按 asyncio task 隔离，而一个
HTTP 请求 = 一个 task）；``ctx.state`` 仍照写（兼容既有写法），读取一律经
``current_session_key()`` / ``current_run_id()``（ContextVar 优先、**未绑定才回退** state）。

本文件即原路线图 **#9** 的验收对象，也是「R1 ≡ 旧报告 F-2 ≡ #9」这一被识别三次却始终未解
缺陷的收口。

用例纪律：断言拦**语义**（读串 / 回退 / 置空），不只断言函数存在。第 1、5 例的并发编排是
**确定性**的（用 Event 严格排序），故把读取通道换回旧的「只读 state」实现时**必红**——
即它们确为该缺陷的检测器，而非靠时序碰运气（实测见本轮记录）。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from agent.api import (Services, State, StateKeys, bind_request_id,
                       current_run_id, current_session_key, set_current_run_id,
                       set_current_session_key)
from agent.profiles import build_agent
from agent.sys.budget import _run_id as budget_run_id
from agent.sys.budget import _session_key as budget_session_key
from agent.sys.plugin_manager import PluginManager

AGENT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = AGENT / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path) -> PluginManager:
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "plugin_state.json"))
    for pid in ("plugins.context", "plugins.approval"):
        pm.set_enabled(pid, True)
    return pm


def _build(tmp_path):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       plugin_mgr=_pm(tmp_path))


def _readers(c) -> tuple[str, str, str]:
    """三个**真实读取点**的读数（不是复刻实现，是生产代码本身）。

    - ``budget``：预算桶选择（钱记到哪个会话的账上）
    - ``context/router``：会话级治理器实例选择（窗口参数取谁的）
    - ``approval``：审批归属判定（这次调用算谁的）
    """
    return (budget_session_key(c.root),
            c.root.get(Services.CONTEXT)._session_key(),
            c.root.get(Services.APPROVAL)._skey())


@pytest.fixture(autouse=True)
def _isolate_request_scope():
    """每个用例前后**解除**请求作用域绑定（会话键 / 归组键 / 请求标识）。

    ContextVar 绑定在**当前线程**的上下文里 ⇒ 不复位就会顺着 ``asyncio.run`` 的上下文
    复制**泄漏到后续用例**（本项目真踩过：别的文件里出现「未绑定却读到上一个用例的会话
    键」）。``None`` = 解除绑定，回到「未绑定 ⇒ 回退 ``ctx.state``」。
    """
    set_current_session_key(None)
    set_current_run_id(None)
    bind_request_id("")
    yield
    set_current_session_key(None)
    set_current_run_id(None)
    bind_request_id("")


def test_binding_emptiness_and_unbinding_are_three_distinct_states():
    """三态必须严格区分，否则收尾置空会被回退分支读回上一轮的值。

    1) **未绑定** ⇒ 回退 ``ctx.state``；
    2) **绑定空串** ⇒ 权威空值，**不回退**（run 收尾走的正是这条）；
    3) **解除绑定**（传 ``None``）⇒ 回到回退。
    """
    class _Ctx:
        def __init__(self):
            self.state = State({"session_key": "u:from-state",
                                "react_run_id": "u:from-state:r0"})

    ctx = _Ctx()
    try:
        assert current_session_key(ctx) == "u:from-state", "未绑定时应回退 state"
        assert current_run_id(ctx) == "u:from-state:r0"

        set_current_session_key("")
        set_current_run_id("")
        assert current_session_key(ctx) == "", "绑定空串是权威空值，不该回退"
        assert current_run_id(ctx) == ""

        set_current_session_key(None)
        set_current_run_id(None)
        assert current_session_key(ctx) == "u:from-state", "解除绑定后应恢复回退"
        assert current_run_id(ctx) == "u:from-state:r0"
    finally:
        set_current_session_key(None)
        set_current_run_id(None)


# ---------------------------------------------------------------------------
# ① 并发两会话：各读各的（核心用例）
# ---------------------------------------------------------------------------

def test_concurrent_requests_keep_their_own_session(tmp_path):
    """同容器内两个并发请求：三个读取点都读到**自己**的会话键。

    编排刻意确定性：A 先写（两通道）→ B 再写（两通道）→ A 才读。于是「A 读的时刻，
    容器级 state 里是 B 的值」被**必然**构造出来——旧实现（只读 state）下 A 必读串，
    这正是本用例作为检测器的意义；ContextVar 实现下 A 仍读到自己的值。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        a_wrote, b_wrote = asyncio.Event(), asyncio.Event()

        async def request_a():
            sk = "u:A"
            # 忠实复刻真实写点：chat.send 与 loop.run 都是「两通道同写」
            set_current_session_key(sk)
            c.root.state[StateKeys.SESSION_KEY] = sk
            a_wrote.set()
            await b_wrote.wait()            # 等 B 覆盖容器级 state
            return _readers(c)

        async def request_b():
            await a_wrote.wait()            # 严格排在 A 写之后
            sk = "u:B"
            set_current_session_key(sk)
            c.root.state[StateKeys.SESSION_KEY] = sk
            b_wrote.set()
            return _readers(c)

        a, b = await asyncio.gather(request_a(), request_b())
        assert a == ("u:A",) * 3, f"会话 A 读串到别处：{a}"
        assert b == ("u:B",) * 3, f"会话 B 读串到别处：{b}"
        await c.teardown()

    _run(s())


def test_run_id_isolated_between_concurrent_requests(tmp_path):
    """归组键（``REACT_RUN_ID``）同走请求作用域通道：并发下账单行不取别人的 run。

    断言走 **budget 的真实读取点**（``_run_id``——账单行 ``run_id`` 的取值处），
    而非光断言原语本身：否则把读点改回共享槽时本用例不会红（实测过这一点）。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        a_wrote, b_wrote = asyncio.Event(), asyncio.Event()

        async def request_a():
            set_current_run_id("u:A:r1")
            c.root.state[StateKeys.REACT_RUN_ID] = "u:A:r1"
            a_wrote.set()
            await b_wrote.wait()
            return budget_run_id(c.root)

        async def request_b():
            await a_wrote.wait()
            set_current_run_id("u:B:r2")
            c.root.state[StateKeys.REACT_RUN_ID] = "u:B:r2"
            b_wrote.set()
            return budget_run_id(c.root)

        a, b = await asyncio.gather(request_a(), request_b())
        assert (a, b) == ("u:A:r1", "u:B:r2"), f"归组键读串：{(a, b)}"
        # 收尾置空（loop.run 两条 return 路径）⇒ 不得被回退分支读回上一轮的 run
        set_current_run_id("")
        assert current_run_id(c.root) == ""
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ② 上下文传播语义（ContextVar 的关键性质）
# ---------------------------------------------------------------------------

def test_bound_scope_propagates_to_child_tasks_by_copy():
    """请求内派生子任务（``create_task``）读到本请求的值；且是**创建瞬间的复制**。

    意义：LLM 工具调用的并发子任务、SSE 的 ``_worker``、emit 的异步监听都靠这条性质
    才拿得到会话归属。
    """
    async def s():
        set_current_session_key("u:A")

        async def child() -> str:
            await asyncio.sleep(0)
            return current_session_key()

        assert await asyncio.create_task(child()) == "u:A"

        # 创建后父任务改绑 ⇒ 已创建的子任务不受影响（复制而非引用）
        set_current_session_key("u:B")
        task = asyncio.create_task(child())
        set_current_session_key("u:C")
        assert await task == "u:B"
        assert current_session_key() == "u:C"

    _run(s())


# ---------------------------------------------------------------------------
# ③ 兼容：未绑定时的回退与「显式置空」的严格性
# ---------------------------------------------------------------------------

def test_unbound_reads_fall_back_to_container_state(tmp_path):
    """未绑定请求作用域 ⇒ 仍读容器级 state（既有调用点与测试写法零改动）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        c.root.state[StateKeys.SESSION_KEY] = "u:A"
        assert current_session_key(c.root) == "u:A"
        assert _readers(c) == ("u:A",) * 3
        await c.teardown()

    _run(s())


def test_explicit_empty_binding_beats_stale_container_state(tmp_path):
    """显式绑定为空串 ⇒ **不得**回退读到容器里的陈旧值。

    「未绑定」与「绑定为空」必须严格区分：run 收尾会显式置空，若把空串也当"未绑定"，
    回退分支就会把上一轮的 run / 会话读回来。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        c.root.state[StateKeys.SESSION_KEY] = "u:A"
        c.root.state[StateKeys.REACT_RUN_ID] = "u:A:r1"
        set_current_session_key("")
        set_current_run_id("")
        assert current_session_key(c.root) == ""
        assert current_run_id(c.root) == ""
        assert budget_session_key(c.root) == ""
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ④ 守卫：入口必须绑定 + 不得出现绕过原语的第二读取点
# ---------------------------------------------------------------------------

def test_request_entry_binds_request_scope():
    """守卫：请求入口（``_identity`` 是唯一漏斗）必须把会话键绑到请求作用域。

    删掉那行**不会**让任何行为测试变红——读取端会静默回退容器级 state（= 悄悄退回 R1
    缺陷）。故把「入口必须绑定」变成可机检约束（G1 教训：文档承诺要落到断言上）。
    """
    src = (AGENT / "run" / "chat.py").read_text(encoding="utf-8")
    start = src.index("def _identity(")
    end = src.index("\n    def ", start + 1)
    assert "set_current_session_key(" in src[start:end], \
        "请求入口 _identity 必须调用 set_current_session_key（R1：唯一漏斗）"
    # 生产侧另两处写点也须同步绑定（覆盖非 HTTP 路径，如 --once 直连 chat.send）
    for rel in ("sys/chat.py", "plugins/react/loop.py"):
        text = (AGENT / rel).read_text(encoding="utf-8")
        assert "set_current_session_key(" in text, f"{rel} 未绑定请求作用域"


def test_session_key_has_no_second_read_path():
    """守卫：除原语与写点外，生产代码不得再直接读容器级会话键（防 R1 悄悄退化）。"""
    offenders: list[str] = []
    for sub in ("sys", "plugins", "run"):
        for path in sorted((AGENT / sub).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for i, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if "StateKeys.SESSION_KEY" not in line:
                    continue
                code = line.split("#", 1)[0]
                if "state" not in code:
                    continue                    # 纯文档/注释里点名，不是读取
                if re.search(r"\]\s*=[^=]", code):
                    continue                    # 写点：ctx.state[KEY] = …
                offenders.append(f"{path.relative_to(AGENT)}:{i}: {code.strip()}")
    assert not offenders, (
        "会话键存在绕过原语的直读点（应改用 current_session_key(ctx)）："
        f"{offenders}")
