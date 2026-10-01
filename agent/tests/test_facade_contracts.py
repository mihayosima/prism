"""跨插件门面契约守卫 —— 让「提供方改名」在测试期即红，不再等运行期才断。

背景（2026-10-01 解耦性复核 §P3-4）：

``core/contract.py`` 只有两个 Protocol，其中**只有 ``ReactResultLike`` 被消费方真正引用**
（``sys/chat.py``）。其余跨插件门面全是无类型调用：

| 消费方 → 提供方                              | 调用面                                                    |
|---|---|
| ``sys/chat`` → memory                         | ``ctx.memory.remember`` / ``ctx.memory.context``          |
| ``sys/chat`` → react                          | ``ctx.react.run``（已有 ``ReactResultLike``）             |
| ``sys/chat`` / ``react/loop`` / ``run/chat`` → context | ``gov.assess`` / ``gov.memory_budget`` / ``gov.ledger`` |
| ``react/loop`` / ``run/chat`` → work_memory   | ``wm.begin/get/update/checkpoint/finish/list_active``     |

更尴尬的是 ``GovernorLike``（``plugins/context/protocols.py``）与 ``WorkMemoryLike``
（``plugins/work_memory/backend.py``）**都定义了 Protocol，但定义在提供方插件内** ——
消费方因 import 红线引用不到，只能当注释看 ⇒ 提供方改名时测试不红、运行期才断。

本测试与既有 ``ReactResultLike`` 守卫（``test_memory_seams``）同构：在**测试侧**
（不受分层红线约束）import 提供方类，断言消费方实际调用的门面成员**仍然存在**；
并反向核对调用点仍在源码里，防登记表腐烂成一堆永真的死断言。
"""

from __future__ import annotations

import re
from pathlib import Path

# 注意：``ctx.memory`` 的门面对象**不是** ``@plugin`` 类 ``Memory``，而是它在
# ``apply()`` 里 ``ctx.provide(Services.MEMORY, driver)`` 交出去的 ``MemoryDriver``
# —— 登记表必须指向真正的门面类，否则守卫守在空处。
from agent.plugins.context import ContextGovernor
from agent.plugins.memory import MemoryDriver
from agent.plugins.work_memory import WorkMemory

AGENT = Path(__file__).resolve().parent.parent          # agent/


def _unwrap(obj):
    """``@plugin`` 装饰后模块级拿到的是 ``Plugin`` 实例（类属性取不到），``_raw`` 才是原始类。"""
    return obj if isinstance(obj, type) else obj._raw


# 接缝登记表：插件 id -> (提供方类, 消费方调用的门面成员, 调用点文件, 接收者正则)
SEAMS: dict[str, tuple[object, set[str], tuple[str, ...], str]] = {
    "plugins.memory": (
        MemoryDriver,
        {"remember", "context"},
        ("sys/chat.py",),
        r"ctx\.memory\.",
    ),
    "plugins.context": (
        ContextGovernor,
        {"assess", "memory_budget", "ledger"},
        ("sys/chat.py", "plugins/react/loop.py", "run/chat.py"),
        r"\bgov\.",
    ),
    "plugins.work_memory": (
        WorkMemory,
        {"begin", "get", "update", "checkpoint", "finish", "list_active"},
        ("plugins/react/loop.py", "run/chat.py"),
        r"\bwm\.",
    ),
}


def test_facade_members_still_exist():
    """提供方门面成员必须仍在 —— 改名/删除即红（等价于把 Protocol 挪到可引用处）。"""
    bad: list[str] = []
    for plug, (provider, members, _, _) in sorted(SEAMS.items()):
        cls = _unwrap(provider)
        missing = {m for m in members if not callable(getattr(cls, m, None))}
        if missing:
            bad.append(f"{plug}: {cls.__name__} 缺少门面成员 {sorted(missing)}")
    assert not bad, (
        "跨插件门面契约被破坏（消费方只能运行期才断，须同步改调用点或补回成员）:\n  "
        + "\n  ".join(bad))


def test_registered_seams_are_still_called():
    """反向核对：登记的成员确实还被消费方**以登记接收者**调用。

    防两件事：① 登记表腐烂成永真的死断言；② 消费方换了接收者/换走门面，守卫却仍在旧处打转。
    """
    uncalled: list[str] = []
    for plug, (_, members, where, recv) in sorted(SEAMS.items()):
        blob = "\n".join((AGENT / w).read_text(encoding="utf-8") for w in where)
        for m in sorted(members):
            if not re.search(rf"{recv}{re.escape(m)}\b", blob):
                uncalled.append(f"{plug}: 未找到 {recv}{m} 调用点（于 {where}）")
    assert not uncalled, (
        "接缝登记表已腐烂（登记的门面调用已不存在，守卫形同虚设，请更新 SEAMS）:\n  "
        + "\n  ".join(uncalled))
