"""``agent/tests`` 全局夹具（2026-10-06 提取；此前**全仓零 conftest**）。

背景
----
请求作用域的标识走 ContextVar 传递，**按 asyncio task 隔离**——这既是它
能胜任并发请求隔离的原因，也是它会**顺着上下文泄漏**到后续用例/文件的原因
（一个 HTTP 请求 = 一个 task，task 创建时复制当前上下文）。

此前复位逻辑在 4 个测试文件里**各写一遍**，且名字不一致
（``_reset_session_scope`` / ``_isolate_session_scope`` / ``_isolate_request_scope``）；
更关键的是它们**只复位了会话键**——``run id`` 与 ``request id`` 从未复位，
是长期存在的隔离缺口（本次一并补齐）。

为什么 autouse 且对全目录生效
----------------------------
泄漏的表现是「没绑会话却读到上一个用例的值」这类**跨用例串扰**，它只会
**掩盖**真实缺陷（测试靠脏状态变绿），几乎不会凭空造出红灯 ⇒ 宁可复位得
更彻底，也不要靠"当前用例恰好不写这些键"侥幸成立。

刻意不复位 ``CURRENT_PLUGIN``（``core/context.py``）
-------------------------------------------------
它由容器用 **token 配对**、在 ``finally`` 里精确复位
（``container.py`` 的 ``assemble``/``start``）。全局强行复位反而会破坏其
配对语义（``reset`` 需要 token），且它本就已被正确管理 ⇒ 不在此处干预。

复位语义（务必与生产侧一致，勿简化）
----------------------------------
* ``set_current_session_key(None)`` / ``set_current_run_id(None)``：
  ``None`` = **解除绑定**（回到"未绑定" ⇒ 读取回退 ``ctx.state``）。
  ★ 不可写成 ``set_current_run_id("")``——空串是**权威空值**，与"未绑定"严格区分。
* ``bind_request_id("")``：``""`` = **显式解除绑定**。
  ★ 不可省略参数——``bind_request_id(None)`` 的语义是**新生成一个 id**，
    那等于"复位"时又绑了个新值，正好南辕北辙。
"""

from __future__ import annotations

import pytest

from agent.core.obs import bind_request_id
from agent.core.session import set_current_run_id, set_current_session_key


@pytest.fixture(autouse=True)
def _reset_request_scope():
    """每个用例前后，解除全部请求级 ContextVar 绑定。"""
    set_current_session_key(None)
    set_current_run_id(None)
    bind_request_id("")
    yield
    set_current_session_key(None)
    set_current_run_id(None)
    bind_request_id("")
