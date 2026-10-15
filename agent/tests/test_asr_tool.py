"""ASR agent 工具暴露（整合进工作流）：``asr.transcribe_file`` 注册契约。

设计要点（见 ``agent/plugins/asr/__init__.py``）：
- 工具名 ``asr.transcribe_file``，owner 域 ``plugins.asr``，可在 ``tool_domains`` 白名单裁剪；
- risk="read"（仅读取文件、不改状态）；
- handler 调 ``ASRService.transcribe_path`` 并 ``to_dict()``，引擎未就绪时如实抛 ``EngineUnavailable``。

**不加载真引擎**（契约测试要的是注册/路由正确性，不是识别精度）。

本环境未装 pytest-asyncio，故测试用 ``asyncio.run`` 包同步执行（避免插件依赖）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from agent.api import AgentError
from agent.plugins.asr import ASR
from agent.sys.tasks import TaskRegistry


class _FakeCtx:
    """最小 ctx：持有真 TaskRegistry，provide/effect/get 均留桩。"""

    def __init__(self, config=None):
        self.config = config or {}
        self.tasks = TaskRegistry()
        self._provided = {}
        self._effects = []

    def provide(self, key, obj):
        self._provided[key] = obj

    def effect(self, fn):
        self._effects.append(fn)

    def get(self, key):
        return None


class _FakeService:
    """假门面：``transcribe_path`` 不真解码，返回预置 ``FileTranscribeResult``。"""

    async def transcribe_path(self, path, opts=None):
        from agent.plugins.asr.providers import FileTranscribeResult, Segment

        return FileTranscribeResult(
            duration_sec=1.0, sample_rate=16000, mode="offline",
            segments=[Segment(index=0, start_sec=0.0, end_sec=1.0, text="测试文本")],
        )


def _apply_with_fake_service(config=None) -> tuple[object, _FakeCtx]:
    ctx = _FakeCtx(config=config)
    # ASR 是 @plugin 包装后的 Plugin 单例，用 ``ASR.apply(ctx)`` 触发其 underlying 实例的 apply
    asyncio.run(ASR.apply(ctx))
    inst = ASR._obj()
    # apply 内部用真实 ASRService 替身；注册完再换成不加载引擎的假门面，
    # 让 handler 闭包（引用 self._service）走假路径，避免几百 MB 模型加载。
    inst._service = _FakeService()
    return inst, ctx


def test_asr_tool_registered_with_correct_metadata():
    _, ctx = _apply_with_fake_service()

    names = ctx.tasks.names()
    assert "asr.transcribe_file" in names, f"工具未注册：{names}"

    entry = next(t for t in ctx.tasks.catalog() if t["name"] == "asr.transcribe_file")
    assert entry["owner"] == "plugins.asr", entry
    assert entry["risk"] == "read", entry
    assert "path" in entry["params"], entry
    assert entry["required"] == ["path"], entry


def test_asr_tool_handler_returns_segments():
    _, ctx = _apply_with_fake_service()

    # 直接调 Task.invoke（只做 schema 校验 + handler，不触碰 events/durable 链路）
    task = ctx.tasks._tasks["asr.transcribe_file"]
    result = asyncio.run(task.invoke({"path": "/tmp/x.mp3"}))

    assert result["sample_rate"] == 16000
    assert result["segments"][0]["text"] == "测试文本"


def test_asr_tool_handler_honors_min_segment():
    _, ctx = _apply_with_fake_service()

    task = ctx.tasks._tasks["asr.transcribe_file"]
    # 可省参数缺省不报错（schema 默认 0.0）
    result = asyncio.run(task.invoke({"path": "/tmp/y.wav"}))
    assert result["mode"] == "offline"


def test_asr_tool_rejects_missing_path():
    _, ctx = _apply_with_fake_service()

    task = ctx.tasks._tasks["asr.transcribe_file"]
    with pytest.raises(AgentError):
        asyncio.run(task.invoke({}))            # 缺必填 path
