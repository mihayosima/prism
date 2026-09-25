"""通用 MCP stdio 客户端测试（假 MCP 服务器子进程）。"""

from __future__ import annotations

import asyncio
import sys

from agent.sys.mcp_client import McpStdioClient


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