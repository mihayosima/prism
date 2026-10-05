"""``HandlerDeps`` 接缝：用**替身**构造依赖包即可注册全部路由并打端点。

此前要拿到这套路由，**只能真跑 ``_serve``** —— handler 依赖全是 ``_serve`` 的局部
闭包（``_Box`` / ``active_runs`` / ``_identity`` / ``pm`` …），无从注入 ⇒ 想测一个
端点就得真起 uvicorn + 真实装配容器（Redis/DB/插件全就位），HTTP 层因此几乎不可单测。

依赖显式化成 ``HandlerDeps``（2026-10-06）后，依赖集合可被替身构造，于是：
    · 不需要 uvicorn 进程；
    · 不需要真实容器（不 assemble、不连 Redis、不建 DB）；
即可把全部路由注册到一个全新 ``FastAPI()`` 上直接打端点。

★ 本文件的价值是**证明接缝真的可用**，不是"看起来显式了"：
  若哪天又把依赖改回闭包捕获（或某个域函数漏接 deps），这里会立刻变红。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.testclient import TestClient

from agent.core.session import make_identity
from agent.run.chat import HandlerDeps, _register_routes


class _FakeRoot:
    """最小容器根替身：只提供本文件会走到的读取面。"""

    def __init__(self) -> None:
        self.state = {}
        self.config = {}

    def has(self, _svc) -> bool:
        return False

    def get(self, _svc):
        return None          # /api/status 用 getattr(..., "?") 兜底，None 即可


class _FakeBox:
    """``_serve`` 里 ``_Box`` 的替身（handlers 一律经 ``c.root`` 取服务）。"""

    def __init__(self) -> None:
        self.root = _FakeRoot()


def _deps(*, identity=None) -> HandlerDeps:
    return HandlerDeps(
        c=_FakeBox(),
        active_runs={},
        pm=object(),
        identity=identity or (lambda request=None, body=None: make_identity()),
        apply_rebuild=_fake_rebuild,
        sse=lambda event, payload: "data: {}\n\n",
        approval_or_none=lambda: None,
        JSONResponse=JSONResponse,
        Request=Request,
        StreamingResponse=StreamingResponse,
    )


async def _fake_rebuild() -> None:
    return None


def _app_with_routes(*, identity=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    _register_routes(app, _deps(identity=identity))
    return app


def test_routes_register_without_serving():
    """不经 ``_serve``、不起 uvicorn、不装配真实容器，路由照样全部注册。"""
    app = _app_with_routes()
    paths = {getattr(r, "path", None) for r in app.router.routes}
    for expect in ("/health", "/ready", "/metrics", "/api/chat",
                   "/api/status", "/api/approval/pending",
                   "/api/plugins", "/api/settings"):
        assert expect in paths, f"{expect} 未注册"


def test_health_endpoint_works_with_fake_deps():
    """探针端点在替身依赖下真实可用（此前必须真起服务才能验证）。"""
    client = TestClient(_app_with_routes())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_injected_identity_reaches_handler():
    """★ 关键：注入的 ``identity`` 真的**送达**了 handler，而不只是被接受。

    替身身份固定为 ``u:s`` ⇒ 若 deps 没贯通（handler 仍走别的身份源），
    这里会读到别的值 ⇒ 接缝其实没生效会立刻暴露。
    """
    ident = make_identity(user="u", session="s")

    def _identity(request=None, body=None):
        return ident

    client = TestClient(_app_with_routes(identity=_identity))
    body = client.get("/api/status").json()
    assert body["session_id"] == "u:s"
    assert body["user"] == "u" and body["session"] == "s"
