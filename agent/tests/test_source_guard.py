"""S4 源头护栏测试（``core.tokens.guard_source`` × ``sys.tasks.invoke``）——

覆盖验收清单：

- **原语**：未超限零损耗（同一对象）· ``None``/``<=0`` 不裁（与 ``clip_text`` **有意相反**）·
  dict 形态规则（structured 优先、让位通道清空为 None 且保留键）· 保留通道非空才让位 ·
  定点硬裁实占 <= 上限 · 幂等 · ``omitted_tokens`` = 净减少量；
- **接线**：有治理器则裁（事件带遥测）· 无治理器不裁不崩 · 小结果零损耗；
- **健壮**：``guard_source`` 抛异常时 ``invoke`` 仍返回**原值**且 ``ok=True``
  （护栏不得成为新的失败点）；
- **嵌套**：内层 ``invoke`` 已裁 → 外层再调 ``omitted == 0``（不重复裁）；
- **口径守卫**：非测试源码内不得残留旧**字符口径**（配置键 / 截断标注）。

全部离线确定性（fixture provider），不触网。
"""

from __future__ import annotations

import asyncio
import io
import re
import tokenize
from pathlib import Path

from agent.api import Events, Services
from agent.core.tokens import clip_text, clip_text_hard, estimate_tokens, guard_source
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

AGENT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = AGENT / "plugins"
BIG = "X" * 40000                     # ≈10000 token（远超 8192 默认上限）


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path, *, context: bool = True) -> PluginManager:
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.context", context)
    return pm


def _build(tmp_path, *, context: bool = True, patch: dict | None = None):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=_pm(tmp_path, context=context))


def _capture_post(ctx) -> list[dict]:
    """订阅 ``agent/tasks/post-execute``，收集载荷（同步监听 → ``emit`` 当场执行）。"""
    got: list[dict] = []

    def _h(*args):
        if args and isinstance(args[0], dict):
            got.append(args[0])

    ctx.get(Services.EVENTS).on(ctx, Events.TASKS_POST_EXECUTE, _h)
    return got


# ---------------------------------------------------------------------------
# 原语：guard_source 语义
# ---------------------------------------------------------------------------


def test_guard_source_passthrough_when_under_limit():
    """未超限 → 原样返回（小结果零损耗，**同一对象**），omitted = 0。"""
    small = {"structured": None, "content": [{"type": "text", "text": "hi"}], "text": "hi"}
    out, omitted = guard_source(small, 8192)
    assert out is small and omitted == 0
    s = "短内容"
    assert guard_source(s, 8192)[0] is s


def test_guard_source_none_or_nonpositive_bypasses_but_clip_text_clears():
    """``None`` / ``<=0`` → **不裁**（护栏宁可放过）；与 ``clip_text`` 的清空语义**有意相反**。"""
    for limit in (None, 0, -1):
        out, omitted = guard_source(BIG, limit)
        assert out is BIG and omitted == 0
    # 对照守卫：clip_text <=0 **清空** —— 两者分歧是设计而非疏漏，不得"顺手统一"
    cleared, _ = clip_text(BIG, 0)
    assert cleared == ""


def test_guard_source_dict_prefers_structured_and_clears_other_channels():
    """dict 形态规则：structured 优先；让位通道**清空为 None 且保留键**（防下游 KeyError）。"""
    value = {"structured": {"k": "Y" * 40000},
             "content": [{"type": "text", "text": "Z" * 40000}],
             "text": "Z" * 40000}
    out, omitted = guard_source(value, 200)
    assert set(out) == set(value)                              # 键集合不变
    assert out["content"] is None and out["text"] is None      # 让位通道清空（非删除）
    assert isinstance(out["structured"], str)                  # 保留通道已硬裁为文本
    assert "[...省略" in out["structured"] and "tokens...]" in out["structured"]
    assert estimate_tokens(out) <= 200 and omitted > 0


def test_guard_source_falls_back_to_content_when_text_empty():
    """保留通道非空才让位：``text`` 为空而 ``content`` 有值 → 以 content 为保留通道。"""
    value = {"structured": None, "content": [{"type": "text", "text": "K" * 40000}], "text": ""}
    out, _ = guard_source(value, 200)
    assert out["content"] is not None and out["text"] is None  # 不得两者都清空（防内容全丢）
    assert estimate_tokens(out) <= 200


def test_guard_source_omitted_is_net_reduction():
    """``omitted_tokens`` = 原值 − 裁后（净减少），非 clip_text 的"中段 token 数"。"""
    before = estimate_tokens(BIG)
    out, omitted = guard_source(BIG, 300)
    assert estimate_tokens(out) <= 300
    assert omitted == before - estimate_tokens(out) > 0


def test_clip_text_hard_fills_budget_and_is_stable():
    """定点硬裁须**严格 <= 上限**且**充分用额**，二次再裁无变化（幂等）。

    两个反面做法各有单测价值：不做定点 → 略超上限（实测 8199）；只做二次收缩 →
    大幅超出时过度截断（实测 5 万 token 只剩 1349）。
    """
    for unit in ("abcdefghij ", "中文测试内容 "):
        text = unit * 400
        out, omitted = clip_text_hard(text, 1000)
        t = estimate_tokens(out)
        assert t <= 1000, f"超限：{t} > 1000（标注开销未计入）"
        assert t >= 900, f"欠用预算：{t}/1000"
        assert omitted > 0
        out2, omitted2 = clip_text_hard(out, 1000)
        assert omitted2 == 0 and out2 == out


def test_guard_source_is_idempotent():
    """幂等：对已裁结果再裁 → omitted == 0 且返回**同一对象**。"""
    once, om1 = guard_source(BIG, 500)
    twice, om2 = guard_source(once, 500)
    assert om1 > 0 and om2 == 0 and twice is once


# ---------------------------------------------------------------------------
# 接线：sys/tasks.invoke
# ---------------------------------------------------------------------------


def test_invoke_clips_result_via_source_budget(tmp_path):
    """有治理器：结果在 ``invoke`` 内被裁到 ``source_clip_budget()``，事件带裁剪遥测。"""
    c = _build(tmp_path, patch={"agent": {"context": {"source_clip_tokens": 200}}})

    async def s():
        await c.assemble()
        assert c.root.has(Services.CONTEXT)

        async def _big():
            return "X" * 40000

        c.root.tasks.register(c.root, "test.big", schema={}, handler=_big, owner="test",
                              description="返回超大结果")
        posts = _capture_post(c.root)
        res = await c.root.tasks.invoke(c.root, "test.big", caller="test")
        assert estimate_tokens(res) <= 200 and "[...省略" in res
        assert posts and posts[-1]["clipped"] is True
        assert posts[-1]["omitted_tokens"] > 0 and posts[-1]["limit_tokens"] == 200
        await c.teardown()

    _run(s())


def test_invoke_small_result_is_untouched(tmp_path):
    """小结果零损耗：未超限 → 事件 ``clipped=False``，返回值原样。"""
    c = _build(tmp_path, patch={"agent": {"context": {"source_clip_tokens": 8192}}})

    async def s():
        await c.assemble()

        async def _small():
            return {"ok": "fine"}

        c.root.tasks.register(c.root, "test.small", schema={}, handler=_small, owner="test")
        posts = _capture_post(c.root)
        res = await c.root.tasks.invoke(c.root, "test.small", caller="test")
        assert res == {"ok": "fine"}
        assert posts[-1]["clipped"] is False and posts[-1]["omitted_tokens"] == 0
        await c.teardown()

    _run(s())


def test_invoke_without_context_plugin_does_not_clip(tmp_path):
    """降级矩阵：无 ``plugins.context`` → 不裁、不报错，主链路正常。"""
    c = _build(tmp_path, context=False)

    async def s():
        await c.assemble()
        assert not c.root.has(Services.CONTEXT)

        async def _big():
            return "X" * 40000

        c.root.tasks.register(c.root, "test.big", schema={}, handler=_big, owner="test")
        res = await c.root.tasks.invoke(c.root, "test.big", caller="test")
        assert res == "X" * 40000
        await c.teardown()

    _run(s())


def test_invoke_survives_guard_failure(tmp_path):
    """护栏**不得**成为新的失败点：``guard_source`` 抛异常 → 返回原值、``ok=True``。"""
    c = _build(tmp_path, patch={"agent": {"context": {"source_clip_tokens": 200}}})

    async def s():
        await c.assemble()
        import agent.sys.tasks as tasks_mod

        original = tasks_mod.guard_source

        def _boom(*a, **k):
            raise RuntimeError("guard boom")

        tasks_mod.guard_source = _boom
        try:
            async def _big():
                return "X" * 40000

            c.root.tasks.register(c.root, "test.big", schema={}, handler=_big, owner="test")
            posts = _capture_post(c.root)
            res = await c.root.tasks.invoke(c.root, "test.big", caller="test")
            assert res == "X" * 40000                          # 原值，未被"半裁"
            assert posts[-1]["ok"] is True                     # 任务成功态不受污染
            assert posts[-1]["clipped"] is False and posts[-1]["omitted_tokens"] == 0
        finally:
            tasks_mod.guard_source = original
            await c.teardown()

    _run(s())


def test_nested_invoke_clips_only_once(tmp_path):
    """嵌套 ``invoke``（工具调工具）：内层裁到上限后，外层再调 ``omitted == 0``（不重复裁）。

    注：当前仓内**没有**工具调工具的既有路径（``Task.invoke`` 只收 kwargs），
    此处用闭包持有 ``ctx`` 的合成 handler 锁定该不变量（面向未来的回归守卫）。
    """
    c = _build(tmp_path, patch={"agent": {"context": {"source_clip_tokens": 200}}})

    async def s():
        await c.assemble()

        async def _inner():
            return "N" * 40000

        c.root.tasks.register(c.root, "test.inner", schema={}, handler=_inner, owner="test")

        async def _outer():
            return await c.root.tasks.invoke(c.root, "test.inner", caller="test.outer")

        c.root.tasks.register(c.root, "test.outer", schema={}, handler=_outer, owner="test")
        posts = _capture_post(c.root)
        res = await c.root.tasks.invoke(c.root, "test.outer", caller="test")

        by_task = {p["task"]: p for p in posts}
        assert by_task["test.inner"]["clipped"] is True
        assert by_task["test.inner"]["omitted_tokens"] > 0
        assert by_task["test.outer"]["clipped"] is False       # 已 <= 上限 → 直返，不重复裁
        assert by_task["test.outer"]["omitted_tokens"] == 0
        assert estimate_tokens(res) <= 200
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 口径守卫：旧字符口径不得复活（验收项 8）
# ---------------------------------------------------------------------------

_LEGACY = [
    (re.compile(r"tool_result_max_chars"), "旧字符口径配置键"),
    (re.compile(r"truncated[\s\S]{0,40}?chars\)"), "旧字符截断标注"),
]


def _source_without_comments(fp: Path) -> str:
    """源码去掉**注释**后的 token 串。

    注释允许提及历史（如"旧 X 已删除"），但**代码与字符串**不得残留旧口径——
    故此处用 tokenize 精确剔除 COMMENT，而非行级启发式。
    """
    src = fp.read_text(encoding="utf-8")
    parts: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        parts.append(tok.string)
    return " ".join(parts)


def _scanned_files() -> list[Path]:
    """待扫描源码（**排除 tests 自身**，否则本文件的断言字面量会自触发）。"""
    out: list[Path] = []
    for fp in AGENT.rglob("*.py"):
        parts = fp.parts
        if "__pycache__" in parts or "tests" in parts:
            continue
        rel = fp.relative_to(AGENT)
        if rel.parts[0] in ("core", "sys", "plugins", "run") or fp.name == "api.py":
            out.append(fp)
    return sorted(out)


def test_no_legacy_char_clip_wording_in_source():
    """验收项 8：单条结果上限已统一为 **token 口径的源头护栏**，旧字符口径不得复活。"""
    bad: list[str] = []
    for fp in _scanned_files():
        text = _source_without_comments(fp)
        for pat, why in _LEGACY:
            if pat.search(text):
                bad.append(f"  {fp.relative_to(AGENT)} 残留 {why}（/{pat.pattern}/）")
    assert not bad, "旧字符口径回归（应统一走 core.tokens.guard_source）:\n" + "\n".join(bad)
