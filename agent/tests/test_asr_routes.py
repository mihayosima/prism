"""ASR 端点层契约：WS 帧序、文件转写、未启用降级、WS 鉴权。

沿用 ``test_handler_deps.py`` 的接缝思路：用**替身**构造 ``HandlerDeps``，
把路由注册到全新的 ``FastAPI()`` 上用 ``TestClient`` 打——不需要 uvicorn、
不需要真实容器、不加载几百 MB 模型（ASR 服务用假门面替身）。

覆盖的**端点契约**（前端依赖这些帧/形状，改动即破）：
    POST /api/asr/transcribe-file → ``{ok, duration_sec, sample_rate, mode, segments[]}``
    GET  /api/asr/status         → ``{ok, available, status}``
    WS   /api/asr/ws             → ``loading`` → ``ready`` → ``partial/final*``
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.api import Services
from agent.core.session import make_identity
from agent.run.chat import HandlerDeps, _register_routes
from agent.plugins.asr.providers import EngineUnavailable


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------
class _FakeASRService:
    """假 ASR 门面：记录调用、返回预置结果或抛预置异常。"""

    def __init__(self, *, ready: bool = True, segments=None, raise_on_ready=None):
        self._ready = ready
        self._segments = segments if segments is not None else [
            {"index": 0, "start_sec": 0.0, "end_sec": 1.5, "text": "会议开始"},
            {"index": 1, "start_sec": 1.6, "end_sec": 3.2, "text": "由张工汇报"},
        ]
        self._raise = raise_on_ready
        self.pushed: list[bytes] = []
        self.stopped = False

    async def ensure_ready(self):
        if self._raise is not None:
            raise self._raise
        if not self._ready:
            raise EngineUnavailable("未安装 sherpa-onnx")

    def status(self):
        return {"state": "ready" if self._ready else "unavailable",
                "reason": "" if self._ready else "未安装 sherpa-onnx",
                "provider": {"name": "sherpa", "streaming": True, "file": True,
                             "ready": self._ready},
                "models": {}}

    def open_stream(self):
        return self

    async def feed_stream(self, session, pcm, sample_rate=16000):
        self.pushed.append(pcm)
        return [{"type": "partial", "text": "今天天气"}] if len(self.pushed) == 1 \
            else [{"type": "final", "text": "今天天气怎么样"}]

    async def finish_stream(self, session):
        self.stopped = True
        return [{"type": "final", "text": "今天天气怎么样"}]

    def reset(self):
        pass

    async def transcribe_path(self, path, opts):
        from agent.plugins.asr.providers import FileTranscribeResult, Segment
        segs = [Segment(**s) for s in self._segments]
        return FileTranscribeResult(duration_sec=3.2, sample_rate=16000, segments=segs)

    def dispose(self):
        pass


class _FakeRoot:
    def __init__(self, asr=None):
        self.state = {}
        self.config = {}
        self._asr = asr

    def has(self, svc) -> bool:
        return svc == Services.ASR and self._asr is not None

    def get(self, svc):
        return self._asr if svc == Services.ASR else None


class _FakeBox:
    def __init__(self, asr=None):
        self.root = _FakeRoot(asr)


def _app(*, asr=None, token: str = "") -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)
    deps = HandlerDeps(
        c=_FakeBox(asr), active_runs={}, pm=object(),
        identity=lambda request=None, body=None: make_identity(),
        apply_rebuild=_noop, sse=lambda e, p: "data: {}\n\n",
        approval_or_none=lambda: None,
        JSONResponse=JSONResponse, Request=Request, StreamingResponse=StreamingResponse,
        token=token)
    _register_routes(app, deps)
    return app


async def _noop() -> None:
    return None


def _wav_bytes() -> bytes:
    """最小合法 wav（16kHz mono 16bit，0.2s 正弦）——soundfile 读得动。"""
    import numpy as np
    import soundfile as sf
    sr = 16000
    t = np.arange(int(sr * 0.2)) / sr
    data = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
    buf = io.BytesIO()
    sf.write(buf, data, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


# --------------------------------------------------------------------------
# 路由存在性
# --------------------------------------------------------------------------
def test_asr_routes_registered():
    app = _app(asr=_FakeASRService())
    paths = {r.path for r in app.routes}
    assert "/api/asr/status" in paths
    assert "/api/asr/transcribe-file" in paths
    assert "/api/asr/ws" in paths


# --------------------------------------------------------------------------
# 未启用 ⇒ 503 明示（不崩服务）
# --------------------------------------------------------------------------
def test_status_503_when_plugin_disabled():
    client = TestClient(_app(asr=None))
    r = client.get("/api/asr/status")
    assert r.status_code == 503
    assert r.json()["available"] is False
    assert "未启用" in r.json()["error"]


def test_transcribe_file_503_when_plugin_disabled():
    client = TestClient(_app(asr=None))
    r = client.post("/api/asr/transcribe-file",
                    files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert r.status_code == 503
    assert "未启用" in r.json()["error"]


def test_ws_closes_when_plugin_disabled():
    """插件未启用的 WS：accept 后立刻发 error 并关闭（前端据此降级）。"""
    client = TestClient(_app(asr=None))
    with client.websocket_connect("/api/asr/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "未启用" in msg["error"]


# --------------------------------------------------------------------------
# 状态端点
# --------------------------------------------------------------------------
def test_status_reports_engine_state():
    client = TestClient(_app(asr=_FakeASRService(ready=False)))
    body = client.get("/api/asr/status").json()
    assert body["available"] is True
    assert body["status"]["state"] == "unavailable"
    assert "sherpa-onnx" in body["status"]["reason"]


# --------------------------------------------------------------------------
# 文件转写（模式 F）
# --------------------------------------------------------------------------
def test_transcribe_file_returns_segment_contract():
    client = TestClient(_app(asr=_FakeASRService()))
    r = client.post("/api/asr/transcribe-file",
                    files={"file": ("meeting.wav", _wav_bytes(), "audio/wav")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["duration_sec"] == 3.2
    assert body["sample_rate"] == 16000
    assert body["mode"] == "offline"
    segs = body["segments"]
    assert len(segs) == 2
    assert set(segs[0]) == {"index", "start_sec", "end_sec", "text"}
    assert segs[0]["text"] == "会议开始"


def test_transcribe_file_400_on_unparsable_audio():
    """解不动的音频 ⇒ 400 明确报错（不谎报 ok+空 segments）。"""
    svc = _FakeASRService()

    async def _boom(path, opts):
        raise ValueError("Format not recognised")

    svc.transcribe_path = _boom
    client = TestClient(_app(asr=svc))
    r = client.post("/api/asr/transcribe-file",
                    files={"file": ("bad.m4a", b"not really audio", "audio/mp4")})
    assert r.status_code == 400
    assert "无法解析音频" in r.json()["error"]


def test_transcribe_file_413_on_oversize(monkeypatch):
    """超限 ⇒ 413，且**在读完整个文件前就中止**（不把超大文件读进内存）。

    把 200MB 上限临时压到 1KB（真造 200MB 夹具会把 CI 拖死）。
    """
    import agent.run.chat as chat_mod

    monkeypatch.setattr(chat_mod, "ASR_UPLOAD_MAX_BYTES", 1024)
    svc = _FakeASRService()
    client = TestClient(_app(asr=svc))
    big = b"\x00" * 4096
    r = client.post("/api/asr/transcribe-file",
                    files={"file": ("big.wav", big, "audio/wav")})
    assert r.status_code == 413
    assert "上限" in r.json()["error"]
    # 关键：超限时**不应**进入转写
    assert svc.pushed == []


def test_transcribe_file_does_not_persist_audio():
    """★ 用户音频不留存：转写后临时文件必须已删（本地私有能力的隐私底线）。"""
    upload_dir = Path("data/asr_uploads")
    before = set(upload_dir.glob("*")) if upload_dir.is_dir() else set()
    client = TestClient(_app(asr=_FakeASRService()))
    r = client.post("/api/asr/transcribe-file",
                    files={"file": ("x.wav", _wav_bytes(), "audio/wav")})
    assert r.status_code == 200
    after = set(upload_dir.glob("*")) if upload_dir.is_dir() else set()
    assert after == before, "上传的音频必须清理，不得留在磁盘"


# --------------------------------------------------------------------------
# WS 流式（模式 R）
# --------------------------------------------------------------------------
def test_ws_frame_sequence_loading_ready_partial_final():
    """帧序是前端契约：loading → ready → partial → … → stop 收 final。"""
    svc = _FakeASRService()
    client = TestClient(_app(asr=svc))
    with client.websocket_connect("/api/asr/ws") as ws:
        assert ws.receive_json()["type"] == "loading"
        assert ws.receive_json()["type"] == "ready"

        ws.send_bytes(b"\x00\x01" * 160)                 # 一帧 PCM
        assert ws.receive_json() == {"type": "partial", "text": "今天天气"}

        ws.send_bytes(b"\x00\x01" * 160)
        assert ws.receive_json() == {"type": "final", "text": "今天天气怎么样"}

        ws.send_text(json.dumps({"type": "stop"}))
        # stop 之后连接应关闭（不抛异常即通过）
    assert svc.stopped is True
    assert len(svc.pushed) == 2


def test_ws_start_frame_accepted():
    client = TestClient(_app(asr=_FakeASRService()))
    with client.websocket_connect("/api/asr/ws") as ws:
        ws.receive_json()      # loading
        ws.receive_json()      # ready
        ws.send_text(json.dumps({"type": "start", "lang": "zh-CN"}))
        ws.send_bytes(b"\x01\x02" * 320)
        assert ws.receive_json()["type"] == "partial"
        ws.send_text(json.dumps({"type": "stop"}))


def test_ws_ignores_unknown_control_frames():
    """未知控制帧不应让连接崩（前端版本错配时的前向兼容）。"""
    svc = _FakeASRService()
    client = TestClient(_app(asr=svc))
    with client.websocket_connect("/api/asr/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_text("not-json-at-all")
        ws.send_text(json.dumps({"type": "unknown-command"}))
        ws.send_bytes(b"\x00\x00" * 320)
        assert ws.receive_json()["type"] == "partial"   # 仍能正常工作


def test_ws_reports_engine_unavailable_for_frontend_fallback():
    """★ 引擎不可用 ⇒ 发 error + 给可读原因（前端据此切 Web Speech API）。"""
    svc = _FakeASRService(raise_on_ready=EngineUnavailable("未安装 sherpa-onnx"))
    client = TestClient(_app(asr=svc))
    with client.websocket_connect("/api/asr/ws") as ws:
        assert ws.receive_json()["type"] == "loading"
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "sherpa-onnx" in msg["error"]


# --------------------------------------------------------------------------
# WS 鉴权（http 中间件不拦 WS ⇒ 必须自校验）
# --------------------------------------------------------------------------
def test_ws_rejects_missing_token_when_auth_enabled():
    client = TestClient(_app(asr=_FakeASRService(), token="secret123"))
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/asr/ws") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_rejects_wrong_token():
    client = TestClient(_app(asr=_FakeASRService(), token="secret123"))
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/asr/ws?token=nope") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_accepts_correct_token_via_query():
    svc = _FakeASRService()
    client = TestClient(_app(asr=svc, token="secret123"))
    with client.websocket_connect("/api/asr/ws?token=secret123") as ws:
        assert ws.receive_json()["type"] == "loading"


def test_ws_open_when_auth_disabled():
    """未启用鉴权（token=""）⇒ WS 不受影响（默认本地 dev 场景）。"""
    client = TestClient(_app(asr=_FakeASRService(), token=""))
    with client.websocket_connect("/api/asr/ws") as ws:
        assert ws.receive_json()["type"] == "loading"
