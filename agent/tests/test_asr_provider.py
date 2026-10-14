"""ASR provider 层契约：能力抽象、refine passthrough、配置合并、不可用降级。

**全部用假 sherpa 模块**（不加载真模型）：契约测试要的是"分支覆盖"，不是识别精度。
真引擎的加载与 decode 已在阶段0 验收（``data/asr_models/verify_stage0.py``）与
手工端到端验证中完成，**不在单元测试里重复**（一次加载几百 MB，会把测试拖成分钟级）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.plugins.asr import DEFAULT_CONFIG, _deep_merge, _config
from agent.plugins.asr.models import SPECS, ensure_ready, spec_for, status
from agent.plugins.asr.providers import (ASRProvider, EngineUnavailable, Segment,
                                         SherpaOnnxProvider)
from agent.plugins.asr.service import (STATE_READY, STATE_UNAVAILABLE, ASRService)


# --------------------------------------------------------------------------
# 假 sherpa：只需覆盖 load/describe/会话三条路径
# --------------------------------------------------------------------------
class _FakeOnline:
    def __init__(self):
        self.streams = []

    def create_stream(self):
        st = _FakeStream()
        self.streams.append(st)
        return st


class _FakeStream:
    def __init__(self):
        self.waveforms = []

    def accept_waveform(self, sr, samples):
        self.waveforms.append((sr, len(samples)))


class _FakeOffline:
    def create_stream(self):
        return _FakeOfflineStream()


class _FakeOfflineStream:
    def __init__(self):
        self.result = ""

    def accept_waveform(self, sr, samples):
        self.n = len(samples)


class _FakeOnlineRecognizer:
    last_kwargs = None

    @classmethod
    def from_transducer(cls, **kwargs):
        _FakeOnlineRecognizer.last_kwargs = kwargs
        return _FakeOnline()


class _FakeOfflineRecognizer:
    @classmethod
    def from_sense_voice(cls, **kwargs):
        return _FakeOffline()


class _FakeSherpa:
    OnlineRecognizer = _FakeOnlineRecognizer
    OfflineRecognizer = _FakeOfflineRecognizer

    class VadModelConfig:
        def __init__(self):
            self.silero_vad = type("S", (), {})()

    class VoiceActivityDetector:
        def __init__(self, cfg, buffer_size_in_seconds=30):
            self.cfg = cfg
            self.buffer_size_in_seconds = buffer_size_in_seconds

        def accept_waveform(self, samples):
            pass

        def flush(self):
            pass

        def empty(self):
            return True

        def reset(self):
            pass


def _cfg(**over):
    base = {
        "provider": "sherpa", "sample_rate": 16000, "num_threads": 1,
        "auto_download": False,
        "streaming": {"model": "sherpa-onnx-x-asr-zh-en-chunk-480ms"},
        "file": {"model": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"},
        "vad": {"enabled": True},
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# provider 抽象
# --------------------------------------------------------------------------
def test_asr_provider_is_abstract():
    """基类不可直接实例化（防止漏实现方法悄悄混进来）。"""
    with pytest.raises(TypeError):
        ASRProvider({})       # type: ignore[abstract]


def test_sherpa_provider_declares_both_modes():
    p = SherpaOnnxProvider(_cfg(), sherpa=_FakeSherpa)
    assert p.streaming_supported is True
    assert p.file_supported is True
    assert p.name == "sherpa"


def test_refine_is_passthrough_first_version():
    """★ 首版 refine 必须**原样返回**——它是第二版钩子，不通电。

    诚实性要求：调用方不得误以为文本已被标点/ITN 规范过。
    """
    p = SherpaOnnxProvider(_cfg(), sherpa=_FakeSherpa)
    assert p.refine("今天天气怎么样") == "今天天气怎么样"
    assert p.refine("") == ""


def test_load_sets_ready_and_describe_reports_capabilities():
    p = SherpaOnnxProvider(_cfg(), sherpa=_FakeSherpa)
    p.load()
    assert p.ready is True
    d = p.describe()
    assert d["ready"] is True and d["streaming"] and d["file"]
    assert d["reason"] == ""
    assert d["models"]["streaming"].startswith("sherpa-onnx-x-asr")
    assert "sense-voice" in d["models"]["file"]


def test_load_passes_model_type_zipformer2():
    """★ ``model_type="zipformer2"`` 必须显式传——漏了会因缺 encoder_dims 失败。"""
    p = SherpaOnnxProvider(_cfg(), sherpa=_FakeSherpa)
    p.load()
    kw = _FakeOnlineRecognizer.last_kwargs
    assert kw is not None
    assert kw["model_type"] == "zipformer2"
    assert kw["enable_endpoint_detection"] is True
    assert kw["sample_rate"] == 16000


def test_load_idempotent():
    """重复 load 不应重��加载（WS 每次连接都会 ensure_ready）。"""
    p = SherpaOnnxProvider(_cfg(), sherpa=_FakeSherpa)
    p.load()
    first = p._online
    p.load()
    assert p._online is first


# --------------------------------------------------------------------------
# 不可用（诚实降级）
# --------------------------------------------------------------------------
def test_missing_sherpa_raises_engine_unavailable(monkeypatch):
    """缺依赖 ⇒ EngineUnavailable + **可读 reason**，不是崩栈/静默降级。"""
    import builtins
    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name == "sherpa_onnx":
            raise ImportError("No module named 'sherpa_onnx'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    p = SherpaOnnxProvider(_cfg())
    with pytest.raises(EngineUnavailable) as exc:
        p.load()
    assert "sherpa-onnx" in str(exc.value)


def test_missing_model_raises_engine_unavailable(tmp_path):
    """模型缺失且 auto_download=False ⇒ 明确报错（不静默、不假就绪）。"""
    p = SherpaOnnxProvider(_cfg(), sherpa=_FakeSherpa)
    p.config["model_root"] = str(tmp_path)          # 空目录 ⇒ 三个模型都不在
    with pytest.raises(EngineUnavailable) as exc:
        p.load()
    assert "缺失" in str(exc.value)


def test_transcribe_before_ready_raises():
    """未 load 就转写 ⇒ EngineUnavailable（不返回空结果骗人）。"""
    p = SherpaOnnxProvider(_cfg(), sherpa=_FakeSherpa)
    with pytest.raises(EngineUnavailable):
        p.transcribe_file([0.0] * 100, 16000)


# --------------------------------------------------------------------------
# Segment 契约
# --------------------------------------------------------------------------
def test_segment_to_dict_shape():
    """Segment 序列化形状是**端点/下游契约**（index/start_sec/end_sec/text）。"""
    d = Segment(index=0, start_sec=1.2345, end_sec=8.7654, text="会议开始").to_dict()
    assert d == {"index": 0, "start_sec": 1.234, "end_sec": 8.765, "text": "会议开始"}


# --------------------------------------------------------------------------
# 配置合并（容器级 ← 会话级）
# --------------------------------------------------------------------------
def test_deep_merge_preserves_sibling_keys():
    """★ 嵌套节必须深合并——浅合并会把未覆盖的兄弟键整节丢掉。"""
    merged = _deep_merge(DEFAULT_CONFIG, {"streaming": {"model": "custom-model"}})
    assert merged["streaming"]["model"] == "custom-model"
    # 兄弟键（endpoint 阈值）必须保留
    assert "rule1_min_trailing_silence" in merged["streaming"]
    assert merged["file"]["model"].startswith("sherpa-onnx-sense-voice")


class _FakeCtx:
    def __init__(self, config=None, view=None):
        self.config = config or {}
        self._view = view

    def get(self, key):
        return self._view


def test_default_config_vad_model_must_be_empty():
    """★ 回归：``vad.model`` 默认必须是 **空串**（= 用清单默认落点）。

    填 ``silero_vad.onnx`` 会被 ``spec_for()`` 当**目录名**⇒ 路径拼成
    ``.../silero_vad.onnx/silero_vad.onnx`` ⇒ ``mkdir`` 撞同名文件 ⇒
    ``FileExistsError(17)`` ⇒ **引擎整体不可用**（实测踩过，reason 显示
    ``WinError 183``，极难定位）。此测试把该语义钉死。
    """
    assert DEFAULT_CONFIG["vad"]["model"] == ""


def test_status_and_load_agree_on_vad_override():
    """★ 回归：``status()`` 的模型视图必须与 ``load()`` 用**同一套** spec 解析。

    漏掉 vad override 时：status 报"就绪"而 load 实际失败 —— 状态与事实错位，
    前端会因此判定引擎可用却拿不到识别结果。
    """
    from agent.plugins.asr import DEFAULT_CONFIG as DC

    cfg = {**DC, "auto_download": False}
    svc = ASRService(cfg)
    try:
        st = svc.status()
        spec = spec_for("vad", str((cfg.get("vad") or {}).get("model") or ""))
        paths = spec.paths()
        # status 报告的 files 必须与 load 实际会用的路径逐字一致
        assert st["models"]["vad"]["files"]["model"] == paths["model"]
        assert st["models"]["vad"]["ready"] is True
    finally:
        svc.dispose()


def test_config_reads_container_section():
    ctx = _FakeCtx(config={"agent": {"asr": {"num_threads": 8}}})
    cfg = _config(ctx)
    assert cfg["num_threads"] == 8
    assert cfg["provider"] == "sherpa"          # 未覆盖键取默认


def test_config_session_overrides_container():
    """会话级覆盖容器级（与 sandbox 同构：SESSION_CFG_VIEW 返回已解包的节）。"""
    from agent.api import SESSION_CFG_VIEW
    from agent.core.session import set_current_session_key

    def _view(section, sk):
        return {"num_threads": 2} if section == "asr" else {}

    ctx = _FakeCtx(config={"agent": {"asr": {"num_threads": 8, "sample_rate": 16000}}})
    ctx.get = lambda key: (_view if key == SESSION_CFG_VIEW else None)
    # _config 靠 current_session_key(ctx) 判定是否读会话级；无键会**提前**回落容器级
    set_current_session_key("sess-test-asr")
    try:
        cfg = _config(ctx)
    finally:
        set_current_session_key(None)
    assert cfg["num_threads"] == 2               # 会话级胜出
    assert cfg["sample_rate"] == 16000           # 容器级保留


# --------------------------------------------------------------------------
# 模型清单
# --------------------------------------------------------------------------
def test_model_spec_paths_are_unique_and_named():
    """三个模型的角色路径互不重叠（错位会让 provider 拿到别人的文件）。"""
    allp = []
    for key, spec in SPECS.items():
        paths = list(spec.paths().values())
        assert all(p.endswith((".onnx", ".txt")) for p in paths), key
        allp.extend(paths)
    assert len(allp) == len(set(allp))


def test_status_reports_missing_honestly(tmp_path):
    """空目录 ⇒ 三个模型都 ready=False，且**列出缺什么**（不谎报）。"""
    st = status(str(tmp_path))
    assert all(not v["ready"] for v in st.values())
    assert st["streaming"]["missing"] and st["file"]["missing"]


def test_ensure_ready_no_download_raises_with_names(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        ensure_ready("file", str(tmp_path), allow_download=False)
    assert "model" in str(exc.value) and "tokens" in str(exc.value)


def test_spec_for_allows_model_override():
    """配置换模型目录名 ⇒ 目录跟着变（URL 仍复用同角色的清单）。"""
    s = spec_for("streaming", "my-own-dir")
    assert s.dir_name == "my-own-dir"
    assert set(s.files) == {"encoder", "decoder", "joiner", "tokens"}


# --------------------------------------------------------------------------
# Service 状态机
# --------------------------------------------------------------------------
def test_service_lazy_loading_state_transitions():
    """idle → ready：未调用时不加载（不拖慢启动/装配）。"""
    async def _run():
        svc = ASRService(_cfg(), provider=SherpaOnnxProvider(_cfg(), sherpa=_FakeSherpa))
        assert svc.state == "idle"
        assert svc.status()["state"] == "idle"
        await svc.ensure_ready()
        assert svc.state == STATE_READY
        assert svc.status()["provider"]["ready"] is True
        svc.dispose()
    asyncio.run(_run())


def test_service_unavailable_state_is_visible():
    """不可用 ⇒ state=unavailable + reason（前端据此降级 Web Speech）。"""
    async def _run():
        bad = SherpaOnnxProvider(_cfg(), sherpa=_FakeSherpa)
        bad.config["model_root"] = "/nonexistent-model-root-xyz"
        svc = ASRService(_cfg(), provider=bad)
        with pytest.raises(EngineUnavailable):
            await svc.ensure_ready()
        assert svc.state == STATE_UNAVAILABLE
        assert svc.status()["reason"]
        svc.dispose()
    asyncio.run(_run())


def test_service_status_before_any_load_reports_models():
    """未加载时也能报模型就绪情况（前端/设置页不需要触发加载）。"""
    svc = ASRService(_cfg())
    st = svc.status()
    assert set(st["models"]) == {"streaming", "file", "vad"}
    assert svc.state == "idle"
    svc.dispose()