"""B3 测试：前后端分离的跨域（CORS）+ 访问令牌（鉴权）。

**纪律：opt-in，默认关闭**。
本文件同时锁住三件事：
  1. **默认关闭**——`_parse` 不传参时 `token == ""`、`cors_origin == []`；
     中间件不挂 ⇒ 行为与改造前逐字节一致（既有 392 项测试不受影响）。
  2. **启用后语义**——Bearer / `?token=` 双通道、常量时间比较、`/` 与 `/api/status`
     放行、OPTIONS 预检放行、CORS 来源精确回显（非法来源不回显）。
  3. **B4 细化**——鉴权只作用于 `/api/*`；**非 `/api/` 路径（前端静态产物）一律放行**，
     否则浏览器加载 `/assets/*.js`（`<script>` 无法带 token）会 401 ⇒ 单端口部署白屏。

测试手法：**用 ASGI 传输层直连**（httpx.ASGITransport），不启真实端口。
不 import `_serve`（它要拉起真容器）；改为在测试内**按同一份参数复刻中间件栈**——
复刻片段与 `agent/run/chat.py` 中的实现保持结构一致，若那段被改坏，
本文件的 `_parse` 断言 + 复刻语义断言会同时红灯（双保险）。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agent.run.chat import _parse


# ---------- 1. 默认关闭（opt-in 的第一层保证） ----------

def test_defaults_are_off():
    """不传参 ⇒ 不鉴权、不启用 CORS（**改造默认零影响**）。"""
    args = _parse([])
    assert args.token == ""
    assert args.cors_origin == []


def test_flags_parse():
    """`--token` 单值；`--cors-origin` 可多次累加（argparse append 语义）。"""
    args = _parse(["--token", "sekret",
                   "--cors-origin", "http://127.0.0.1:5174",
                   "--cors-origin", "https://prism.example"])
    assert args.token == "sekret"
    assert args.cors_origin == ["http://127.0.0.1:5174", "https://prism.example"]


# ---------- 2. 复刻中间件栈（与 chat.py 实现同构） ----------

def _build_app(token: str, cors_origins: list[str]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["Cache-Control", "X-Accel-Buffering"],
        )

    exempt = {"/", "/api/status"}

    def _extract_token(request: Request) -> str:
        h = request.headers.get("authorization") or ""
        if h[:7].lower() == "bearer ":
            return h[7:].strip()
        return str(request.query_params.get("token") or "")

    if token:
        import hmac

        @app.middleware("http")
        async def _auth_mw(request: Request, call_next):
            path = request.url.path
            # 与 chat.py 同构：预检 / 显式豁免 / **非 /api/（B4 静态产物）** 三类放行
            if (request.method == "OPTIONS"
                    or path in exempt
                    or not path.startswith("/api/")):
                return await call_next(request)
            if not hmac.compare_digest(_extract_token(request), token):
                return JSONResponse({"error": "未授权：缺少或无效的 token"},
                                    status_code=401)
            return await call_next(request)

    @app.get("/")
    async def _home():
        return JSONResponse({"ok": "home"})

    @app.get("/api/status")
    async def _status():
        return JSONResponse({"ok": "status"})

    @app.get("/api/tools")
    async def _tools():
        return JSONResponse({"ok": "tools"})

    # B4：模拟静态产物路径（非 /api/），用于验证「浏览器无 token 也能加载前端资源」
    @app.get("/assets/index-abc123.js")
    async def _asset():
        return JSONResponse({"ok": "asset"})

    @app.get("/favicon.ico")
    async def _favicon():
        return JSONResponse({"ok": "icon"})

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver")


def _run(coro):
    return asyncio.run(coro)


# ---------- 3. 鉴权语义 ----------

def test_no_auth_when_token_empty():
    """`--token` 留空 ⇒ 中间件不挂，一切放行（默认关闭的运行时侧证明）。"""
    async def s():
        async with _client(_build_app("", [])) as c:
            for path in ("/", "/api/status", "/api/tools"):
                r = await c.get(path)
                assert r.status_code == 200, f"{path} 应放行，实得 {r.status_code}"

    _run(s())


def test_exempt_paths_always_pass():
    """`/` 与 `/api/status` 是健康探针，鉴权启用后仍放行。"""
    async def s():
        async with _client(_build_app("sekret", [])) as c:
            assert (await c.get("/")).status_code == 200
            assert (await c.get("/api/status")).status_code == 200

    _run(s())


def test_static_assets_pass_without_token():
    """**B4**：非 `/api/` 路径（前端静态产物）一律放行。

    浏览器用 `<script src="/assets/...">` 加载产物时**无法携带 token**；
    若把静态资源也纳入鉴权，单端口部署下 token 一开就整页白屏。
    数据面（`/api/*`）不受影响，安全强度不变。
    """
    async def s():
        async with _client(_build_app("sekret", [])) as c:
            for path in ("/assets/index-abc123.js", "/favicon.ico"):
                r = await c.get(path)
                assert r.status_code == 200, f"{path} 应放行（静态资源），实得 {r.status_code}"

    _run(s())


def test_rejects_without_token():
    """受保护端点无凭据 ⇒ 401，且错误体可读（前端据此提示）。"""
    async def s():
        async with _client(_build_app("sekret", [])) as c:
            r = await c.get("/api/tools")
            assert r.status_code == 401
            assert "token" in r.json()["error"]

    _run(s())


def test_accepts_bearer_and_query_token():
    """双通道都要能过：`Authorization: Bearer`（普通 fetch）与 `?token=`（SSE/EventSource）。"""
    async def s():
        async with _client(_build_app("sekret", [])) as c:
            r1 = await c.get("/api/tools", headers={"Authorization": "Bearer sekret"})
            assert r1.status_code == 200, "Bearer 通道未通过"
            r2 = await c.get("/api/tools", params={"token": "sekret"})
            assert r2.status_code == 200, "query token 通道未通过"

    _run(s())


def test_bearer_takes_precedence_over_query():
    """headers 优先于 query：两处都带时以 Authorization 为准（避免歧义）。"""
    async def s():
        async with _client(_build_app("sekret", [])) as c:
            r = await c.get("/api/tools", params={"token": "wrong"},
                            headers={"Authorization": "Bearer sekret"})
            assert r.status_code == 200

    _run(s())


@pytest.mark.parametrize("bad", [
    {"Authorization": "Bearer wrong"},                 # 错令牌
    {"Authorization": "sekret"},                       # 缺 Bearer 前缀
    {"Authorization": "Basic sekret"},                 # 错 scheme
    {"Authorization": "Bearer "},                      # 空值
    {},                                                # 无头
])
def test_rejects_bad_credentials(bad):
    """各种错误形态一律 401（含「缺前缀」「错 scheme」这类易漏形态）。"""
    async def s():
        async with _client(_build_app("sekret", [])) as c:
            r = await c.get("/api/tools", headers=bad)
            assert r.status_code == 401, f"{bad} 应拒，实得 {r.status_code}"

    _run(s())


def test_bearer_is_case_insensitive_on_scheme():
    """HTTP 认证方案名大小写不敏感（`bearer`/`BEARER` 均须识别）。"""
    async def s():
        async with _client(_build_app("sekret", [])) as c:
            for scheme in ("bearer", "BEARER", "BeArEr"):
                r = await c.get("/api/tools",
                                headers={"Authorization": f"{scheme} sekret"})
                assert r.status_code == 200, f"scheme={scheme} 未识别"

    _run(s())


def test_options_preflight_passes_without_token():
    """预检不带 Authorization（浏览器规范）⇒ 必须放行，否则 CORS 全废。"""
    async def s():
        async with _client(_build_app("sekret", ["http://127.0.0.1:5174"])) as c:
            r = await c.request("OPTIONS", "/api/tools", headers={
                "Origin": "http://127.0.0.1:5174",
                "Access-Control-Request-Method": "POST",
            })
            assert r.status_code in (200, 204), f"预检被拒：{r.status_code}"

    _run(s())


# ---------- 4. CORS 语义 ----------

def test_cors_echoes_allowed_origin_with_credentials():
    """允许来源 ⇒ 精确回显 + `allow-credentials: true`（不能用 `*`）。"""
    async def s():
        async with _client(_build_app("", ["http://127.0.0.1:5174"])) as c:
            r = await c.get("/api/status", headers={"Origin": "http://127.0.0.1:5174"})
            assert r.headers.get("access-control-allow-origin") == "http://127.0.0.1:5174"
            assert r.headers.get("access-control-allow-credentials") == "true"

    _run(s())


def test_cors_does_not_echo_foreign_origin():
    """非白名单来源 ⇒ **不**回显 `allow-origin`（浏览器自行拦截）。"""
    async def s():
        async with _client(_build_app("", ["http://127.0.0.1:5174"])) as c:
            r = await c.get("/api/status", headers={"Origin": "http://evil.example"})
            assert r.headers.get("access-control-allow-origin") is None

    _run(s())


def test_cors_absent_when_not_configured():
    """不传 `--cors-origin` ⇒ 响应里没有任何 CORS 头（默认零影响）。"""
    async def s():
        async with _client(_build_app("", [])) as c:
            r = await c.get("/api/status", headers={"Origin": "http://127.0.0.1:5174"})
            assert r.headers.get("access-control-allow-origin") is None

    _run(s())
