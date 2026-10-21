"""run 层韧性守卫 —— R2 / R3 修复的回归测试（2026-10-02 复核后新增）。

为什么要有这组测试：

- **R2**：``ConfigStore`` 遇到损坏的覆盖文件必须**转存留证**再按空配置处理。
  旧实现静默返回空，而 ``_save`` 的备份只覆盖 v1 分支——文件是 v2 时
  ``is_v1`` 为 False ⇒ **不备份就被空数据覆盖**，用户全部会话级覆盖不可恢复。
- **R3**：``run/chat`` 此前未注册任何 ``exception_handler`` ⇒ 非 HTTPException 的
  未捕获异常返回 **HTML** 500，前端 ``res.json()`` 直接解析失败且无留痕。

R3 的测试取的是**真实 app**（跑真 ``_serve``，经 ``uvicorn.Server.serve`` 拦截），
不是像 ``test_b3_auth_cors`` 那样复刻中间件栈——复刻只能证明"这个写法可行"，
证明不了"生产代码里确实生效"。
"""

from __future__ import annotations

import asyncio
import json
import signal

import pytest

from agent.run.config_store import V2, ConfigStore


# ---------------------------------------------------------------------------
# R2：配置覆盖文件损坏必须转存留证
# ---------------------------------------------------------------------------

def test_corrupt_config_is_quarantined_not_silently_dropped(tmp_path):
    """JSON 解析失败 → 转存 ``*.corrupt`` + 按空配置处理 + 原文件移走。"""
    p = tmp_path / "agent_plugin_config.json"
    p.write_text("{ this is not json ", encoding="utf-8")

    cs = ConfigStore(p)

    assert cs._data == {"version": V2, "container": {}, "sessions": {}}
    bak = p.with_suffix(".corrupt")
    assert bak.exists(), "损坏文件必须被转存留证（否则后续写入即永久丢失）"
    assert bak.read_text(encoding="utf-8") == "{ this is not json "
    assert not p.exists(), "损坏文件应被移走，避免被空配置覆盖后无从恢复"


def test_corrupt_v2_file_is_recoverable_across_write(tmp_path):
    """v2 形状的损坏文件：旧实现在随后的写入里会把它永久覆盖掉，本用例钉住该回归。"""
    p = tmp_path / "c.json"
    original = '{"version": 2, "container": {"plugins.x": '
    p.write_text(original, encoding="utf-8")

    cs = ConfigStore(p)
    # 旧实现的致命路径：返回 version=V2 ⇒ is_v1=False ⇒ _save 不做任何备份
    assert cs.is_v1 is False

    cs.patch_container({"plugins.x": {"k": 1}})          # 触发 _save

    assert json.loads(p.read_text(encoding="utf-8"))["container"] == {"plugins.x": {"k": 1}}
    assert p.with_suffix(".corrupt").read_text(encoding="utf-8") == original


def test_non_dict_toplevel_is_quarantined(tmp_path):
    """顶层结构非法（合法 JSON 但非对象）同样算损坏，不能静默吞掉。"""
    p = tmp_path / "c.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")

    cs = ConfigStore(p)

    assert cs._data["container"] == {}
    assert p.with_suffix(".corrupt").exists()


def test_missing_file_still_starts_clean(tmp_path):
    """不存在的文件是**正常首启**，不得被误判为损坏、不得产生 .corrupt。"""
    p = tmp_path / "nope.json"

    cs = ConfigStore(p)

    assert cs._data == {"version": V2, "container": {}, "sessions": {}}
    assert not p.with_suffix(".corrupt").exists()


# ---------------------------------------------------------------------------
# R3：未捕获异常返回 JSON 500，且不改变 HTTPException 的既有形状
# ---------------------------------------------------------------------------

def _serve_app(tmp_path):
    """跑真实 ``_serve`` 并取其 app（拦截 ``uvicorn.Server.serve``；非复刻中间件栈）。

    ``_serve`` 会注册 SIGTERM/SIGINT 处理器，测试进程里必须**原样还原**，
    否则会污染同一次 pytest 会话（handler 指向已关闭的事件循环）。
    """
    import uvicorn

    from agent.run import chat

    cap: dict = {}
    orig_serve = uvicorn.Server.serve
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

    async def _fake_serve(self):                 # noqa: ANN001 —— 替身，仅取 app
        cap["app"] = self.config.app

    uvicorn.Server.serve = _fake_serve
    try:
        args = chat._parse(["--fixture", "--port", "8799",
                            "--db", str(tmp_path / "verify.db")])
        rc = asyncio.run(chat._serve(args))
    finally:
        uvicorn.Server.serve = orig_serve
        for s, h in saved.items():
            try:
                signal.signal(s, h)
            except (ValueError, OSError, TypeError):   # noqa: BLE001
                pass

    assert rc == 0, "「关停」路径应正常返回 0"
    assert cap.get("app") is not None
    return cap["app"]


@pytest.fixture(scope="module")
def served_app(tmp_path_factory):
    """整个模块共用一个真实 app（``_serve`` 要拉真容器，不便宜）。"""
    return _serve_app(tmp_path_factory.mktemp("r3"))


def _add_route_before_mount(app, path: str, handler):
    """★ 在 ``Mount("/")`` **之前**插入路由。

    为什么不能用 ``@app.get(...)`` 事后追加：``_serve`` 末尾用
    ``app.mount("/", _SPAStaticFiles(...))`` 托管前端（``chat.py:1396``），
    而 **Starlette 的路由匹配按注册顺序，``Mount`` 会捕获其后一切** ——
    事后追加的路由一律落进静态文件处理器，返回 **404 而不是被测的 500**。

    这就是本用例此前长期红灯的根因（**不是生产缺陷**：生产把 mount 放最后是对的）。

    ⚠️ 插入点的判定条件：``Mount("/")`` 实例的 ``path`` 属性是**空字符串**
    （实测 ``app.mount("/")`` → ``Mount.path == ""``），**不是 ``"/"``**。
    写成 ``r.path == "/"`` 会永远匹配不到 ⇒ 静默退化成"没插入"。

    **没有 Mount 时直接追加**：``web/dist`` 未构建（CI 全新 checkout 即是）时
    ``_serve`` 改注一个"产物缺失"说明页，压根不注册 catch-all。
    此时不存在"被静态处理器截走"的可能，追加与前置等价。
    原先行此处的硬断言让整个模块在无 dist 的环境下恒红——那是测试在对
    自己的假设宣誓，不是在生产行为上设防；真正的防位由下面的 500 断言承担
    （路由真被截走会拿到 404，立刻红）。
    """
    from starlette.routing import Route

    route = Route(path, handler, methods=["GET"])
    routes = app.router.routes
    # 第一个 Mount 之前插入（用类型判，不靠 path —— Mount.path 是 ""）
    idx = next((i for i, r in enumerate(routes)
                if type(r).__name__ == "Mount"), None)
    if idx is None:
        routes.append(route)
    else:
        routes.insert(idx, route)
    return route


def test_global_handlers_are_registered(served_app):
    """两个 handler 必须真的挂在 app 上（这是 R3 的「数量塌缩」断言）。"""
    handlers = {getattr(k, "__name__", k) for k in served_app.exception_handlers}
    assert "Exception" in handlers, "兜底 handler 缺失 ⇒ 未捕获异常仍会是 HTML 500"
    assert "AgentError" in handlers


def test_unhandled_exception_returns_json_500(served_app):
    """未捕获异常 → 500 + application/json（旧行为是 HTML，前端解析失败）。"""
    from starlette.testclient import TestClient

    async def _boom(request):        # noqa: ARG001 —— 裸 Route 不会像 APIRoute 那样注入
        raise RuntimeError("boom-for-verification")

    _add_route_before_mount(served_app, "/__boom_for_test", _boom)

    with TestClient(served_app, raise_server_exceptions=False) as cl:
        r = cl.get("/__boom_for_test")

    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    # ★ 2026-10-04 硬化：未捕获的**意外**异常不得回显内部消息（含栈/路径/密钥/SQL），
    # 改为通用体 + request_id；完整异常仅留服务端 traceback（见 _on_unhandled）。
    body = r.json()
    assert body["error"] == "服务内部错误，请稍后重试或凭 request_id 报障"
    assert "request_id" in body and body["request_id"], "必须带 request_id 便于报障"
    assert "boom-for-verification" not in body["error"], "内部异常消息不得回显给客户端"


def test_http_exception_shape_unchanged(served_app):
    """404 等 HTTPException 未被接管，仍是既有 ``{"detail": ...}``（零行为变更）。"""
    from starlette.testclient import TestClient

    with TestClient(served_app, raise_server_exceptions=False) as cl:
        r = cl.get("/api/definitely-not-here")

    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": "Not Found"}


# ---------------------------------------------------------------------------
# H2（2026-10-04）：同会话并发护栏
# ---------------------------------------------------------------------------
# 缺陷：``/api/chat`` 此前既不登记 ``active_runs`` 也不查会话忙不忙；
# ``/api/chat/stream`` 只登记不查 ⇒ 同一会话连发两条 ⇒ 两个 run 抢同一个
# ``react_chain:{session_key}``（互相覆盖），且后一个请求的 ``_probe_resume``
# 会把**正在跑的**前一个标 orphaned。两条都是静默错误。

def test_session_busy_detects_same_session_only():
    """纯函数口径：同会话命中、跨会话不命中。"""
    from agent.run.chat import _session_busy

    runs = {"r1": (None, "u:A"), "r2": (None, "u:B")}
    assert _session_busy(runs, "u:A") == "r1"
    assert _session_busy(runs, "u:B") == "r2"
    assert _session_busy(runs, "u:C") is None
    assert _session_busy(runs, "") is None
    assert _session_busy({}, "u:A") is None


def test_both_chat_endpoints_guard_and_register(served_app):
    """★ 两个对话端点都必须 **查忙 + 登记**（源码级守卫）。

    ⚠️ 为什么不用端到端打请求：``TestClient`` **不跑 lifespan** ⇒ 容器未 assemble
    ⇒ ``/api/chat`` 依赖容器的部分直接 500（``ctx[agent] 读取: 服务 'config' 未提供``）。
    那是**夹具局限、不是生产缺陷**（要真验需起真实 uvicorn）⇒ 这里退到源码级断言，
    语义校验交给上面的纯函数用例。
    """
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent / "run" / "chat.py").read_text(
        encoding="utf-8")
    # ★ 2026-10-04：查忙+登记已收口为原子的 ``_acquire_session_run``（修复 H2-TOCTOU），
    # 两个端点都须走它（内部含 ``_session_busy`` 查忙 + ``active_runs`` 登记）。
    assert src.count("_acquire_session_run(active_runs, ident.session_key)") >= 2, \
        "/api/chat 与 /api/chat/stream 都必须经原子登记（查忙+登记一体，防 TOCTOU）"
    assert 'status_code=409' in src, "忙时必须 409 诚实拒绝（排队会让用户以为没反应）"
    assert '"code": "session_busy"' in src, "409 响应需带机器可读 code"
    # 非流式也必须登记（否则它查不到别人、自己也无法被取消）。
    # 登记动作现统一收进 ``_acquire_session_run``，内部用参数名 ``session_key``：
    assert "active_runs[run_id] = (marker, session_key)" in src,         "原子登记必须注册占位 marker ⇒ 否则查不到别人、自己也无法被取消"
    assert "active_runs.pop(run_id, None)" in src, "登记后必须清理（否则 busy 永为真）"
    # 顺带补上的能力缺口：非流式现在回 run_id ⇒ 那轮可被 /api/chat/cancel 取消
    assert '"run_id": run_id' in src, "非流式响应应带 run_id（否则无法被取消端点定位）"
