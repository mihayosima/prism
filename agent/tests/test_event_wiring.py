"""P3 测试：订阅/发布一致性守卫（capability 接缝接线真发生）。

两路验证：
- 静态（AST）：扫描 agent 源码，断言每个 required seam 既有发布点又有订阅点，
  ctx.on(Events.X) 的 X 必为真实常量。
- 运行时：build_agent().assemble() 真实触发装配期校验；并单独验证
  EventBus.waterfall 对 required seam 0 监听即抛（运行期纵深守卫）。

AST 计数口径（固定，防误算）：
  发布点 = emit / waterfall / publish / durable
           —— durable 内部即 emit，事件值位于**第 2** 个参数；其余位于**第 1** 个参数。
  订阅点 = on —— 事件值位于**第 1** 个参数。
  仅 `Events.<CONST>` 形式计入（P1 已强制；不处理 `import Events as E` 之别名，超时由运行时校验兜底）。
"""

import asyncio
import ast
import pathlib

import pytest

from agent.api import Container, EventBus, Events
from agent.core.errors import EventWiringError
from agent.core.events_catalog import Events as _Events, REQUIRED_SUBSCRIBER_EVENTS
from agent.sys.events_plugin import EventsPlugin

AGENT_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _run(coro):
    return asyncio.run(coro)


def _const_value(node) -> str | None:
    """若 node 是 Events.<CONST> 属性访问，返回其常量值；否则 None。"""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "Events":
        return getattr(_Events, node.attr, None)
    return None


def _scan():
    publishes: set[str] = set()
    subscribes: set[str] = set()
    on_consts: set[str] = set()
    for py in AGENT_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # 发布点：emit / waterfall / publish（事件值=第 1 参）
            if isinstance(func, ast.Attribute) and func.attr in ("emit", "waterfall", "publish"):
                v = _const_value(node.args[0]) if node.args else None
                if v is not None:
                    publishes.add(v)
            # 发布点：durable(ctx, event, payload)（事件值=第 2 参）
            elif isinstance(func, ast.Name) and func.id == "durable" and len(node.args) >= 2:
                v = _const_value(node.args[1])
                if v is not None:
                    publishes.add(v)
            # 订阅点：on(event, handler)（事件值=第 1 参）
            elif isinstance(func, ast.Attribute) and func.attr == "on" and node.args:
                v = _const_value(node.args[0])
                if v is not None:
                    subscribes.add(v)
                    on_consts.add(v)
    return publishes, subscribes, on_consts


def test_required_seams_have_publisher_and_subscriber():
    publishes, subscribes, _ = _scan()
    for seam in REQUIRED_SUBSCRIBER_EVENTS:
        assert seam in publishes, f"{seam} 无发布点（缺 waterfall/emit/durable）"
        assert seam in subscribes, f"{seam} 无订阅点（缺 ctx.on(Events.X)）"


def test_on_references_real_constants():
    _, _, on_consts = _scan()
    valid = {v for k, v in vars(_Events).items() if not k.startswith("_") and isinstance(v, str)}
    assert on_consts <= valid, f"ctx.on 引用了非 Events 常量：{on_consts - valid}"


def test_assemble_verifies_wiring():
    from agent.profiles import build_agent
    c = build_agent()                       # 默认 verify_wiring=True
    _run(c.assemble())
    bus = c.root.get("events")
    # LLM_PRE_CALL 由 guard + budget 挂（≥1）；TASKS_PRE_EXECUTE 由 guard 挂（≥1）
    assert len(bus.listeners(Events.LLM_PRE_CALL)) >= 1
    assert len(bus.listeners(Events.TASKS_PRE_EXECUTE)) >= 1


def test_wiring_verify_detects_missing_subscriber():
    # 仅装配 events（无 guard/budget）→ required seam 0 监听 → 装配期即崩
    c = Container(verify_wiring=True)
    c.add(EventsPlugin)
    with pytest.raises(EventWiringError):
        _run(c.assemble())


def test_waterfall_required_seam_no_listener_raises():
    # 运行期纵深守卫：required seam 0 监听，分发瞬间即抛
    bus = EventBus(required_seams={Events.LLM_PRE_CALL})
    with pytest.raises(EventWiringError):
        _run(bus.waterfall(Events.LLM_PRE_CALL, {}))
