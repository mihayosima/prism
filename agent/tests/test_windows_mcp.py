"""windows_mcp 插件配置解析测试（离线，不起真子进程）。"""

from __future__ import annotations

from agent.plugins.windows_mcp import resolve


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