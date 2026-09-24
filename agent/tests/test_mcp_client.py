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