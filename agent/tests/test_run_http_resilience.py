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


def test_global_handlers_are_registered(served_app):
    """两个 handler 必须真的挂在 app 上（这是 R3 的「数量塌缩」断言）。"""
    handlers = {getattr(k, "__name__", k) for k in served_app.exception_handlers}
    assert "Exception" in handlers, "兜底 handler 缺失 ⇒ 未捕获异常仍会是 HTML 500"
    assert "AgentError" in handlers


def test_unhandled_exception_returns_json_500(served_app):
    """未捕获异常 → 500 + application/json（旧行为是 HTML，前端解析失败）。"""
    from starlette.testclient import TestClient

    @served_app.get("/__boom_for_test")
    async def _boom():
        raise RuntimeError("boom-for-verification")

    with TestClient(served_app, raise_server_exceptions=False) as cl:
        r = cl.get("/__boom_for_test")

    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"error": "boom-for-verification", "kind": "RuntimeError"}


def test_http_exception_shape_unchanged(served_app):
    """404 等 HTTPException 未被接管，仍是既有 ``{"detail": ...}``（零行为变更）。"""
    from starlette.testclient import TestClient

    with TestClient(served_app, raise_server_exceptions=False) as cl:
        r = cl.get("/api/definitely-not-here")

    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": "Not Found"}
