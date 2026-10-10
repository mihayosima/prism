"""P1-1 无进展检测 —— 检测器型破坏实验（零触网，fixture LLM）。

设计纪律（主方案 §6 / 实施细节 §3.5）：**检测器型测试须「破坏实验验证」**——
删掉检测器（或把阈值设极大）则用例 A 失败，证明其真生效，而非恒绿断言。

三用例：
- A（启用）：fixture「触顶」脚本每轮同参 ``test.echo`` ⇒ 检测器提前终止，
  ``steps < max_steps``、``reason`` 含「无进展」、``REACT_EXHAUSTED`` 落库 reason 一致
  （断言穿过真实读取点）。
- B（关闭）：``no_progress_repeat=999`` ⇒ 退化到烧满 ``max_steps``，
  ``reason`` 以「已达步数上限」开头。
- C（无误伤）：fixture「两步链路」正常收敛（``steps`` 小、``reason is None``、不触发检测）。

手法与 ``test_multisession.py`` / ``test_subagent_plugin.py`` 同构：``build_agent``
（默认 fixture provider，零触网）+ 注册 ``test.echo`` + 直接驱动 ``c.root.react.run``。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.api import Events, make_identity
from agent.plugins.react.loop import ReactConfig, ReactLoop
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

# fixture「触顶」脚本：只要 prompt 含「触顶」，每轮都返回同参 test.echo({"text":"loop"})
_FIXTURE_PROMPT_LOOP = "请执行触顶压力测试：反复调用 test.echo 回显同一参数"


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path):
    PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.react", True)
    pm.set_enabled("plugins.work_memory", True)
    return pm


def _build(tmp_path, *, no_progress_repeat: int = 3, max_steps: int = 8):
    return build_agent(
        config={"db_path": str(tmp_path / "agent.db")},
        plugin_mgr=_pm(tmp_path),
        # agent.react 子配置可经 patch 注入（test_followup_topics 已验证通过校验）
        patch={"agent": {"react": {"no_progress_repeat": no_progress_repeat,
                                   "max_steps": max_steps}}})


async def _echo_handler(text: str = "hi"):
    return {"echo": text}


#: 恒定返回的探针 —— 模拟 `tool_search` 返回**高度相似候选列表**的真实情形：
#: 参数不同但结果文本相同 ⇒ 旧（调用级）判据会误判"观察高度重复"。
async def _same_handler(q: str = ""):
    return {"matches": ["a.py", "b.py", "c.py"], "hint": "候选 1/2/3"}


def _register_echo(ctx) -> None:
    ctx.tasks.register(
        ctx, "test.echo",
        schema={"text": {"type": "str", "required": False, "default": "hi"}},
        handler=_echo_handler, owner="plugins.testtools",
        description="回显参数", risk="read")
    ctx.tasks.register(
        ctx, "test.same",
        schema={"q": {"type": "str", "required": False, "default": ""}},
        handler=_same_handler, owner="plugins.testtools",
        description="恒定返回同一结果（模拟检索类工具的相似候选列表）", risk="read")


async def _exhausted_reason(ctx) -> str | None:
    """穿过真实读取点：从 agent_events 读 REACT_EXHAUSTED 落库的 reason。"""
    rows = await ctx.db.query(
        ctx,
        "SELECT payload FROM agent_events WHERE event=? ORDER BY seq DESC LIMIT 1",
        (Events.REACT_EXHAUSTED,))
    if not rows:
        return None
    return (json.loads(rows[-1]["payload"]) or {}).get("reason")


def test_no_progress_detects_loop_and_stops_early(tmp_path):
    """A（启用）：同参死循环 ⇒ 提前终止（steps<max_steps）、reason 含「无进展」、
    REACT_EXHAUSTED 落库 reason 一致。

    破坏实验判据：删掉 ``_detect_no_progress`` 调用（或设 no_progress_repeat=999）
    则本例会烧满 8 步、reason 变「已达步数上限」→ 本断言失败，证明检测器真生效。
    """
    c = _build(tmp_path, no_progress_repeat=3, max_steps=8)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        ident = make_identity(tenant="t1", user="u1", session="sessA")
        res = await c.root.react.run(
            c.root, _FIXTURE_PROMPT_LOOP,
            session_id=ident.session_key, identity=ident)
        db_reason = await _exhausted_reason(c.root)
        await c.teardown()
        return res, db_reason

    res, db_reason = _run(s())

    assert res.exhausted is True
    assert res.steps < 8, f"检测器未提前终止（steps={res.steps}）"
    assert res.reason and "无进展" in res.reason, res.reason
    assert db_reason and "无进展" in db_reason, db_reason   # 穿过真实读取点
    assert res.reason == db_reason                            # 内存与落库一致


def test_no_progress_disabled_degrades_to_max_steps(tmp_path):
    """B（关闭）：no_progress_repeat=999 ⇒ 烧满 max_steps，reason=已达步数上限。

    与 A 共用同一 fixture 脚本：唯一变量是阈值 → 证明 A 的提前终止确由检测器贡献，
    而非 fixture 脚本自身结束（脚本永不收敛）。
    """
    c = _build(tmp_path, no_progress_repeat=999, max_steps=8)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        ident = make_identity(tenant="t1", user="u1", session="sessB")
        res = await c.root.react.run(
            c.root, _FIXTURE_PROMPT_LOOP,
            session_id=ident.session_key, identity=ident)
        db_reason = await _exhausted_reason(c.root)
        await c.teardown()
        return res, db_reason

    res, db_reason = _run(s())

    assert res.steps == 8, f"关闭后不应提前终止（steps={res.steps}）"
    assert (res.reason and res.reason.startswith("已达步数上限")), res.reason
    assert db_reason and db_reason.startswith("已达步数上限"), db_reason


def test_no_progress_no_false_positive_on_legit_flow(tmp_path):
    """C（无误伤）：fixture「两步链路」正常收敛（steps 小、reason is None、不触发检测）。"""
    c = _build(tmp_path, no_progress_repeat=3, max_steps=8)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        ident = make_identity(tenant="t1", user="u1", session="sessC")
        res = await c.root.react.run(
            c.root, "两步链路：先回显再总结",
            session_id=ident.session_key, identity=ident)
        await c.teardown()
        return res

    res = _run(s())

    assert res.ok is True
    assert res.exhausted is False, "正常收敛任务不应被标记为触顶"
    assert res.reason is None, f"正常收敛不应有 reason（reason={res.reason}）"
    assert res.steps < 8


# 破坏实验（检测器型测试纪律）：用例 A（启用）与用例 B（no_progress_repeat=999＝关闭）
# 共用同一 fixture「触顶」刺激，唯一变量是阈值——A 提前终止、B 烧满 max_steps。
# 这正是「删掉检测器 ⇒ A 的提前终止断言失败」的可执行证明（无需 monkeypatch 即可隔离
# 检测器贡献；monkeypatch 版因本机 sqlite 连接时序偶发 _conn=None，已弃用）。

# ---- D（回归）：一步内多调用不得误伤（2026-10-10 真模型实测缺陷）----

def test_no_progress_no_false_positive_on_multi_call_single_step(tmp_path):
    """D（回归·误伤）：**一步内 4 个不同参调用**不该被判无进展。

    缺陷实录（真模型基线）：``observed`` 按**工具调用**累积，而启发式2 把它当**步**切片
    ⇒ 单步内多个 ``tool_search``（检索不同关键词、返回高度相似的候选文本）即可凑满
    repeat=3，实测报出 ``steps=1`` 的"最近 3 步观察结果高度重复"——任务刚起步就被砍。

    ★ 参数**必须各不相同**（否则命中**启发式1** 的真死循环，那是正确拦截，会掩盖缺陷）；
    且返回内容**必须相同**（模拟 ``tool_search`` 的相似候选列表）——否则旧判据也不命中。
    这两点都是实测踩出来的：初版 fixture 用 ``test.echo`` 回显不同参数 ⇒ 内容各异 ⇒
    **破坏实验全绿**（压根测不到缺陷）；改用 ``test.same`` 恒定返回后破坏实验才转红。

    破坏实验判据：把调用点改回不传 ``step_fps``（等价于修复前行为）⇒ 4 条同内容观察
    一次凑满 3 条 ⇒ 启发式2 命中 ⇒ ``res.exhausted`` 变 True ⇒ 本断言失败。
    修复后 4 个调用只占 1 条步级指纹 ⇒ 正常收敛。
    """
    c = _build(tmp_path, no_progress_repeat=3, max_steps=8)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        ident = make_identity(tenant="t1", user="u1", session="sessD")
        res = await c.root.react.run(
            c.root, "多调用：单步内并行回显同一参数",
            session_id=ident.session_key, identity=ident)
        await c.teardown()
        return res

    res = _run(s())

    assert res.exhausted is False, (
        f"单步多调用被误判为无进展（reason={res.reason}）")
    assert res.reason is None, f"正常收敛不应有 reason（reason={res.reason}）"
    assert res.ok is True


def test_step_fp_is_step_level_not_call_level():
    """单元级钉死判据单位：一步内 N 个调用 ⇒ 只产出**1 条**步级指纹。

    这是误伤修复的最小不变式：``_step_fp`` 若退回调用级，用例 D 就会失败。
    """
    from agent.plugins.react.components import ToolResult

    same = [ToolResult(name="t", content="X", tool_call_id=str(i)) for i in range(4)]
    fp = ReactLoop._step_fp(same)
    assert isinstance(fp, str) and fp != ""
    # 一步内 4 个**同内容**调用 ⇒ 指纹里出现 4 段（贡献齐全），但整体只是**一条**
    assert fp.count("t:") == 4, "指纹应包含全部调用的贡献（内容哈希）"
    # 无调用 ⇒ None（调用方跳过该步，空串不得被当成"内容相同"）
    assert ReactLoop._step_fp([]) is None
    # 不同工具名即使内容相同，指纹也不同（防"两个空查询"被误判）
    a = ReactLoop._step_fp([ToolResult(name="x", content="same")])
    b = ReactLoop._step_fp([ToolResult(name="y", content="same")])
    assert a != b


def test_detect_no_progress_requires_consecutive_steps():
    """启发式2 须**连续 N 步**结果相同；中间隔空步不算重复。

    防止把"非连续的两次相同"误判为原地打转。
    """
    cfg = ReactConfig(no_progress_window=5, no_progress_repeat=3)
    sig = [("t", "1")] * 3          # 启发式1 不参与：指纹各不同即可
    sig = [("t", str(i)) for i in range(3)]
    # 3 步全同 ⇒ 命中
    hit = ReactLoop._detect_no_progress(sig, [], cfg, ["S", "S", "S"])
    assert hit and "无进展" in hit
    # 中间有 None（空步）⇒ 不算连续重复
    none_mid = ReactLoop._detect_no_progress(sig, [], cfg, ["S", None, "S"])
    assert none_mid is None, f"隔空步不应判为连续重复（got={none_mid}）"
    # 内容不同 ⇒ 不命中
    diff = ReactLoop._detect_no_progress(sig, [], cfg, ["S", "T", "S"])
    assert diff is None

