"""provider 层：``ASRProvider`` 抽象 + ``SherpaOnnxProvider``。

为什么要有抽象
--------------
模式 R与模式 F用的是两个不同模型/两个不同 API
，
但对外承诺的能力形状是同一个：能吃音频、能出文本。第二版要引入
「实时出字 + 离线润色」的 Hybrid 组合，若没有这层抽象，就得把 provider 的
分支散进 service/端点里，换引擎时四处改。

因此抽象只锁能力形状，不锁实现：

 name / streaming_supported / file_supported
 load() -> None
 transcribe_file(...) -> list[Segment]
 create_stream_session() -> StreamSession | None
 refine(text) -> str 首版 passthrough
 vad -> VadModule | None
 describe() -> dict 

诚实性纪律：``describe()`` 必须报告真实能力——
模型没下载就报 ``ready=False`` 并列出缺什么，绝不返回一个"看起来能用"的
provider。降级是显式选择，不是隐式��默。

Segment 契约
 ``{"index": int, "start_sec": float, "end_sec": float, "text": str}``
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from agent.api import EngineUnavailable

from .models import FILE_OFFLINE, STREAMING, TARGET_SAMPLE_RATE, ensure_ready, spec_for
from .vad import VadModule

logger = logging.getLogger("agent.plugins.asr.providers")

# : 引擎不可用——受控失败，由 service 转成明确状态，不静默。
# : 类型本体在 ``agent.core.errors``，经 ``agent.api`` 导出后在此 re-export：
# : 插件与传输层因此都只认 api，传输层不必 import 本模块
# : ，否则撞「装配/传输层依赖业务插件实现」红线，见 core/errors 的同名注释。


@dataclass
class Segment:
    """长录音的一个语音段。``to_dict`` 供端点序列化。"""

    index: int
    start_sec: float
    end_sec: float
    text: str

    def to_dict(self) -> dict:
        return {"index": int(self.index), "start_sec": round(float(self.start_sec), 3),
                "end_sec": round(float(self.end_sec), 3), "text": str(self.text)}


@dataclass
class FileTranscribeResult:
    duration_sec: float
    sample_rate: int
    segments: list[Segment] = field(default_factory=list)
    mode: str = "offline"

    def to_dict(self) -> dict:
        return {"duration_sec": round(float(self.duration_sec), 3),
                "sample_rate": int(self.sample_rate), "mode": self.mode,
                "segments": [s.to_dict() for s in self.segments]}


class StreamSession(ABC):
    """一次实时识别会话。

 与 provider 的关系：provider 持重资源，session 持易变状态
 。这样并发连接不会互相污染 endpoint 判定。
 """

    @abstractmethod
    def push(self, pcm_int16: bytes, sample_rate: int = TARGET_SAMPLE_RATE) -> list[dict]:
        """喂一块 int16 PCM，返回本次产生的帧。帧形如
 ``{"type":"partial"|"final", "text": str}``。"""

    @abstractmethod
    def finish(self) -> list[dict]:
        """收尾：冲刷剩余音频并返回 final 帧。"""

    @abstractmethod
    def reset(self) -> None:
        """清空会话状态。"""


class ASRProvider(ABC):
    """识别引擎的能力契约。"""

    name: str = "abstract"
    streaming_supported: bool = False
    file_supported: bool = False

    def __init__(self, config: dict):
        self.config = dict(config or {})

    @abstractmethod
    def load(self) -> None:
        """加载/自检引擎。不可用时抛 ``EngineUnavailable``。"""

    @abstractmethod
    def describe(self) -> dict:
        """能力自述：``{name, streaming, file, ready, provider_ready, reason, models…}``。"""

    @abstractmethod
    def create_stream_session(self) -> StreamSession | None:
        """新建实时会话；不支持流式返回 ``None``。"""

    @abstractmethod
    def transcribe_file(self, samples, sample_rate: int,
                        opts: dict | None = None) -> FileTranscribeResult:
        """长录音转写：``samples`` 为 float32/16k 单声道。"""

    def refine(self, text: str) -> str:
        """实时出字的定稿钩子。

 首版 passthrough：原样返回，明确不是"已润色"——避免下游误以为
 标点已规范。开关在 ``config['refine']['enabled']``。
 """
        return text


class SherpaStreamingSession(StreamSession):
    """``OnlineRecognizer`` + ``endpoint detection`` 的流式会话。

 断句靠 sherpa 自带的 endpoint 规则，
 判定为 endpoint 即回 ``final`` 并 ``reset`` 该 stream，开始下一句。
 """

    def __init__(self, recognizer, tokens_stream, *, sample_rate: int,
                 rule1_min_trailing_silence: float, rule2_min_trailing_silence: float,
                 rule3_min_utterance_length: float):
        self._rec = recognizer
        self._stream = tokens_stream
        self.sample_rate = int(sample_rate)
        self._r1 = float(rule1_min_trailing_silence)
        self._r2 = float(rule2_min_trailing_silence)
        self._r3 = float(rule3_min_utterance_length)

    def push(self, pcm_int16: bytes, sample_rate: int = TARGET_SAMPLE_RATE) -> list[dict]:
        import numpy as np

        if not pcm_int16:
            return []
        samples = np.frombuffer(pcm_int16, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(sample_rate, samples)
        frames: list[dict] = []
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)
            text = self._rec.get_result(self._stream)
            if not text:
                continue
            if (self._rec.is_endpoint(self._stream)
                    or _is_endpoint_by_rules(samples, self._r1, self._r2, self._r3)):
                frames.append({"type": "final", "text": text})
                self._rec.reset(self._stream)
            else:
                frames.append({"type": "partial", "text": text})
        return frames

    def finish(self) -> list[dict]:
        self._stream.accept_waveform(self.sample_rate, _silence(self.sample_rate))
        frames: list[dict] = []
        while self._rec.is_ready(self._stream):
            self._rec.decode_stream(self._stream)
        text = self._rec.get_result(self._stream)
        if text:
            frames.append({"type": "final", "text": text})
        self.reset()
        return frames

    def reset(self) -> None:
        self._rec.reset(self._stream)


def _silence(sample_rate: int, seconds: float = 0.6) -> "object":
    import numpy as np

    return np.zeros(int(sample_rate * seconds), dtype=np.float32)


def _is_endpoint_by_rules(_last_chunk, r1: float, r2: float, r3: float) -> bool:
    """占位：真实规则判定目前由 sherpa 的 ``is_endpoint`` 承担。

 保留参数与函数形状，是为了让"自定义尾静音阈值"在第二版有明确落点；
 首版返回 False，不与 sherpa 规则重复判定，避免双重断句。
 """
    return False


def _sense_voice_text(result) -> str:
    """从 SenseVoice 的识别结果里取纯文本。

 ``OfflineStream.result`` 对 SenseVoice 而言是 JSON 字符串，形如
 ``{"lang":"<|zh|>","emotion":"<|NEUTRAL|>","event":"<|Speech|>",
 "text":"昨天是星期一","timestamps":[...],"tokens":[...]}``
 —— 直接把它当文本返回，会把整段 JSON 当成转写结果发给前端/下游。
 ⇒ 必须解析后取 ``text``。对非 JSON 则原样返回。
 """
    if result is None:
        return ""
    if isinstance(result, dict):
        return str(result.get("text") or "").strip()
    raw = str(result).strip()
    if raw.startswith("{"):
        try:
            import json
            return str(json.loads(raw).get("text") or "").strip()
        except Exception:                       # noqa: BLE001 —— 非预期格式按原文兜底
            return raw
    return raw.strip()


class SherpaOnnxProvider(ASRProvider):
    """sherpa-onnx 本地引擎：持两个识别器，按模式路由。

 - 实时：``OnlineRecognizer.from_transducer`` + X-ASR chunk-480ms
 ；
 - 长录音：``OfflineRecognizer.from_sense_voice`` + SenseVoice int8
 。

 ``model_type="zipformer2"`` 不是可省参数：官方 ``sherpa_streaming_infer.py``
 就是这么调的；漏了它 zipformer2 加载路径取不到 ``encoder_dims`` 元数据会直接失败。
 """

    name = "sherpa"
    streaming_supported = True
    file_supported = True

    def __init__(self, config: dict, *, sherpa=None):
        super().__init__(config)
        self._sherpa = sherpa            # 注入点；None ⇒ load 时 import
        self._online = None
        self._offline = None
        self._vad: VadModule | None = None
        self._ready = False
        self._reason = ""
        # 配置项不在构造时快照成实例属性：服务门面与本 provider
        # 共享同一个 config dict，容器热重载改配置时若这里已快照，改动会被静默忽略
        # 故一律读取时取值。

    # ---- 配置读取 ----

    @property
    def _sample_rate(self) -> int:
        return int(self.config.get("sample_rate") or TARGET_SAMPLE_RATE)

    @property
    def _num_threads(self) -> int:
        return int(self.config.get("num_threads") or 2)

    @property
    def _streaming_cfg(self) -> dict:
        return dict(self.config.get("streaming") or {})

    @property
    def _file_cfg(self) -> dict:
        return dict(self.config.get("file") or {})

    @property
    def _vad_cfg(self) -> dict:
        return dict(self.config.get("vad") or {})

    @property
    def _auto_download(self) -> bool:
        return bool(self.config.get("auto_download", True))

    @property
    def _model_root(self) -> str:
        return str(self.config.get("model_root") or "data/asr_models")

    # ------------------------------------------------------------------ 加载

    def load(self) -> None:
        """加载引擎 + 模型。缺包/缺模型 ⇒ ``EngineUnavailable``。"""
        if self._ready:
            return
        if self._sherpa is None:
            try:
                import sherpa_onnx
            except Exception as exc:                  # noqa: BLE001 —— 任何导入失败都算不可用
                self._reason = f"未安装 sherpa-onnx（{exc}）；请在服务解释器里 pip install sherpa-onnx"
                raise EngineUnavailable(self._reason) from exc
            self._sherpa = sherpa_onnx
        try:
            if self.streaming_supported:
                self._load_online()
            if self.file_supported or self._vad_cfg.get("enabled", True):
                self._load_offline()
            self._load_vad()
        except EngineUnavailable:
            raise
        except Exception as exc:                      # noqa: BLE001
            self._reason = f"引擎加载失败：{exc}"
            raise EngineUnavailable(self._reason) from exc
        self._ready = True
        self._reason = ""

    def _load_online(self) -> None:
        spec = spec_for("streaming", str(self._streaming_cfg.get("model") or ""))
        ensure_ready("streaming", self._model_root, spec.dir_name,
                     allow_download=self._auto_download)
        p = spec.paths(self._model_root)
        self._online = self._sherpa.OnlineRecognizer.from_transducer(
            tokens=p["tokens"], encoder=p["encoder"], decoder=p["decoder"],
            joiner=p["joiner"], model_type="zipformer2",
            num_threads=self._num_threads, sample_rate=self._sample_rate,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=float(self._streaming_cfg.get("rule1_min_trailing_silence", 0.8)),
            rule2_min_trailing_silence=float(self._streaming_cfg.get("rule2_min_trailing_silence", 1.2)),
            rule3_min_utterance_length=float(self._streaming_cfg.get("rule3_min_utterance_length", 20.0)),
        )

    def _load_offline(self) -> None:
        spec = spec_for("file", str(self._file_cfg.get("model") or ""))
        ensure_ready("file", self._model_root, spec.dir_name,
                     allow_download=self._auto_download)
        p = spec.paths(self._model_root)
        self._offline = self._sherpa.OfflineRecognizer.from_sense_voice(
            model=p["model"], tokens=p["tokens"], use_itn=True,
            num_threads=self._num_threads)

    def _load_vad(self) -> None:
        spec = spec_for("vad", str(self._vad_cfg.get("model") or ""))
        ensure_ready("vad", self._model_root, spec.dir_name,
                     allow_download=self._auto_download)
        # 必须把 ``self._sherpa`` 透传给 VadModule：否则它会自己去 import 真
        # sherpa_onnx，导致注入失效。注入链断在这里会同时毁掉可测性与"引擎降级"语义。
        self._vad = VadModule(
            spec.paths(self._model_root)["model"],
            sample_rate=self._sample_rate,
            min_silence_duration=float(self._vad_cfg.get("min_silence_duration", 0.25)),
            min_speech_duration=float(self._vad_cfg.get("min_speech_duration", 0.1)),
            buffer_size_in_seconds=float(self._vad_cfg.get("buffer_size_in_seconds", 30.0)),
            sherpa=self._sherpa,
        )

    # ------------------------------------------------------------------ 自述

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def reason(self) -> str:
        return self._reason

    def describe(self) -> dict:
        """能力自述。如实报告缺口。"""
        return {
            "name": self.name,
            "streaming": bool(self.streaming_supported),
            "file": bool(self.file_supported),
            "ready": bool(self._ready),
            "reason": self._reason,
            "sample_rate": self._sample_rate,
            "num_threads": self._num_threads,
            "models": {
                "streaming": str(self._streaming_cfg.get("model") or STREAMING.dir_name),
                "file": str(self._file_cfg.get("model") or FILE_OFFLINE.dir_name),
                "vad": str(self._vad_cfg.get("model") or "silero_vad.onnx"),
            },
        }

    # ------------------------------------------------------------------ 实时

    def create_stream_session(self) -> StreamSession | None:
        if not self.streaming_supported:
            return None
        if not self._ready:
            raise EngineUnavailable(self._reason or "流式引擎未就绪")
        stream = self._online.create_stream()
        return SherpaStreamingSession(
            self._online, stream, sample_rate=self._sample_rate,
            rule1_min_trailing_silence=float(self._streaming_cfg.get("rule1_min_trailing_silence", 0.8)),
            rule2_min_trailing_silence=float(self._streaming_cfg.get("rule2_min_trailing_silence", 1.2)),
            rule3_min_utterance_length=float(self._streaming_cfg.get("rule3_min_utterance_length", 20.0)),
        )

    # ------------------------------------------------------------------ 长录音

    def transcribe_file(self, samples, sample_rate: int,
                        opts: dict | None = None) -> FileTranscribeResult:
        import numpy as np

        if not self._ready:
            raise EngineUnavailable(self._reason or "离线引擎未就绪")
        if not self.file_supported:
            raise EngineUnavailable("当前 provider 不支持长录音转写")
        opts = dict(opts or {})
        audio = np.asarray(samples, dtype=np.float32)
        sr = int(sample_rate or self._sample_rate)
        duration = float(len(audio)) / float(sr or 1)

        segs: list[Segment] = []
        if audio.size:
            min_len = float(opts.get("min_segment_sec", 0.0) or 0.0)
            ranges = (self._vad.iter_segments(audio, min_len_sec=min_len)
                      if self._vad is not None else [(0.0, duration)])
            for idx, (start, end) in enumerate(ranges):
                seg_audio = audio[int(start * sr):int(end * sr)]
                if seg_audio.size == 0:
                    continue
                stream = self._offline.create_stream()
                stream.accept_waveform(sr, seg_audio)
                self._offline.decode_stream(stream)
                text = _sense_voice_text(getattr(stream, "result", ""))
                segs.append(Segment(index=idx, start_sec=start, end_sec=end, text=text))
        return FileTranscribeResult(duration_sec=duration, sample_rate=sr,
                                    segments=segs, mode="offline")


__all__ = ["ASRProvider", "EngineUnavailable", "FileTranscribeResult", "Segment",
           "SherpaOnnxProvider", "SherpaStreamingSession", "StreamSession"]