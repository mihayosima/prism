"""通用 MCP stdio 客户端测试（假 MCP 服务器子进程）。"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from agent.core.mcp_client import McpStdioClient


def _run(coro):
    return asyncio.run(coro)


_FAKE_MCP = '''import sys, json
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    mid, method = msg["id"], msg["method"]
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{"tools":{}},"serverInfo":{"name":"fake","version":"1"}}})
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
              {"name":"echo","description":"回显参数",
               "inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}]}})
    elif method == "tools/call":
        args = msg["params"]["arguments"]
        send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps(args)}],"isError":False}})
    else:
        send({"jsonrpc":"2.0","id":mid,"result":{}})
'''


def test_mcp_client_list_and_call(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(_FAKE_MCP, encoding="utf-8")
    client = McpStdioClient(sys.executable, [str(script)])

    async def s():
        await client.start()
        tools = await client.list_tools()
        assert any(t["name"] == "echo" for t in tools)
        result = await client.call_tool("echo", {"text": "hi"})
        assert '"hi"' in result["text"] or "hi" in result["text"]
        await client.close()

    _run(s())


def test_mcp_client_tool_error_raises(tmp_path):
    script = tmp_path / "fake_mcp_err.py"
    script.write_text(_FAKE_MCP.replace('"isError":False', '"isError":True'), encoding="utf-8")
    client = McpStdioClient(sys.executable, [str(script)])

    async def s():
        await client.start()
        try:
            await client.call_tool("echo", {"text": "x"})
            raise AssertionError("应抛错")
        except RuntimeError:
            pass
        await client.close()

    _run(s())


# ---- S2/S3 加固测试 ----

_CHATTY_MCP = '''import sys, json
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for i in range(2000):                        # ~80KB stderr，远超 64KB 管道缓冲区
    sys.stderr.write("log line %04d aaaa bbbb cccc dddd eeee\\n" % i)
sys.stderr.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    mid, method = msg["id"], msg["method"]
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{},"serverInfo":{"name":"chatty","version":"1"}}})
    else:
        send({"jsonrpc":"2.0","id":mid,"result":{}})
'''


def test_chatty_stderr_does_not_deadlock(tmp_path):
    """S2：话痨 stderr 子进程不再写满管道卡死，请求照常完成。"""
    script = tmp_path / "fake_mcp_chatty.py"
    script.write_text(_CHATTY_MCP, encoding="utf-8")
    client = McpStdioClient(sys.executable, [str(script)], timeout_s=10.0)

    async def s():
        await client.start()
        result = await client.call_tool("echo", {"ok": 1})
        assert "result" in result or result["text"] == ""
        await client.close()

    _run(s())


_CRASHY_MCP = '''import sys, json
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    mid, method = msg["id"], msg["method"]
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{},"serverInfo":{"name":"crashy","version":"1"}}})
    else:
        sys.exit(0)                          # 后续任何请求：不回应，直接退出
'''


def test_process_exit_fails_pending_immediately(tmp_path):
    """S3：子进程退出时在途请求立即收到 ConnectionError，而非悬挂到超时。"""
    import time
    script = tmp_path / "fake_mcp_crashy.py"
    script.write_text(_CRASHY_MCP, encoding="utf-8")
    client = McpStdioClient(sys.executable, [str(script)], timeout_s=30.0)

    async def s():
        await client.start()
        t0 = time.monotonic()
        try:
            await client.list_tools()
            raise AssertionError("子进程退出后应抛 ConnectionError")
        except ConnectionError:
            pass
        assert time.monotonic() - t0 < 5.0, "应立即报错而非等到超时"
        await client.close()

    _run(s())


_SILENT_MCP = '''import sys, json
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    mid, method = msg["id"], msg["method"]
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{},"serverInfo":{"name":"silent","version":"1"}}})
    # 其余请求：永远不回应
'''


def test_close_fails_inflight_request(tmp_path):
    """S3：close() 时在途请求立即收到 ConnectionError。"""
    script = tmp_path / "fake_mcp_silent.py"
    script.write_text(_SILENT_MCP, encoding="utf-8")
    client = McpStdioClient(sys.executable, [str(script)], timeout_s=30.0)

    async def s():
        await client.start()
        task = asyncio.create_task(client.call_tool("echo", {}))
        await asyncio.sleep(0.1)             # 让请求发出并进入 pending
        await client.close()
        try:
            await task
            raise AssertionError("close 后在途请求应失败")
        except ConnectionError:
            pass

    _run(s())


# ---- S4 加固测试：死亡可见 / 归因唯一 / 不留孤儿 ----

_CRASH_ON_CALL_MCP = '''import sys, json
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
              "capabilities":{},"serverInfo":{"name":"crashy","version":"1"}}})
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
              {"name":"Boom","description":"调用即崩",
               "inputSchema":{"type":"object","properties":{}}}]}})
    else:
        sys.exit(7)                      # 关键：在调用途中退出
'''

# 连 initialize 都不回应（且不退出）⇒ start() 必然超时
_MUTE_MCP = '''import sys
for _line in sys.stdin:
    pass
'''


def test_dead_state_is_detected_and_error_is_stable(tmp_path):
    """S4：子进程退出后客户端「知道自己死了」，且报错恒定。

    修复前实测（复现用户报障）：第 1 次 ConnectionError(stdout EOF)、第 2 次起
    ConnectionResetError，且 returncode 恒为 None ⇒ 客户端对死亡毫无感知、永不恢复。
    """
    script = tmp_path / "fake_mcp_crash_on_call.py"
    script.write_text(_CRASH_ON_CALL_MCP, encoding="utf-8")
    client = McpStdioClient(sys.executable, [str(script)], timeout_s=30.0)

    async def s():
        await client.start()
        assert client.is_alive and client.dead_reason is None

        with pytest.raises(ConnectionError):
            await client.call_tool("Boom", {})

        await asyncio.sleep(0.5)             # 让 _wait_loop 拿到退出码（权威死因）
        assert not client.is_alive, "子进程已退出 ⇒ is_alive 必须为 False"
        assert "exit code 7" in (client.dead_reason or ""), client.dead_reason

        # 之后每次调用都必须是**同一条** ConnectionError（不再漂移成 ConnectionResetError）
        for _ in range(2):
            with pytest.raises(ConnectionError) as ei:
                await client.call_tool("Boom", {})
            assert str(ei.value) == client.dead_reason

        await client.close()

    _run(s())


def test_close_reason_is_closed_not_crashed(tmp_path):
    """S4：主动 close 时在途请求必须报「已关闭」，不得误报成「子进程已退出」。"""
    script = tmp_path / "fake_mcp_silent_2.py"
    script.write_text(_SILENT_MCP, encoding="utf-8")
    client = McpStdioClient(sys.executable, [str(script)], timeout_s=30.0)

    async def s():
        await client.start()
        task = asyncio.create_task(client.call_tool("echo", {}))
        await asyncio.sleep(0.3)             # 让请求进入 pending
        await client.close()
        with pytest.raises(ConnectionError) as ei:
            await task
        msg = str(ei.value)
        assert "已关闭" in msg, f"归因错位：{msg}"
        assert "子进程已退出" not in msg, f"主动关闭被误报成崩溃：{msg}"

    _run(s())


def test_failed_start_reaps_child(tmp_path):
    """S4：start() 失败（initialize 超时）也要收干净，不留孤儿子进程。"""
    script = tmp_path / "fake_mcp_mute.py"
    script.write_text(_MUTE_MCP, encoding="utf-8")
    client = McpStdioClient(sys.executable, [str(script)], timeout_s=1.0)

    async def s():
        with pytest.raises(asyncio.TimeoutError):
            await client.start()
        assert client._proc is None, "start 失败后必须回收子进程（否则变孤儿）"
        assert not client.is_alive

    _run(s())


# ---------------------------------------------------------------------------
# S5：宿主安全删除策略隔离
#
# 根因（2026-10-01 实测）：宿主通过 PYTHONPATH 把 sitecustomize.py 注入本进程树，
# 其 `_check_bulk_delete_guard` 在计数超阈值时 `raise SystemExit(1)` —— BaseException
# 会顺着 anyio TaskGroup / stdio_server() 把**整个 MCP 服务器进程**打掉：一次
# `delete_path` 就让 windows_mcp 以退出码 1 死亡，之后列目录/跑命令全部 EOF。
# 而该计数按 `CODEBUDDY_CONVERSATION_REQUEST_ID` 归档且**启动时冻结**：真 CLI 每轮刷新
# （计数=本 turn），长驻后端则变成「进程生命周期累计器」，必然越阈值并永久卡死。
# ---------------------------------------------------------------------------

_ENV_PROBE_MCP = '''import os, sys, json
WATCHED = ["CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR", "CODEBUDDY_TOOL_CALL_ID",
           "CODEBUDDY_SESSION_ID", "CODEBUDDY_CONVERSATION_REQUEST_ID"]
def send(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    mid, method = msg["id"], msg["method"]
    if method == "initialize":
        send({"jsonrpc":"2.0","id":mid,"result":{"protocolVersion":"2024-11-05",
              "capabilities":{"tools":{}},"serverInfo":{"name":"probe","version":"1"}}})
    elif method == "tools/list":
        send({"jsonrpc":"2.0","id":mid,"result":{"tools":[
              {"name":"env","description":"env",
               "inputSchema":{"type":"object","properties":{}}}]}})
    else:
        present = [k for k in WATCHED if k in os.environ]
        send({"jsonrpc":"2.0","id":mid,"result":{"content":[{"type":"text","text":json.dumps({
            "present": present,
            "has_pythonpath": bool(os.environ.get("PYTHONPATH")),
        })}],"isError":False}})
'''


def test_child_env_strips_host_safe_delete_vars(monkeypatch):
    """`child_env` 必须剥掉 4 个守卫变量，其余（含 PYTHONPATH）原样保留。"""
    from agent.core.mcp_client import _HOST_SAFE_DELETE_ENV, child_env

    for key in _HOST_SAFE_DELETE_ENV:
        monkeypatch.setenv(key, "host-injected")
    monkeypatch.setenv("PYTHONPATH", r"D:\shim")
    monkeypatch.setenv("AGENT_KEEP_ME", "1")

    env = child_env()
    for key in _HOST_SAFE_DELETE_ENV:
        assert key not in env, f"{key} 必须剥掉，否则守卫会打死 MCP 子进程"
    assert env["PYTHONPATH"] == r"D:\shim", "PYTHONPATH 必须保留（回收站语义靠 shim）"
    assert env["AGENT_KEEP_ME"] == "1", "无关变量不得误伤"

    # 显式传入的同名变量也必须被剥掉（合并之后再剥，保证不变量）
    env2 = child_env({"CODEBUDDY_TOOL_CALL_ID": "explicit"})
    assert "CODEBUDDY_TOOL_CALL_ID" not in env2


def test_spawned_child_never_sees_host_safe_delete_vars(tmp_path, monkeypatch):
    """运行时实证：真正落到 MCP 子进程的环境里，这 4 个变量一个都不能有。"""
    from agent.core.mcp_client import _HOST_SAFE_DELETE_ENV

    for key in _HOST_SAFE_DELETE_ENV:
        monkeypatch.setenv(key, "host-injected")
    monkeypatch.setenv("PYTHONPATH", r"D:\shim")

    script = tmp_path / "env_probe_mcp.py"
    script.write_text(_ENV_PROBE_MCP, encoding="utf-8")
    client = McpStdioClient(sys.executable, [str(script)])

    async def s():
        await client.start()
        try:
            result = await client.call_tool("env", {})
            payload = json.loads(result["text"])
            assert payload["present"] == [], f"子进程仍看到守卫变量：{payload['present']}"
            assert payload["has_pythonpath"], "PYTHONPATH 应保留"
        finally:
            await client.close()

    _run(s())