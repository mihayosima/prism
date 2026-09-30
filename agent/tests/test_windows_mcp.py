"""windows_mcp 插件配置解析测试（离线，不起真子进程）。"""

from __future__ import annotations

import asyncio
import logging
import sys

import pytest

from agent.plugins.windows_mcp import WindowsMcp, _on_stderr, resolve


def test_default_uvx_command():
    cmd, args, timeout = resolve({}, {})
    assert cmd == "uvx"
    assert args == ["windows-mcp", "serve"]
    assert timeout == 120.0


def test_env_override():
    cmd, args, timeout = resolve({}, {"WINDOWS_MCP_COMMAND": "node",
                                      "WINDOWS_MCP_ARGS": '["x.js","--stdio"]'})
    assert cmd == "node"
    assert args == ["x.js", "--stdio"]


def test_config_override_wins():
    cmd, args, timeout = resolve({"command": "npx.cmd", "args": ["-y", "windows-mcp"],
                                  "timeout_s": 90},
                                 {"WINDOWS_MCP_COMMAND": "node"})
    assert cmd == "npx.cmd"
    assert args == ["-y", "windows-mcp"]
    assert timeout == 90.0


def test_env_args_comma_form():
    cmd, args, _ = resolve({}, {"WINDOWS_MCP_COMMAND": "uvx",
                                "WINDOWS_MCP_ARGS": "windows-mcp,serve"})
    assert cmd == "uvx"
    assert args == ["windows-mcp", "serve"]


# ---- S4 加固测试：死因可见 / 自愈重启 ----

# 可用环境变量 FLAKY_FLAG 指定「崩溃开关」文件：文件存在 ⇒ 调用即退出（模拟子进程崩溃）
_FLAKY_MCP = '''import sys, json, os
FLAG = os.environ.get("FLAKY_FLAG", "")
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{},"serverInfo":{"name":"flaky","version":"1"}}})
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
              {"name":"echo","description":"回显参数",
               "inputSchema":{"type":"object","properties":{"text":{"type":"string"}},
                              "required":["text"]}}]}})
    elif method == "tools/call":
        if FLAG and os.path.exists(FLAG):
            sys.exit(7)                       # 崩溃模式
        args = msg["params"]["arguments"]
        send({"jsonrpc":"2.0","id":mid,"result":{
              "content":[{"type":"text","text":json.dumps(args)}],"isError":False}})
'''


class _Tasks:
    def __init__(self):
        self.registered: dict[str, object] = {}

    def register(self, ctx, name, schema=None, handler=None, owner=None, description=None):
        self.registered[name] = handler


class _Ctx:
    """最小 ctx 桩：够 apply() 跑通并保留 handler。"""

    def __init__(self, config):
        self.config = config
        self.state: dict = {}
        self.tasks = _Tasks()
        self.services: dict = {}
        self.effects: list = []

    def provide(self, key, val):
        self.services[key] = val

    def effect(self, fn):
        self.effects.append(fn)


def test_stderr_goes_to_warning(caplog):
    """S4：子进程 stderr 必须落到 WARNING。

    修复前它走 ``logger.debug``，默认日志级别下被彻底丢弃 ⇒ 线上后端日志里
    「子进程为什么死」零痕迹（实测整份日志 8 行、无一条 MCP 记录）。
    """
    with caplog.at_level(logging.WARNING, logger="agent.plugins.windows_mcp"):
        _on_stderr("   ")                       # 空白行忽略
        _on_stderr("Fatal: 权限不足，无法访问桌面")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("权限不足" in m for m in msgs), msgs
    assert all(r.levelno >= logging.WARNING for r in caplog.records)
    assert len(caplog.records) == 1, "空白行不应产生日志"


def test_plugin_self_heals_after_child_crash(tmp_path):
    """S4 P1：子进程崩溃后，**下一次调用**应自动就地重启并成功（无需重启后端）。

    修复前：子进程死一次即永久失效（后续全是 ConnectionResetError），只能重启整个后端。
    """
    script = tmp_path / "flaky_mcp.py"
    script.write_text(_FLAKY_MCP, encoding="utf-8")
    flag = tmp_path / "crash.flag"
    flag.write_text("1", encoding="utf-8")       # 先处于「崩溃模式」

    plug = WindowsMcp._raw()                     # @plugin 包装成 Plugin ⇒ 取原始类实例化
    ctx = _Ctx({"windows_mcp": {
        "command": sys.executable,
        "args": [str(script)],
        "timeout_s": 30,
        # env 由客户端合并进子进程环境；重启时沿用同一份 ⇒ 只需删掉 flag 即可「康复」
        "env": {"FLAKY_FLAG": str(flag)},
    }})

    async def s():
        await plug.apply(ctx)
        call = ctx.tasks.registered["mcp.windows.echo"]

        with pytest.raises(ConnectionError):
            await call(text="hi")                # 第一次：子进程退出
        await asyncio.sleep(0.5)
        assert plug._client is not None and not plug._client.is_alive

        flag.unlink()                            # 切到「康复模式」
        out = await call(text="hi")              # 第二次：应自动重启并成功
        assert "hi" in out["text"]
        assert plug._client.is_alive
        assert ctx.state.get("windows_mcp_error") is None, ctx.state

        # 插件页状态不再"假健康"：alive 实时可读
        st = await plug.status(ctx)
        assert st["alive"] is True
        assert "echo" in st["tools"]

        await plug._close()

    asyncio.run(s())


def test_plugin_reports_unavailable_when_restart_fails(tmp_path):
    """S4 P1：重启也失败时，必须如实抛错并把错误写进状态（不得静默、不得假装健康）。"""
    script = tmp_path / "flaky_mcp2.py"
    script.write_text(_FLAKY_MCP, encoding="utf-8")
    flag = tmp_path / "crash2.flag"
    flag.write_text("1", encoding="utf-8")

    plug = WindowsMcp._raw()                     # @plugin 包装成 Plugin ⇒ 取原始类实例化
    ctx = _Ctx({"windows_mcp": {
        "command": sys.executable,
        "args": [str(script)],
        "timeout_s": 30,
        "env": {"FLAKY_FLAG": str(flag)},
    }})

    async def s():
        await plug.apply(ctx)
        call = ctx.tasks.registered["mcp.windows.echo"]
        with pytest.raises(ConnectionError):
            await call(text="hi")                # 子进程崩溃
        await asyncio.sleep(0.5)

        # 让重启必然失败：把启动命令指向不存在的可执行文件
        plug._cfg = dict(plug._cfg, command=str(tmp_path / "no_such_mcp.exe"))
        with pytest.raises(RuntimeError) as ei:
            await call(text="hi")
        assert "重启失败" in str(ei.value), str(ei.value)

        st = await plug.status(ctx)
        assert st["alive"] is False, "重启失败后必须如实报告不可用"
        assert st["error"], "重启失败必须写进状态，供插件页显示"
        await plug._close()

    asyncio.run(s())