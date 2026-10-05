"""run/chat.py 模块级纯函数单测（2026-10-04：把 HTTP 层 42% 覆盖率往上推的第一批）。

为什么单独成文件：``run/chat.py`` 是**全仓最大 & 覆盖率最低**（42%）的文件，而它的
低覆盖根因是 ``_serve()`` 巨型闭包工厂——handler 靠闭包捕获状态，必须起真实 uvicorn 才能测。

本文件**绕开**这个约束：只测那些**不依赖闭包**的模块级函数。它们正是「改正逻辑后
无需起服务即可回归」的部分，也是设置页/会话准入/安全头这几条用户可感路径的判定核心。

★ 刻意**不**测：``_serve`` / ``_register_*`` 内部的 handler（需真实容器 + 请求上下文，
见 test_run_http_resilience.py 的真 uvicorn 夹具）。

schema 相关断言一律**反射** ``_SETTING_SCHEMA`` 取键，避免设置项改名/增删时本文件碎裂。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from agent.run.chat import (
    _acquire_session_run,
    _coerce_setting,
    _domain_options,
    _req_rid,
    _security_headers_middleware,
    _session_busy,
    _SETTING_SCHEMA,
)


def _run(coro):
    return asyncio.run(coro)


def _req(path: str = "/api/x", rid: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        state=types.SimpleNamespace(rid=rid),
        url=types.SimpleNamespace(path=path),
    )


class _Resp:
    """最小响应替身：只需支持 ``headers.setdefault`` / ``in`` / ``[]=``。"""

    def __init__(self, headers: dict | None = None):
        self.headers = dict(headers or {})


def _next(resp: _Resp | None = None):
    """``call_next`` 替身：Starlette 的 call_next 是**协程函数**，必须返回 awaitable。"""
    async def call_next(_request):
        return resp if resp is not None else _Resp()
    return call_next


# ---------------------------------------------------------------- 设置项校验

def _key_of_kind(kind: str) -> str:
    for k, v in _SETTING_SCHEMA.items():
        if v[0] == kind:
            return k
    raise AssertionError(f"_SETTING_SCHEMA 中没有 {kind} 类型的设置项，测试需更新")


def test_coerce_setting_bool_accepts_chinese_and_english():
    k = _key_of_kind("bool")
    assert _coerce_setting(k, True) is True
    assert _coerce_setting(k, False) is False
    for truthy in ("1", "true", "on", "yes", "是", "开", " ON "):
        assert _coerce_setting(k, truthy) is True, truthy
    for falsy in ("0", "false", "off", "no", "否", "关", ""):
        assert _coerce_setting(k, falsy) is False, falsy


def test_coerce_setting_choice_rejects_unknown():
    k = _key_of_kind("choice")
    allowed = _SETTING_SCHEMA[k][1]
    assert _coerce_setting(k, allowed[0]) == allowed[0]
    with pytest.raises(ValueError):
        _coerce_setting(k, "__definitely_not_a_choice__")


def test_coerce_setting_list_normalizes_separators():
    k = _key_of_kind("list")
    # 中英文逗号/顿号都要能切，且去空白、去空项
    assert _coerce_setting(k, "a，b、c, d") == ["a", "b", "c", "d"]
    assert _coerce_setting(k, ["a", " b "]) == ["a", "b"]
    assert _coerce_setting(k, "") == []
    # ★ 回归：None 必须当空，不能变成字面量 "None" 幽灵条目
    assert _coerce_setting(k, None) == []


def test_coerce_setting_numeric_clamps_range():
    keys = [k for k, v in _SETTING_SCHEMA.items()
            if v[0] not in ("bool", "choice", "list")]
    assert keys, "schema 里应有数值型设置项"
    k = keys[0]
    _typ, lo, hi = _SETTING_SCHEMA[k]
    assert _coerce_setting(k, lo) == lo
    assert _coerce_setting(k, hi) == hi
    with pytest.raises(ValueError):
        _coerce_setting(k, hi + 1)
    with pytest.raises(ValueError):
        _coerce_setting(k, lo - 1)


def test_coerce_setting_unknown_key_fails_fast():
    with pytest.raises(ValueError):
        _coerce_setting("__no_such_setting__", 1)


# ---------------------------------------------------------------- 工具域清单

def test_domain_options_counts_and_sorts():
    tools = [{"owner": "b"}, {"owner": "a"}, {"owner": "a"}, {"owner": ""}]
    opts = _domain_options(tools, [])
    assert opts == [{"name": "a", "count": 2, "available": True},
                    {"name": "b", "count": 1, "available": True}]


def test_domain_options_keeps_selected_but_unavailable():
    """已勾选但插件当前停用的域必须保留（否则用户的选择会静默消失）。"""
    opts = _domain_options([{"owner": "a"}], ["a", "ghost"])
    ghost = [o for o in opts if o["name"] == "ghost"]
    assert ghost and ghost[0]["available"] is False and ghost[0]["count"] == 0
    assert [o for o in opts if o["name"] == "a"][0]["available"] is True


def test_domain_options_tolerates_bare_string():
    """裸字符串要包成单元素——否则会被逐字符迭代成一堆单字候选项。"""
    assert [o["name"] for o in _domain_options([], "abc")] == ["abc"]
    assert _domain_options([], None) == []


# ---------------------------------------------------------------- 请求标识

def test_req_rid_prefers_request_state_over_contextvar():
    """★ 中间件异常处理器里 ContextVar 会回退空串 ⇒ 必须以 request.state 为准。"""
    assert _req_rid(_req(rid="R-1")) == "R-1"


def test_req_rid_falls_back_to_contextvar():
    from agent.api import bind_request_id

    async def s():
        bind_request_id("R-2")
        return _req_rid(_req(rid=""))
    assert _run(s()) == "R-2"


# ---------------------------------------------------------------- 安全响应头

def test_security_headers_added_for_api_paths():
    async def s():
        return await _security_headers_middleware(_req("/api/events"), _next())
    r = _run(s())
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["Content-Security-Policy"] == "default-src 'none'"


def test_security_headers_api_only_for_data_plane():
    """静态资源不该被套上 no-store / CSP（否则前端产物每次都重新下载）。"""
    async def s():
        return await _security_headers_middleware(_req("/index.html"), _next())
    r = _run(s())
    assert r.headers["X-Content-Type-Options"] == "nosniff"   # 通用头仍然加
    assert "Cache-Control" not in r.headers
    assert "Content-Security-Policy" not in r.headers


def test_security_headers_does_not_overwrite_existing():
    """setdefault 语义：上游已设的头不被覆盖（避免把运维显式配置改掉）。"""
    async def s():
        return await _security_headers_middleware(
            _req("/api/x"), _next(_Resp({"Cache-Control": "max-age=60"})))
    r = _run(s())
    assert r.headers["Cache-Control"] == "max-age=60"


# ---------------------------------------------------------------- 会话准入

def test_session_busy_finds_owner():
    active = {"r1": (object(), "s1"), "r2": (object(), "s2")}
    assert _session_busy(active, "s2") == "r2"
    assert _session_busy(active, "nobody") is None
    assert _session_busy(active, "") is None      # 空会话键 ⇒ 不查（无隔离语义）


def test_session_busy_skips_malformed_entry():
    """形状异常的登记项不得让入口 500（脏数据容错，不是主路径）。"""
    active = {"bad": ("not-a-tuple",), "ok": (object(), "s1")}
    assert _session_busy(active, "s1") == "ok"


def test_acquire_session_run_is_atomic_under_concurrency():
    """★ H2-TOCTOU：并发抢同一会话，只能有一个拿到 ok。

    检测器型：若把 ``_RUN_LOCK`` 去掉（退化为「先查忙、再登记」），
    多个协程会双双通过忙检 ⇒ 断言失败。
    """
    async def s():
        active: dict = {}
        got = await asyncio.gather(*[_acquire_session_run(active, "s1")
                                     for _ in range(50)])
        ok = [g for g in got if g[0] == "ok"]
        busy = [g for g in got if g[0] == "busy"]
        assert len(ok) == 1, f"并发 50 应只有 1 个 ok，实际 {len(ok)}"
        assert len(busy) == 49
        # 不同会话互不干扰
        other = await _acquire_session_run(active, "s2")
        assert other[0] == "ok" and other[1] != ok[0][1]
        for _task, _owner in active.values():
            _task.cancel()
        await asyncio.sleep(0)
    _run(s())
