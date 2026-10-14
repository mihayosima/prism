"""ASR 长录音通道（模式 F）：VAD 切段 → 离线识别 → ``Segment[]``。

**用假 sherpa + 假 VAD**（不加载真模型）：本文件验的是
"切段边界有没有被正确传给识别器""结果顺序/时间戳对不对""不可用时怎么办"，
这些都不需要真的跑 ONNX 图（那属于阶段0 与手工端到端验证的范畴）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.plugins.asr.models import resample
from agent.plugins.asr.providers import EngineUnavailable, SherpaOnnxProvider
from agent.plugins.asr.vad import VadModule


# --------------------------------------------------------------------------
# 假 VAD：按"每 N 采样一段"造确定性的切段结果
# --------------------------------------------------------------------------
class _Seg:
    def __init__(self, start, samples):
        self.start = start
        self.samples = samples


class _FakeVadDetector:
    """可控切段的假检测器：``_segments`` 由测试给定。"""

    instances: list["_FakeVadDetector"] = []

    def __init__(self, cfg=None, buffer_size_in_seconds=30):
        self.cfg = cfg
        self.buffer_size_in_seconds = buffer_size_in_seconds
        self.buffer = []
        self.flushed = False
        _FakeVadDetector.instances.append(self)

    def accept_waveform(self, samples):
        self.buffer.append(np.asarray(samples))

    def flush(self):
        self.flushed = True

    def empty(self):
        return not self.buffer

    @property
    def front(self):
        # ★ 真 sherpa 1.13.8 的 ``front`` 是**属性**（返回 SpeechSegment），不是方法
        return self.buffer[0]

    def pop(self):
        return self.buffer.pop(0)

    def reset(self):
        self.buffer = []


def _fake_sherpa_with_segments(segments, *, empty_result: str = ""):
    """造一个假 sherpa 模块：VAD 按给定段切、离线识别返回固定文本。

    ★ ``segments`` 元素为 ``(start_sample_index, n_samples)``——**直接用采样点**，
      与真 ``SpeechSegment.start`` 的单位完全一致（不引入换算，避免"到底谁除 sr"的歧义）。
      调用方自己把秒换算成采样点（``int(sr * 0.5)``），一眼可读。
    """

    class _VadModelConfig:
        def __init__(self):
            self.silero_vad = type("S", (), {})()

    class _Det:
        instances: list = []

        def __init__(self, cfg, buffer_size_in_seconds=30):
            self._cfg = cfg
            self._segs = [(int(st), int(n)) for st, n in segments]
            self._i = 0
            self.reset_count = 0
            self.accepted_chunks = 0

        def accept_waveform(self, samples):
            # ★ 分块喂入：整段灌会溢出环形缓冲（生产代码已因此丢段）
            self.accepted_chunks += 1

        def flush(self):
            pass

        def empty(self):
            return self._i >= len(self._segs)

        @property
        def front(self):
            # ★ 真 sherpa 1.13.8：``front`` 是 property（写 ``front()`` 会
            #   报 'SpeechSegment' object is not callable —— 生产代码曾真踩）
            st, n = self._segs[self._i]
            return _Seg(st, np.zeros(n, dtype=np.float32))

        def pop(self):
            self._i += 1

        def reset(self):
            self.reset_count += 1

    class _OfflineStream:
        def __init__(self):
            self.result = empty_result
            self.accepted = None
            self.decoded = 0

        def accept_waveform(self, sr, samples):
            self.accepted = (sr, len(samples))

    class _Offline:
        def __init__(self):
            self.streams: list = []

        def create_stream(self):
            st = _OfflineStream()
            self.streams.append(st)
            return st

        def decode_stream(self, stream):
            # 真实 API 是同步 decode（结果落到 stream.result）；此处只记账
            stream.decoded += 1

    class _OfflineRecognizer:
        @classmethod
        def from_sense_voice(cls, **kwargs):
            return _Offline()

    class _OnlineRecognizer:
        @classmethod
        def from_transducer(cls, **kwargs):
            return object()

    mod = type("FakeSherpa", (), {
        "VadModelConfig": _VadModelConfig,
        "VoiceActivityDetector": _Det,
        "OfflineRecognizer": _OfflineRecognizer,
        "OnlineRecognizer": _OnlineRecognizer,
    })
    return mod


def _cfg(**over):
    base = {"sample_rate": 16000, "num_threads": 1, "auto_download": False,
            "streaming": {"model": "sherpa-onnx-x-asr-zh-en-chunk-480ms"},
            "file": {"model": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"},
            "vad": {"enabled": True}}
    base.update(over)
    return base


# --------------------------------------------------------------------------
# VadModule 自身
# --------------------------------------------------------------------------
def test_segment_file_returns_start_and_computed_end():
    """★ ``SpeechSegment`` **没有 end** ⇒ 终点必须由 samples 长度算出。"""
    sr = 16000
    # 一段 1.0s 起的语音，长 0.5s ⇒ end = 1.5
    fake = _fake_sherpa_with_segments([(int(sr * 1.0), int(sr * 0.5))])
    vad = VadModule("dummy.onnx", sample_rate=sr, sherpa=fake)
    segs = vad.segment_file(np.zeros(sr * 3, dtype=np.float32))
    assert len(segs) == 1
    start, end = segs[0]
    assert start == pytest.approx(1.0)
    assert end == pytest.approx(1.5)


def test_segment_file_calls_flush_before_popping():
    """必须 flush 尾部残留，否则最后一句会被丢掉。"""
    fake = _fake_sherpa_with_segments([])
    det_box = {}

    vad = VadModule("dummy.onnx", sherpa=fake)
    vad.load()
    det = fake.VoiceActivityDetector(None, 30)      # 再造一个只为拿 flush 记录
    vad._vad.flush = lambda: det_box.setdefault("flushed", True)
    vad.segment_file(np.zeros(1600, dtype=np.float32))
    assert det_box.get("flushed") is True


def test_vad_reset_called_before_new_segmentation():
    """换音频源前必须 reset，否则上一段尾音会漏进下一段。"""
    fake = _fake_sherpa_with_segments([])
    vad = VadModule("dummy.onnx", sherpa=fake)
    vad.load()
    calls = []
    vad._vad.reset = lambda: calls.append(1)
    vad.segment_file(np.zeros(1600, dtype=np.float32))
    assert calls, "segment_file 入口必须 reset VAD"


def test_iter_segments_filters_short_segments():
    """过短的段（一声'嗯'）应被过滤掉，否则污染下游摘要。"""
    sr = 16000
    segs = [(int(sr * 0.0), int(sr * 0.2)),   # 0.2s → 过滤
            (int(sr * 1.0), int(sr * 2.0))]   # 2.0s → 保留
    fake = _fake_sherpa_with_segments(segs)
    vad = VadModule("dummy.onnx", sample_rate=sr, sherpa=fake)
    got = list(vad.iter_segments(np.zeros(sr * 5, dtype=np.float32), min_len_sec=0.5))
    assert len(got) == 1
    assert got[0][0] == pytest.approx(1.0)


def test_pop_segment_returns_none_when_empty():
    fake = _fake_sherpa_with_segments([])
    vad = VadModule("dummy.onnx", sherpa=fake)
    assert vad.pop_segment() is None


# --------------------------------------------------------------------------
# transcribe_file（VAD → 识别 → Segment[]）
# --------------------------------------------------------------------------
def test_segment_start_is_sample_index_not_seconds():
    """★ ``SpeechSegment.start`` 是**采样点索引**（int），不是秒。

    真实值 158144 出现在 10.05s 音频里 ⇒ 若当秒用，所有切片落在音频外 ⇒ 零段。
    这条测试把该单位钉死：改错单位必然红。
    """
    sr = 16000
    # start=8000 **采样点** ⇒ 0.5s；段长 0.25s（8000 采样点已是正确单位，勿再乘 sr）
    fake = _fake_sherpa_with_segments([(8000, 4000)])
    vad = VadModule("dummy.onnx", sample_rate=sr, sherpa=fake)
    segs = vad.segment_file(np.zeros(sr * 3, dtype=np.float32))
    assert len(segs) == 1
    assert segs[0][0] == pytest.approx(0.5, abs=1e-3)
    assert segs[0][1] == pytest.approx(0.75, abs=1e-3)


def test_segment_file_feeds_in_chunks_to_avoid_ring_overflow():
    """★ 环形缓冲容量有限（实测 ≈10s @16k）⇒ 必须分块喂入并及时 pop。

    整段灌入会溢出并**丢掉已完成语音段**（10.05s 只剩末尾 0.38s）。
    本测试断言"喂入次数 > 1"（即确实分块），防止有人改成整段灌入。
    """
    sr = 16000
    fake = _fake_sherpa_with_segments([(int(sr * 0.0), int(sr * 0.5)), (int(sr * 2.0), int(sr * 0.5))])
    vad = VadModule("dummy.onnx", sample_rate=sr, sherpa=fake)
    det_holder = {}

    class _SpyDet:
        """记 accept_waveform 调用次数（含 init 期间构造的那一个实例不可得，
        故在 load 后替换底层对象的 accept_waveform）。"""

    vad.load()
    orig = vad._vad.accept_waveform
    calls = []

    def _counted(samples):
        calls.append(len(samples))
        return orig(samples)

    vad._vad.accept_waveform = _counted
    audio = np.zeros(sr * 10, dtype=np.float32)       # 10s ⇒ 整段灌必溢出
    segs = vad.segment_file(audio, chunk_seconds=1.0)
    assert len(calls) > 1, "必须分块喂入（整段灌会溢出丢段）"
    assert len(segs) == 2


def test_segment_file_clamps_out_of_range_segments():
    """越界段（start 超出音频尾部）必须被夹紧/丢弃，否则切片为空 → 识别器收到 0 长音频。"""
    sr = 16000
    fake = _fake_sherpa_with_segments([(int(sr * 0.0), int(sr * 0.5)), (int(sr * 99.0), int(sr * 0.5))])
    vad = VadModule("dummy.onnx", sample_rate=sr, sherpa=fake)
    segs = vad.segment_file(np.zeros(sr * 2, dtype=np.float32))   # 音频只有 2s
    assert len(segs) == 1
    assert segs[0][1] <= 2.0


def test_sense_voice_json_result_is_unwrapped_to_text():
    """★ SenseVoice 的 ``stream.result`` 是 **JSON 字符串**，必须取 ``text`` 字段。

    漏了会把整段 JSON 当转写结果返回（实测踩到：输出里带 lang/emotion/tokens）。
    """
    from agent.plugins.asr.providers import _sense_voice_text

    payload = ('{"lang":"<|zh|>","emotion":"<|NEUTRAL|>","event":"<|Speech|>",'
               '"text":"昨天是星期一","timestamps":[0.0,0.24],"tokens":["昨","天"]}')
    assert _sense_voice_text(payload) == "昨天是星期一"
    # 非 JSON 文本原样返回（其它离线模型给的是纯 str）
    assert _sense_voice_text("  hello world  ") == "hello world"
    assert _sense_voice_text(None) == ""
    assert _sense_voice_text({"text": "已是 dict"}) == "已是 dict"


def test_transcribe_file_produces_indexed_segments():
    sr = 16000
    segs = [(int(sr * 0.0), int(sr * 1.0)),
            (int(sr * 1.2), int(sr * 1.0))]
    fake = _fake_sherpa_with_segments(segs, empty_result="今天天气怎么样")
    p = SherpaOnnxProvider(_cfg(), sherpa=fake)
    p.load()
    audio = np.zeros(sr * 3, dtype=np.float32)
    res = p.transcribe_file(audio, sr, {})
    assert res.duration_sec == pytest.approx(3.0)
    assert len(res.segments) == 2
    assert [s.index for s in res.segments] == [0, 1]
    assert res.segments[1].start_sec == pytest.approx(1.2)
    assert res.segments[0].text == "今天天气怎么样"
    # 序列化形状
    d = res.to_dict()
    assert set(d) == {"duration_sec", "sample_rate", "mode", "segments"}
    assert set(d["segments"][0]) == {"index", "start_sec", "end_sec", "text"}


def test_transcribe_file_empty_audio_yields_no_segments():
    """空音频 ⇒ 零段（不是异常，也不是一段空文本）。"""
    fake = _fake_sherpa_with_segments([])
    p = SherpaOnnxProvider(_cfg(), sherpa=fake)
    p.load()
    res = p.transcribe_file(np.zeros(0, dtype=np.float32), 16000, {})
    assert res.segments == []
    assert res.duration_sec == 0.0


def test_transcribe_file_respects_min_segment_filter():
    sr = 16000
    segs = [(int(sr * 0.0), int(sr * 0.2)),
            (int(sr * 1.0), int(sr * 1.5))]
    fake = _fake_sherpa_with_segments(segs, empty_result="嗯")
    p = SherpaOnnxProvider(_cfg(file={"model": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
                                      "min_segment_sec": 0.5}), sherpa=fake)
    p.load()
    res = p.transcribe_file(np.zeros(sr * 3, dtype=np.float32), 16000,
                            {"min_segment_sec": 0.5})
    assert len(res.segments) == 1
    assert res.segments[0].start_sec == pytest.approx(1.0)


def test_transcribe_file_rejects_when_provider_file_unsupported():
    """provider 不支持长录音 ⇒ 明确报错。"""
    fake = _fake_sherpa_with_segments([])
    p = SherpaOnnxProvider(_cfg(), sherpa=fake)
    p.file_supported = False
    p.load()
    with pytest.raises(EngineUnavailable):
        p.transcribe_file(np.zeros(16000, dtype=np.float32), 16000, {})


# --------------------------------------------------------------------------
# 重采样
# --------------------------------------------------------------------------
def test_resample_changes_length_proportionally():
    x = np.zeros(44100, dtype=np.float32)
    y = resample(x, 44100, 16000)
    assert abs(len(y) - 16000) <= 1


def test_resample_identity_when_same_rate():
    x = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    y = resample(x, 16000, 16000)
    assert y is x or np.array_equal(x, y)


def test_resample_handles_empty():
    assert len(resample(np.zeros(0, dtype=np.float32), 44100, 16000)) == 0


# --------------------------------------------------------------------------
# 音频读取失败 ⇒ 端点必须报错而非谎报空结果（在 service 层验证异常传播）
# --------------------------------------------------------------------------
def test_transcribe_path_propagates_read_error(tmp_path):
    """读不动的文件 ⇒ 异常上抛（端点据此回 400/503，**绝不谎报"没说话"**）。

    走 service 门面（``transcribe_path`` 在 service 上——provider 只吃已解码的采样）。
    """
    from agent.plugins.asr.service import ASRService

    provider = SherpaOnnxProvider(_cfg(), sherpa=_fake_sherpa_with_segments([]))
    svc = ASRService(_cfg(), provider=provider)
    bad = tmp_path / "not-audio.wav"
    bad.write_bytes(b"this is not audio")
    try:
        asyncio.run(svc.ensure_ready())
        with pytest.raises(Exception):
            asyncio.run(svc.transcribe_path(str(bad), {}))
    finally:
        svc.dispose()