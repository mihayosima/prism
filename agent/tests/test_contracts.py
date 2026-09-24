"""契约一致性守卫：事件名单一真相源（SSOT）不可被悄悄破坏。

防止「隐式字符串契约漂移」——这是内核组件间唯一的脆弱耦合点：
- ``Events`` 的每个常量都必须在 agent 源码里被至少一处引用；
  否则即出现「声明了却没人用」的孤儿常量（或拼写漂移）。
- 任何 ``.py`` 源码（catalog / tests 除外）不得再出现裸事件名字面量
  （如 ``emit("agent/llm/pre-call")``），否则即回归到运行期静默失效。

改事件名时：只改 ``agent/core/events_catalog.py`` 一处，引用处因 ``Events.X``
而自动跟随；拼错常量名会在导入/装配期即报 ``AttributeError``。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.api import Events, all_events

ROOT = Path(__file__).resolve().parent.parent  # agent/
CATALOG = ROOT / "core" / "events_catalog.py"

# 任何 `emit("agent/...` / `waterfall("agent/...` / `on("agent/...` /
# `durable(ctx, "agent/...` 形式的裸字面量都算违规（应改用 Events.X）。
LEFTOVER = re.compile(
    r"""(?:emit|waterfall|on)\(\s*["']agent/"""
    r"""|\bdurable\(\s*\w+\s*,\s*["']agent/"""
)


def _py_sources(exclude: set[Path]) -> list[Path]:
    out = []
    for p in ROOT.rglob("*.py"):
        if p in exclude or "__pycache__" in p.parts or "tests" in p.parts:
            continue
        out.append(p)
    return out


def test_events_catalog_nonempty():
    evs = all_events()
    assert len(evs) >= 16, f"事件常量过少: {len(evs)}"


def test_every_event_constant_is_used():
    """每个 Events 常量都必须被源码引用（防止孤儿/漂移）。"""
    names = [k for k in vars(Events).items() if not k[0].startswith("_")]
    names = [k for k, _ in names]
    blob = "\n".join(p.read_text(encoding="utf-8") for p in _py_sources({CATALOG}))
    unused = [
        n for n in names
        if f"Events.{n}" not in blob and f'Events["{n}"]' not in blob
    ]
    assert not unused, f"以下 Events 常量未被任何源码引用（应改用或删除）：{unused}"


def test_no_leftover_event_literals():
    """禁止裸事件名字面量出现在 emit/durable/waterfall/on 调用中。"""
    offenders = []
    for p in _py_sources({CATALOG}):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if LEFTOVER.search(line):
                offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "发现裸事件名字面量（应改用 Events.X）：\n" + "\n".join(offenders)
    )


def test_api_exports_events():
    from agent.api import __all__ as API_ALL

    assert "Events" in API_ALL
    assert "all_events" in API_ALL
